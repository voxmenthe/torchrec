#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import copy
import itertools
from collections import defaultdict
from typing import Callable, cast, Dict, List, Optional, Tuple, Type, Union

import torch
import torch.nn as nn
from fbgemm_gpu.split_table_batched_embeddings_ops_inference import (
    EmbeddingLocation,
    IntNBitTableBatchedEmbeddingBagsCodegen,
    PoolingMode,
)
from torch import Tensor
from torchrec.distributed.utils import none_throws
from torchrec.modules.embedding_configs import (
    BaseEmbeddingConfig,
    DATA_TYPE_NUM_BITS,
    data_type_to_sparse_type,
    DataType,
    dtype_to_data_type,
    EmbeddingBagConfig,
    EmbeddingConfig,
    pooling_type_to_pooling_mode,
    PoolingType,
    QuantConfig,
)
from torchrec.modules.embedding_modules import (
    EmbeddingBagCollection as OriginalEmbeddingBagCollection,
    EmbeddingBagCollectionInterface,
    EmbeddingCollection as OriginalEmbeddingCollection,
    EmbeddingCollectionInterface,
    get_embedding_names_by_table,
)
from torchrec.modules.feature_processor_ import FeatureProcessorsCollection
from torchrec.modules.fp_embedding_modules import (
    FeatureProcessedEmbeddingBagCollection as OriginalFeatureProcessedEmbeddingBagCollection,
)

from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor, KeyedTensor
from torchrec.tensor_types import UInt2Tensor, UInt4Tensor
from torchrec.types import ModuleNoCopyMixin

try:
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops")
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops_cpu")
except OSError:
    pass

# OSS
try:
    pass
except ImportError:
    pass

MODULE_ATTR_REGISTER_TBES_BOOL: str = "__register_tbes_in_named_modules"

MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS: str = (
    "__quant_state_dict_split_scale_bias"
)

MODULE_ATTR_ROW_ALIGNMENT_INT: str = "__register_row_alignment_in_named_modules"

MODULE_ATTR_EMB_CONFIG_NAME_TO_PRUNING_INDICES_REMAPPING_DICT: str = (
    "__emb_name_to_pruning_indices_remapping"
)

DEFAULT_ROW_ALIGNMENT = 16


@torch.fx.wrap
def set_fake_stbe_offsets(values: torch.Tensor) -> torch.Tensor:
    return torch.arange(
        0,
        values.numel() + 1,
        device=values.device,
        dtype=values.dtype,
    )


def for_each_module_of_type_do(
    module: nn.Module,
    module_types: List[Type[torch.nn.Module]],
    op: Callable[[torch.nn.Module], None],
) -> None:
    for m in module.modules():
        if any([isinstance(m, t) for t in module_types]):
            op(m)


def quant_prep_enable_quant_state_dict_split_scale_bias(module: nn.Module) -> None:
    setattr(module, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, True)


def quant_prep_enable_quant_state_dict_split_scale_bias_for_types(
    module: nn.Module, module_types: List[Type[torch.nn.Module]]
) -> None:
    for_each_module_of_type_do(
        module,
        module_types,
        lambda m: setattr(m, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, True),
    )


def quant_prep_enable_register_tbes(
    module: nn.Module, module_types: List[Type[torch.nn.Module]]
) -> None:
    for_each_module_of_type_do(
        module,
        module_types,
        lambda m: setattr(m, MODULE_ATTR_REGISTER_TBES_BOOL, True),
    )


def quant_prep_customize_row_alignment(
    module: nn.Module, module_types: List[Type[torch.nn.Module]], row_alignment: int
) -> None:
    for_each_module_of_type_do(
        module,
        module_types,
        lambda m: setattr(m, MODULE_ATTR_ROW_ALIGNMENT_INT, row_alignment),
    )


def pruned_num_embeddings(pruning_indices_mapping: Tensor) -> int:
    return int(torch.max(pruning_indices_mapping).item()) + 1


def quantize_state_dict(
    module: nn.Module,
    table_name_to_quantized_weights: Dict[str, Tuple[Tensor, Tensor]],
    table_name_to_data_type: Dict[str, DataType],
    table_name_to_pruning_indices_mapping: Optional[Dict[str, Tensor]] = None,
) -> torch.device:
    device = torch.device("cpu")
    if not table_name_to_pruning_indices_mapping:
        table_name_to_pruning_indices_mapping = {}
    for key, tensor in module.state_dict().items():
        # Extract table name from state dict key.
        # e.g. ebc.embedding_bags.t1.weight
        splits = key.split(".")
        assert splits[-1] == "weight"
        table_name = splits[-2]
        data_type = table_name_to_data_type[table_name]
        num_rows = tensor.shape[0]
        pruning_indices_mapping: Optional[Tensor] = None
        if table_name in table_name_to_pruning_indices_mapping:
            pruning_indices_mapping = table_name_to_pruning_indices_mapping[table_name]
        if pruning_indices_mapping is not None:
            num_rows = pruned_num_embeddings(pruning_indices_mapping)

        device = tensor.device
        num_bits = DATA_TYPE_NUM_BITS[data_type]

        if tensor.is_meta:
            quant_weight = torch.empty(
                (num_rows, (tensor.shape[1] * num_bits) // 8),
                device="meta",
                dtype=torch.uint8,
            )
            if (
                data_type == DataType.INT8
                or data_type == DataType.INT4
                or data_type == DataType.INT2
            ):
                scale_shift = torch.empty(
                    (num_rows, 4),
                    device="meta",
                    dtype=torch.uint8,
                )
            else:
                scale_shift = None
        else:
            if pruning_indices_mapping is not None:
                rows_mask = pruning_indices_mapping.gt(-1)
                tensor = tensor[rows_mask, :]

            if tensor.dtype == torch.float or tensor.dtype == torch.float16:
                if data_type == DataType.FP16:
                    if tensor.dtype == torch.float:
                        tensor = tensor.half()
                    quant_res = tensor.view(torch.uint8)
                else:
                    quant_res = (
                        torch.ops.fbgemm.FloatOrHalfToFusedNBitRowwiseQuantizedSBHalf(
                            tensor, num_bits
                        )
                    )
            else:
                raise Exception("Unsupported dtype: {tensor.dtype}")
            if (
                data_type == DataType.INT8
                or data_type == DataType.INT4
                or data_type == DataType.INT2
            ):
                quant_weight, scale_shift = (
                    quant_res[:, :-4],
                    quant_res[:, -4:],
                )
            else:
                quant_weight, scale_shift = quant_res, None
        table_name_to_quantized_weights[table_name] = (quant_weight, scale_shift)
    return device


def _update_embedding_configs(
    embedding_configs: List[BaseEmbeddingConfig],
    quant_config: Union[QuantConfig, torch.quantization.QConfig],
) -> None:
    per_table_weight_dtype = (
        quant_config.per_table_weight_dtype
        if isinstance(quant_config, QuantConfig) and quant_config.per_table_weight_dtype
        else {}
    )
    for config in embedding_configs:
        config.data_type = dtype_to_data_type(
            per_table_weight_dtype[config.name]
            if config.name in per_table_weight_dtype
            else quant_config.weight().dtype
        )


class EmbeddingBagCollection(EmbeddingBagCollectionInterface, ModuleNoCopyMixin):
    """
    EmbeddingBagCollection represents a collection of pooled embeddings (EmbeddingBags).
    This EmbeddingBagCollection is quantized for lower precision. It relies on fbgemm quantized ops and provides
    table batching.

    It processes sparse data in the form of KeyedJaggedTensor
    with values of the form [F X B X L]
    F: features (keys)
    B: batch size
    L: Length of sparse features (jagged)

    and outputs a KeyedTensor with values of the form [B * (F * D)]
    where
    F: features (keys)
    D: each feature's (key's) embedding dimension
    B: batch size

    Args:
        table_name_to_quantized_weights (Dict[str, Tuple[Tensor, Tensor]]): map of tables to quantized weights
        embedding_configs (List[EmbeddingBagConfig]): list of embedding tables
        is_weighted: (bool): whether input KeyedJaggedTensor is weighted
        device: (Optional[torch.device]): default compute device

    Call Args:
        features: KeyedJaggedTensor,

    Returns:
        KeyedTensor

    Example::

        table_0 = EmbeddingBagConfig(
            name="t1", embedding_dim=3, num_embeddings=10, feature_names=["f1"]
        )
        table_1 = EmbeddingBagConfig(
            name="t2", embedding_dim=4, num_embeddings=10, feature_names=["f2"]
        )
        ebc = EmbeddingBagCollection(tables=[eb1_config, eb2_config])

        #        0       1        2  <-- batch
        # "f1"   [0,1] None    [2]
        # "f2"   [3]    [4]    [5,6,7]
        #  ^
        # feature
        features = KeyedJaggedTensor(
            keys=["f1", "f2"],
            values=torch.tensor([0, 1, 2, 3, 4, 5, 6, 7]),
            offsets=torch.tensor([0, 2, 2, 3, 4, 5, 8]),
        )

        ebc.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.PlaceholderObserver.with_args(
                dtype=torch.qint8
            ),
            weight=torch.quantization.PlaceholderObserver.with_args(dtype=torch.qint8),
        )

        qebc = QuantEmbeddingBagCollection.from_float(ebc)
        quantized_embeddings = qebc(features)
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        is_weighted: bool,
        device: torch.device,
        output_dtype: torch.dtype = torch.float,
        table_name_to_quantized_weights: Optional[
            Dict[str, Tuple[Tensor, Tensor]]
        ] = None,
        register_tbes: bool = False,
        quant_state_dict_split_scale_bias: bool = False,
        row_alignment: int = DEFAULT_ROW_ALIGNMENT,
    ) -> None:
        super().__init__()
        self._is_weighted = is_weighted
        self._embedding_bag_configs: List[EmbeddingBagConfig] = tables
        self._key_to_tables: Dict[
            Tuple[PoolingType, DataType], List[EmbeddingBagConfig]
        ] = defaultdict(list)
        self._length_per_key: List[int] = []
        # Registering in a List instead of ModuleList because we want don't want them to be auto-registered.
        # Their states will be modified via self.embedding_bags
        self._emb_modules: List[nn.Module] = []
        self._output_dtype = output_dtype
        self._device: torch.device = device
        self._table_name_to_quantized_weights: Optional[
            Dict[str, Tuple[Tensor, Tensor]]
        ] = None
        self.row_alignment = row_alignment

        table_names = set()
        for table in self._embedding_bag_configs:
            if table.name in table_names:
                raise ValueError(f"Duplicate table name {table.name}")
            table_names.add(table.name)
            self._length_per_key.extend(
                [table.embedding_dim] * len(table.feature_names)
            )
            key = (table.pooling, table.data_type)
            self._key_to_tables[key].append(table)

        self._sum_length_per_key: int = sum(self._length_per_key)

        location = (
            EmbeddingLocation.HOST if device.type == "cpu" else EmbeddingLocation.DEVICE
        )

        for key, emb_configs in self._key_to_tables.items():
            (pooling, data_type) = key
            embedding_specs = []
            weight_lists: Optional[
                List[Tuple[torch.Tensor, Optional[torch.Tensor]]]
            ] = ([] if table_name_to_quantized_weights else None)
            feature_table_map: List[int] = []
            index_remappings: List[Optional[torch.Tensor]] = []
            index_remappings_non_none_count: int = 0

            for idx, table in enumerate(emb_configs):
                embedding_specs.append(
                    (
                        table.name,
                        table.num_embeddings,
                        table.embedding_dim,
                        data_type_to_sparse_type(data_type),
                        location,
                    )
                )
                if table_name_to_quantized_weights:
                    none_throws(weight_lists).append(
                        table_name_to_quantized_weights[table.name]
                    )
                feature_table_map.extend([idx] * table.num_features())
                index_remappings.append(table.pruning_indices_remapping)
                if table.pruning_indices_remapping is not None:
                    index_remappings_non_none_count += 1

            emb_module = IntNBitTableBatchedEmbeddingBagsCodegen(
                embedding_specs=embedding_specs,
                pooling_mode=pooling_type_to_pooling_mode(pooling),
                weight_lists=weight_lists,
                device=device,
                output_dtype=data_type_to_sparse_type(dtype_to_data_type(output_dtype)),
                row_alignment=row_alignment,
                feature_table_map=feature_table_map,
                # pyre-ignore
                index_remapping=index_remappings
                if index_remappings_non_none_count > 0
                else None,
            )
            if weight_lists is None:
                emb_module.initialize_weights()
            self._emb_modules.append(emb_module)

        self._embedding_names: List[str] = list(
            itertools.chain(*get_embedding_names_by_table(self._embedding_bag_configs))
        )
        # We map over the parameters from FBGEMM backed kernels to the canonical nn.EmbeddingBag
        # representation. This provides consistency between this class and the EmbeddingBagCollection
        # nn.Module API calls (state_dict, named_modules, etc)
        self.embedding_bags: nn.ModuleDict = nn.ModuleDict()
        for (_key, tables), emb_module in zip(
            self._key_to_tables.items(), self._emb_modules
        ):
            for embedding_config, (weight, qscale, qbias) in zip(
                tables,
                emb_module.split_embedding_weights_with_scale_bias(
                    split_scale_bias_mode=2 if quant_state_dict_split_scale_bias else 0
                ),
            ):
                self.embedding_bags[embedding_config.name] = torch.nn.Module()
                # register as a buffer so it's exposed in state_dict.
                # TODO: register as param instead of buffer
                # however, since this is only needed for inference, we do not need to expose it as part of parameters.
                # Additionally, we cannot expose uint8 weights as parameters due to autograd restrictions.

                if embedding_config.data_type == DataType.INT4:
                    weight = UInt4Tensor(weight)
                elif embedding_config.data_type == DataType.INT2:
                    weight = UInt2Tensor(weight)

                self.embedding_bags[embedding_config.name].register_buffer(
                    "weight", weight
                )
                if quant_state_dict_split_scale_bias:
                    self.embedding_bags[embedding_config.name].register_buffer(
                        "weight_qscale", qscale
                    )
                    self.embedding_bags[embedding_config.name].register_buffer(
                        "weight_qbias", qbias
                    )

                if embedding_config.pruning_indices_remapping is not None:
                    self.embedding_bags[embedding_config.name].register_buffer(
                        "index_remappings_array",
                        emb_module.index_remappings_array,
                    )

        setattr(
            self,
            MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS,
            quant_state_dict_split_scale_bias,
        )
        setattr(self, MODULE_ATTR_REGISTER_TBES_BOOL, register_tbes)
        self.register_tbes = register_tbes
        if register_tbes:
            self.tbes: torch.nn.ModuleList = torch.nn.ModuleList(self._emb_modules)

    def forward(
        self,
        features: KeyedJaggedTensor,
    ) -> KeyedTensor:
        """
        Args:
            features (KeyedJaggedTensor): KJT of form [F X B X L].

        Returns:
            KeyedTensor
        """

        feature_dict = features.to_dict()
        embeddings = []

        # TODO ideally we can accept KJTs with any feature order. However, this will require an order check + permute, which will break torch.script.
        # Once torchsccript is no longer a requirement, we should revisit this.

        for emb_op, (_key, tables) in zip(
            self._emb_modules, self._key_to_tables.items()
        ):
            indices = []
            lengths = []
            offsets = []
            weights = []

            for table in tables:
                for feature in table.feature_names:
                    f = feature_dict[feature]
                    indices.append(f.values())
                    lengths.append(f.lengths())
                    if self._is_weighted:
                        weights.append(f.weights())

            indices = torch.cat(indices)
            lengths = torch.cat(lengths)

            offsets = torch.ops.fbgemm.asynchronous_complete_cumsum(lengths)
            if self._is_weighted:
                weights = torch.cat(weights)

            embeddings.append(
                # Syntax for FX to generate call_module instead of call_function to keep TBE copied unchanged to fx.GraphModule, can be done only for registered module
                emb_op(
                    indices=indices,
                    offsets=offsets,
                    per_sample_weights=weights if self._is_weighted else None,
                )
                if self.register_tbes
                else emb_op.forward(
                    indices=indices,
                    offsets=offsets,
                    per_sample_weights=weights if self._is_weighted else None,
                )
            )

        embeddings = torch.stack(embeddings).reshape(-1, self._sum_length_per_key)

        return KeyedTensor(
            keys=self._embedding_names,
            values=embeddings,
            length_per_key=self._length_per_key,
        )

    def _get_name(self) -> str:
        return "QuantizedEmbeddingBagCollection"

    @classmethod
    def from_float(
        cls, module: OriginalEmbeddingBagCollection
    ) -> "EmbeddingBagCollection":
        assert hasattr(
            module, "qconfig"
        ), "EmbeddingBagCollection input float module must have qconfig defined"
        embedding_bag_configs = copy.deepcopy(module.embedding_bag_configs())
        _update_embedding_configs(
            cast(List[BaseEmbeddingConfig], embedding_bag_configs),
            module.qconfig,
        )
        pruning_dict: Dict[str, torch.Tensor] = getattr(
            module, MODULE_ATTR_EMB_CONFIG_NAME_TO_PRUNING_INDICES_REMAPPING_DICT, {}
        )

        for config in embedding_bag_configs:
            if config.name in pruning_dict:
                pruning_indices_remapping = pruning_dict[config.name]
                config.num_embeddings = pruned_num_embeddings(pruning_indices_remapping)
                config.pruning_indices_remapping = pruning_indices_remapping

        table_name_to_quantized_weights: Dict[str, Tuple[Tensor, Tensor]] = {}
        device = quantize_state_dict(
            module,
            table_name_to_quantized_weights,
            {table.name: table.data_type for table in embedding_bag_configs},
            pruning_dict,
        )
        return cls(
            embedding_bag_configs,
            module.is_weighted(),
            device=device,
            output_dtype=module.qconfig.activation().dtype,
            table_name_to_quantized_weights=table_name_to_quantized_weights,
            register_tbes=getattr(module, MODULE_ATTR_REGISTER_TBES_BOOL, False),
            quant_state_dict_split_scale_bias=getattr(
                module, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, False
            ),
            row_alignment=getattr(
                module, MODULE_ATTR_ROW_ALIGNMENT_INT, DEFAULT_ROW_ALIGNMENT
            ),
        )

    def embedding_bag_configs(
        self,
    ) -> List[EmbeddingBagConfig]:
        return self._embedding_bag_configs

    def is_weighted(self) -> bool:
        return self._is_weighted

    def output_dtype(self) -> torch.dtype:
        return self._output_dtype

    @property
    def device(self) -> torch.device:
        return self._device


class FeatureProcessedEmbeddingBagCollection(EmbeddingBagCollection):
    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        is_weighted: bool,
        device: torch.device,
        output_dtype: torch.dtype = torch.float,
        table_name_to_quantized_weights: Optional[
            Dict[str, Tuple[Tensor, Tensor]]
        ] = None,
        register_tbes: bool = False,
        quant_state_dict_split_scale_bias: bool = False,
        row_alignment: int = DEFAULT_ROW_ALIGNMENT,
        # feature processor is Optional only for the sake of the last position in constructor
        # Enforcing it to be non-None, for None case EmbeddingBagCollection must be used.
        feature_processor: Optional[FeatureProcessorsCollection] = None,
    ) -> None:
        super().__init__(
            tables,
            is_weighted,
            device,
            output_dtype,
            table_name_to_quantized_weights,
            register_tbes,
            quant_state_dict_split_scale_bias,
            row_alignment,
        )
        assert (
            feature_processor is not None
        ), "Use EmbeddingBagCollection for no feature_processor"
        self.feature_processor: FeatureProcessorsCollection = feature_processor

    def forward(
        self,
        features: KeyedJaggedTensor,
    ) -> KeyedTensor:
        features = self.feature_processor(features)
        return super().forward(features)

    def _get_name(self) -> str:
        return "QuantFeatureProcessedEmbeddingBagCollection"

    @classmethod
    # pyre-ignore
    def from_float(
        cls, module: OriginalFeatureProcessedEmbeddingBagCollection
    ) -> "FeatureProcessedEmbeddingBagCollection":
        fp_ebc = module
        ebc = module._embedding_bag_collection
        qconfig = module.qconfig
        assert hasattr(
            module, "qconfig"
        ), "FeatureProcessedEmbeddingBagCollection input float module must have qconfig defined"

        embedding_bag_configs = copy.deepcopy(ebc.embedding_bag_configs())
        _update_embedding_configs(
            cast(List[BaseEmbeddingConfig], embedding_bag_configs),
            qconfig,
        )
        pruning_dict: Dict[str, torch.Tensor] = getattr(
            module, MODULE_ATTR_EMB_CONFIG_NAME_TO_PRUNING_INDICES_REMAPPING_DICT, {}
        )

        for config in embedding_bag_configs:
            if config.name in pruning_dict:
                pruning_indices_remapping = pruning_dict[config.name]
                config.num_embeddings = pruned_num_embeddings(pruning_indices_remapping)
                config.pruning_indices_remapping = pruning_indices_remapping

        table_name_to_quantized_weights: Dict[str, Tuple[Tensor, Tensor]] = {}
        device = quantize_state_dict(
            ebc,
            table_name_to_quantized_weights,
            {table.name: table.data_type for table in embedding_bag_configs},
            pruning_dict,
        )
        return cls(
            embedding_bag_configs,
            ebc.is_weighted(),
            device=device,
            output_dtype=qconfig.activation().dtype,
            table_name_to_quantized_weights=table_name_to_quantized_weights,
            register_tbes=getattr(module, MODULE_ATTR_REGISTER_TBES_BOOL, False),
            quant_state_dict_split_scale_bias=getattr(
                ebc, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, False
            ),
            row_alignment=getattr(
                ebc, MODULE_ATTR_ROW_ALIGNMENT_INT, DEFAULT_ROW_ALIGNMENT
            ),
            # pyre-ignore
            feature_processor=fp_ebc._feature_processors,
        )


class EmbeddingCollection(EmbeddingCollectionInterface, ModuleNoCopyMixin):
    """
    EmbeddingCollection represents a collection of non-pooled embeddings.

    It processes sparse data in the form of `KeyedJaggedTensor` of the form [F X B X L]
    where:

    * F: features (keys)
    * B: batch size
    * L: length of sparse features (variable)

    and outputs `Dict[feature (key), JaggedTensor]`.
    Each `JaggedTensor` contains values of the form (B * L) X D
    where:

    * B: batch size
    * L: length of sparse features (jagged)
    * D: each feature's (key's) embedding dimension and lengths are of the form L

    Args:
        tables (List[EmbeddingConfig]): list of embedding tables.
        device (Optional[torch.device]): default compute device.
        need_indices (bool): if we need to pass indices to the final lookup result dict

    Example::

        e1_config = EmbeddingConfig(
            name="t1", embedding_dim=3, num_embeddings=10, feature_names=["f1"]
        )
        e2_config = EmbeddingConfig(
            name="t2", embedding_dim=3, num_embeddings=10, feature_names=["f2"]
        )

        ec = EmbeddingCollection(tables=[e1_config, e2_config])

        #     0       1        2  <-- batch
        # 0   [0,1] None    [2]
        # 1   [3]    [4]    [5,6,7]
        # ^
        # feature

        features = KeyedJaggedTensor.from_offsets_sync(
            keys=["f1", "f2"],
            values=torch.tensor([0, 1, 2, 3, 4, 5, 6, 7]),
            offsets=torch.tensor([0, 2, 2, 3, 4, 5, 8]),
        )
        feature_embeddings = ec(features)
        print(feature_embeddings['f2'].values())
        tensor([[-0.2050,  0.5478,  0.6054],
        [ 0.7352,  0.3210, -3.0399],
        [ 0.1279, -0.1756, -0.4130],
        [ 0.7519, -0.4341, -0.0499],
        [ 0.9329, -1.0697, -0.8095]], grad_fn=<EmbeddingBackward>)
    """

    def __init__(  # noqa C901
        self,
        tables: List[EmbeddingConfig],
        device: torch.device,
        need_indices: bool = False,
        output_dtype: torch.dtype = torch.float,
        table_name_to_quantized_weights: Optional[
            Dict[str, Tuple[Tensor, Tensor]]
        ] = None,
        register_tbes: bool = False,
        quant_state_dict_split_scale_bias: bool = False,
        row_alignment: int = DEFAULT_ROW_ALIGNMENT,
    ) -> None:
        super().__init__()
        self._emb_modules: List[IntNBitTableBatchedEmbeddingBagsCodegen] = []
        self.embeddings: nn.ModuleDict = nn.ModuleDict()

        self._embedding_configs = tables
        self._embedding_dim: int = -1
        self._need_indices: bool = need_indices
        self._output_dtype = output_dtype
        self._device = device
        self.row_alignment = row_alignment

        table_names = set()
        for config in tables:
            if config.name in table_names:
                raise ValueError(f"Duplicate table name {config.name}")
            table_names.add(config.name)
            self._embedding_dim = (
                config.embedding_dim if self._embedding_dim < 0 else self._embedding_dim
            )
            if self._embedding_dim != config.embedding_dim:
                raise ValueError(
                    "All tables in a EmbeddingCollection are required to have same embedding dimension."
                    + f" Violating case: {config.name}'s embedding_dim {config.embedding_dim} !="
                    + f" {self._embedding_dim}"
                )
            weight_lists: Optional[
                List[Tuple[torch.Tensor, Optional[torch.Tensor]]]
            ] = ([] if table_name_to_quantized_weights else None)
            if table_name_to_quantized_weights:
                none_throws(weight_lists).append(
                    table_name_to_quantized_weights[config.name]
                )
            emb_module = IntNBitTableBatchedEmbeddingBagsCodegen(
                embedding_specs=[
                    (
                        config.name,
                        config.num_embeddings,
                        config.embedding_dim,
                        data_type_to_sparse_type(config.data_type),
                        EmbeddingLocation.HOST
                        if device.type == "cpu"
                        else EmbeddingLocation.DEVICE,
                    )
                ],
                pooling_mode=PoolingMode.SUM,
                weight_lists=weight_lists,
                device=device,
                output_dtype=data_type_to_sparse_type(dtype_to_data_type(output_dtype)),
                row_alignment=row_alignment,
            )
            if weight_lists is None:
                emb_module.initialize_weights()

            self._emb_modules.append(emb_module)
            self.embeddings[config.name] = torch.nn.Module()
            # register as a buffer so it's exposed in state_dict.
            # TODO: register as param instead of buffer
            # however, since this is only needed for inference, we do not need to expose it as part of parameters.
            # Additionally, we cannot expose uint8 weights as parameters due to autograd restrictions.
            weights_list = emb_module.split_embedding_weights_with_scale_bias(
                split_scale_bias_mode=2 if quant_state_dict_split_scale_bias else 0
            )

            weight = weights_list[0][0]
            if config.data_type == DataType.INT4:
                weight = UInt4Tensor(weight)
            elif config.data_type == DataType.INT2:
                weight = UInt2Tensor(weight)

            self.embeddings[config.name].register_buffer("weight", weight)
            if quant_state_dict_split_scale_bias:
                self.embeddings[config.name].register_buffer(
                    "weight_qscale", weights_list[0][1]
                )
                self.embeddings[config.name].register_buffer(
                    "weight_qbias", weights_list[0][2]
                )

            if not config.feature_names:
                config.feature_names = [config.name]

        self._embedding_names_by_table: List[List[str]] = get_embedding_names_by_table(
            tables
        )
        setattr(
            self,
            MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS,
            quant_state_dict_split_scale_bias,
        )
        setattr(self, MODULE_ATTR_REGISTER_TBES_BOOL, register_tbes)
        self.register_tbes = register_tbes
        if register_tbes:
            self.tbes: torch.nn.ModuleList = torch.nn.ModuleList(self._emb_modules)

    def forward(
        self,
        features: KeyedJaggedTensor,
    ) -> Dict[str, JaggedTensor]:
        """
        Args:
            features (KeyedJaggedTensor): KJT of form [F X B X L].

        Returns:
            Dict[str, JaggedTensor]
        """

        feature_embeddings: Dict[str, JaggedTensor] = {}
        jt_dict: Dict[str, JaggedTensor] = features.to_dict()
        for config, embedding_names, emb_module in zip(
            self._embedding_configs,
            self._embedding_names_by_table,
            self._emb_modules,
        ):
            for feature_name, embedding_name in zip(
                config.feature_names, embedding_names
            ):
                f = jt_dict[feature_name]
                values = f.values()
                offsets = set_fake_stbe_offsets(values)
                # Syntax for FX to generate call_module instead of call_function to keep TBE copied unchanged to fx.GraphModule, can be done only for registered module
                lookup = (
                    emb_module(indices=values, offsets=offsets)
                    if self.register_tbes
                    else emb_module.forward(indices=values, offsets=offsets)
                )
                feature_embeddings[embedding_name] = JaggedTensor(
                    values=lookup,
                    lengths=f.lengths(),
                    weights=f.values() if self.need_indices else None,
                )
        return feature_embeddings

    @classmethod
    def from_float(cls, module: OriginalEmbeddingCollection) -> "EmbeddingCollection":
        assert hasattr(
            module, "qconfig"
        ), "EmbeddingCollection input float module must have qconfig defined"
        embedding_configs = copy.deepcopy(module.embedding_configs())
        _update_embedding_configs(
            cast(List[BaseEmbeddingConfig], embedding_configs), module.qconfig
        )
        table_name_to_quantized_weights: Dict[str, Tuple[Tensor, Tensor]] = {}
        device = quantize_state_dict(
            module,
            table_name_to_quantized_weights,
            {table.name: table.data_type for table in embedding_configs},
        )
        return cls(
            embedding_configs,
            device=device,
            need_indices=module.need_indices(),
            output_dtype=module.qconfig.activation().dtype,
            table_name_to_quantized_weights=table_name_to_quantized_weights,
            register_tbes=getattr(module, MODULE_ATTR_REGISTER_TBES_BOOL, False),
            quant_state_dict_split_scale_bias=getattr(
                module, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, False
            ),
            row_alignment=getattr(
                module, MODULE_ATTR_ROW_ALIGNMENT_INT, DEFAULT_ROW_ALIGNMENT
            ),
        )

    def _get_name(self) -> str:
        return "QuantizedEmbeddingCollection"

    def need_indices(self) -> bool:
        return self._need_indices

    def embedding_dim(self) -> int:
        return self._embedding_dim

    def embedding_configs(self) -> List[EmbeddingConfig]:
        return self._embedding_configs

    def embedding_names_by_table(self) -> List[List[str]]:
        return self._embedding_names_by_table

    def output_dtype(self) -> torch.dtype:
        return self._output_dtype

    @property
    def device(self) -> torch.device:
        return self._device
