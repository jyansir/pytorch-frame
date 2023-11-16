import copy
import functools
import os.path as osp
from abc import ABC
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import torch
from torch import Tensor

import torch_frame
from torch_frame.config import TextEmbedderConfig, TextTokenizerConfig
from torch_frame.data import TensorFrame
from torch_frame.data.mapper import (
    CategoricalTensorMapper,
    MultiCategoricalTensorMapper,
    NumericalSequenceTensorMapper,
    NumericalTensorMapper,
    TensorMapper,
    TextEmbeddingTensorMapper,
    TextTokenizationTensorMapper,
    TimestampTensorMapper,
)
from torch_frame.data.multi_nested_tensor import MultiNestedTensor
from torch_frame.data.stats import StatType, compute_col_stats
from torch_frame.typing import (
    ColumnSelectType,
    DataFrame,
    IndexSelectType,
    TaskType,
    TensorData,
)
from torch_frame.utils.split import SPLIT_TO_NUM


def requires_pre_materialization(func):
    @functools.wraps(func)
    def _requires_pre_materialization(self, *args, **kwargs):
        if self.is_materialized:
            raise RuntimeError(
                f"'{self}' cannot be modified via '{func.__name__}' post "
                f"materialization")
        return func(self, *args, **kwargs)

    return _requires_pre_materialization


def requires_post_materialization(func):
    @functools.wraps(func)
    def _requires_post_materialization(self, *args, **kwargs):
        if not self.is_materialized:
            raise RuntimeError(
                f"'{func.__name__}' requires a materialized dataset. Please "
                f"call `dataset.materialize(...)` first.")
        return func(self, *args, **kwargs)

    return _requires_post_materialization


def canonicalize_col_to_pattern(col_to_pattern: Union[Optional[str],
                                                      Dict[str, str]],
                                columns: List[str]) -> Dict[str, str]:
    r"""Canonicalize :obj:`col_to_pattern` into a dictionary format.

    Args:
        col_to_pattern (Union[str, Dict[str, str]]): A dictionary or a string
            specifying the separator/pattern for the multi-categorical
            or timestamp columns. If a string is specified, then the same
            separator/format will be used throughout all the multi-categorical
            or timestamp columns. If a dictionary is given, we use a separator
            specified for each column. (default: :obj:`,`)
        columns (List[str]): A list of multi-categorical or timestamp columns.

    Returns:
        Dict[str, str]: :obj:`col_to_pattern` in a dictionary format, mapping
            multi-categorical or timestamp columns into their specified
            separators.
    """
    if col_to_pattern is None or isinstance(col_to_pattern, str):
        pattern = col_to_pattern
        col_to_pattern = {}
        for col in columns:
            col_to_pattern[col] = pattern
    else:
        missing_cols = set(columns) - set(col_to_pattern.keys())
        if len(missing_cols) > 0:
            raise ValueError(
                f"col_to_sep needs to specify separators for all "
                f"multi-categorical columns, but the separators for the "
                f"following columns are missing: {list(missing_cols)}.")
    return col_to_pattern


class DataFrameToTensorFrameConverter:
    r"""A data frame to :class:`TensorFrame` converter.

    Args:
        col_to_stype (Dict[str, :class:`torch_frame.stype`]):
            A dictionary that maps each column in the data frame to a
            semantic type.
        col_stats (Dict[str, Dict[StatType, Any]]): A dictionary that maps
            column name into stats. Available as :obj:`dataset.col_stats`.
        target_col (str, optional): The column used as target.
            (default: :obj:`None`)
        col_to_sep (Union[str, Dict[str, str]]): A dictionary or a string
            specifying the separator/delimiter for the multi-categorical
            columns. If a string is specified, then the same separator will
            be used throughout all the multi-categorical columns. If a
            dictionary is given, we use a separator specified for each
            column. (default: :obj:`,`)
        text_embedder_cfg
            (:class:`torch_frame.config.TextEmbedderConfig`, optional):
            A text embedder config specifying :obj:`text_embedder` that
            maps sentences into :class:`torch.nn.Embeddings` and
            :obj:`batch_size` that specifies the mini-batch size for
            :obj:`text_embedder`. (default: :obj:`None`)
        text_tokenizer_cfg
            (:class:`torch_frame.config.TextTokenizerConfig`, optional):
            A text tokenizer config specifying :obj:`text_tokenizer` that
            maps sentences into a list of dictionary of tensors. Each
            element in the list corresponds to each sentence, keys are
            input arguments to the model such as :obj:`input_ids`, and
            values are tensors such as tokens.
            :obj:`batch_size` specifies the mini-batch size for
            :obj:`text_tokenizer`. (default: :obj:`None`)
        col_to_time_format (Union[str, Dict[str, str]], optional): A
            dictionary or a string specifying the format for the timestamp
            columns. See `strfttime documentation
            <https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior>`_
            for more information on formats. If a string is specified,
            then the same format will be used throughout all the timestamp
            columns. If a dictionary is given, we use a different format
            specified for each column. If not specified, pandas's internal
            to_datetime function will be used to auto parse time columns.
            (default: None)
    """
    def __init__(
        self,
        col_to_stype: Dict[str, torch_frame.stype],
        col_stats: Dict[str, Dict[StatType, Any]],
        target_col: Optional[str] = None,
        col_to_sep: Union[str, Dict[str, str]] = ",",
        text_embedder_cfg: Optional[TextEmbedderConfig] = None,
        text_tokenizer_cfg: Optional[TextTokenizerConfig] = None,
        col_to_time_format: Optional[Union[str, Dict[str, str]]] = None,
    ):
        self.col_to_stype = col_to_stype
        self.col_stats = col_stats
        self.target_col = target_col
        self.text_embedder_cfg = text_embedder_cfg
        self.text_tokenizer_cfg = text_tokenizer_cfg

        # Pre-compute a canonical `col_names_dict` for tensor frame.
        self._col_names_dict: Dict[torch_frame.stype, List[str]] = {}
        for col, stype in self.col_to_stype.items():
            if col != self.target_col:
                if stype not in self._col_names_dict:
                    self._col_names_dict[stype] = [col]
                else:
                    self._col_names_dict[stype].append(col)
        for stype in self._col_names_dict.keys():
            # in-place sorting of col_names for each stype
            self._col_names_dict[stype].sort()

        self.col_to_sep = canonicalize_col_to_pattern(
            col_to_sep,
            self.col_names_dict.get(torch_frame.multicategorical, []),
        )

        self.col_to_time_format = canonicalize_col_to_pattern(
            col_to_time_format,
            self.col_names_dict.get(torch_frame.timestamp, []),
        )

        if (torch_frame.text_embedded
                in self.col_names_dict) and (self.text_embedder_cfg is None):
            raise ValueError("`text_embedder_cfg` needs to be specified when "
                             "stype.text_embedded column exists.")

        if (torch_frame.text_tokenized
                in self.col_names_dict) and (self.text_tokenizer_cfg is None):
            raise ValueError("`text_tokenizer_cfg` needs to be specified when "
                             "stype.text_tokenized column exists.")

    @property
    def col_names_dict(self) -> Dict[torch_frame.stype, List[str]]:
        return self._col_names_dict

    def _get_mapper(self, col: str) -> TensorMapper:
        r"""Get TensorMapper given a column name."""
        stype = self.col_to_stype[col]
        if stype == torch_frame.numerical:
            return NumericalTensorMapper()
        elif stype == torch_frame.categorical:
            index, _ = self.col_stats[col][StatType.COUNT]
            return CategoricalTensorMapper(index)
        elif stype == torch_frame.multicategorical:
            index, _ = self.col_stats[col][StatType.MULTI_COUNT]
            return MultiCategoricalTensorMapper(index,
                                                sep=self.col_to_sep[col])
        elif stype == torch_frame.timestamp:
            return TimestampTensorMapper(
                format=self.col_to_time_format.get(col, None))
        elif stype == torch_frame.text_embedded:
            return TextEmbeddingTensorMapper(
                self.text_embedder_cfg.text_embedder,
                self.text_embedder_cfg.batch_size,
            )
        elif stype == torch_frame.text_tokenized:
            return TextTokenizationTensorMapper(
                self.text_tokenizer_cfg.text_tokenizer,
                self.text_tokenizer_cfg.batch_size,
            )
        elif stype == torch_frame.sequence_numerical:
            return NumericalSequenceTensorMapper()
        else:
            raise NotImplementedError(f"Unable to process the semantic "
                                      f"type '{stype.value}'")

    def __call__(
        self,
        df: DataFrame,
        device: Optional[torch.device] = None,
    ) -> TensorFrame:
        r"""Convert a given :class:`DataFrame` object into :class:`TensorFrame`
        object.
        """
        xs_dict: Dict[torch_frame.stype, List[TensorData]] = defaultdict(list)

        for stype, col_names in self.col_names_dict.items():
            for col in col_names:
                out = self._get_mapper(col).forward(df[col], device=device)
                xs_dict[stype].append(out)

        feat_dict = {}
        for stype, xs in xs_dict.items():
            if stype.use_multi_nested_tensor:
                feat_dict[stype] = MultiNestedTensor.cat(xs, dim=1)
            elif stype.use_dict_multi_nested_tensor:
                feat_dict[stype]: Dict[str, MultiNestedTensor] = {}
                for key in xs[0].keys():
                    feat_dict[stype][key] = MultiNestedTensor.cat(
                        [x[key] for x in xs], dim=1)
            else:
                feat_dict[stype] = torch.stack(xs, dim=1)

        y: Optional[Tensor] = None
        if self.target_col is not None:
            y = self._get_mapper(self.target_col).forward(
                df[self.target_col], device=device)

        return TensorFrame(feat_dict, self.col_names_dict, y)


class Dataset(ABC):
    r"""A base class for creating tabular datasets.

    Args:
        df (DataFrame): The tabular data frame.
        col_to_stype (Dict[str, torch_frame.stype]): A dictionary that maps
            each column in the data frame to a semantic type.
        target_col (str, optional): The column used as target.
            (default: :obj:`None`)
        split_col (str, optional): The column that stores the pre-defined split
            information. The column should only contain :obj:`0`, :obj:`1`, or
            :obj:`2`. (default: :obj:`None`).
        col_to_sep (Union[str, Dict[str, str]]): A dictionary or a string
            specifying the separator/delimiter for the multi-categorical
            columns. If a string is specified, then the same separator will
            be used throughout all the multi-categorical columns. If a
            dictionary is given, we use a separator specified for each
            column. (default: :obj:`,`)
            (default: :obj:`,`)
        text_embedder_cfg (TextEmbedderConfig, optional): A text embedder
            configuration that specifies the text embedder to map text columns
            into :pytorch:`PyTorch` embeddings. (default: :obj:`None`)
        text_tokenizer_cfg (TextTokenizerConfig, optional):
            A text tokenizer config specifying :obj:`text_tokenizer` that
            maps sentences into a list of dictionary of tensors. Each
            element in the list corresponds to each sentence, keys are
            input arguments to the model such as :obj:`input_ids`, and
            values are tensors such as tokens.
            :obj:`batch_size` specifies the mini-batch size for
            :obj:`text_tokenizer`. (default: :obj:`None`)
        col_to_time_format (Union[str, Dict[str, str]], optional): A
            dictionary or a string specifying the format for the timestamp
            columns. See `strfttime documentation
            <https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior>`_
            for more information on formats. If a string is specified,
            then the same format will be used throughout all the timestamp
            columns. If a dictionary is given, we use a different format
            specified for each column. If not specified, pandas's internal
            to_datetime function will be used to auto parse time columns.
            (default: None)
    """
    def __init__(
        self,
        df: DataFrame,
        col_to_stype: Dict[str, torch_frame.stype],
        target_col: Optional[str] = None,
        split_col: Optional[str] = None,
        col_to_sep: Union[str, Dict[str, str]] = ",",
        text_embedder_cfg: Optional[TextEmbedderConfig] = None,
        text_tokenizer_cfg: Optional[TextTokenizerConfig] = None,
        col_to_time_format: Optional[Union[str, Dict[str, str]]] = None,
    ):
        self.df = df
        self.target_col = target_col

        if split_col is not None:
            if split_col not in df.columns:
                raise ValueError(
                    f"Given split_col ({split_col}) does not match columns of "
                    f"the given df.")
            if split_col in col_to_stype:
                raise ValueError(
                    f"col_to_stype should not contain the split_col "
                    f"({col_to_stype}).")
            if not set(df[split_col]).issubset(set(SPLIT_TO_NUM.values())):
                raise ValueError(
                    f"split_col must only contain {set(SPLIT_TO_NUM.values())}"
                )
        self.split_col = split_col
        self.col_to_stype = col_to_stype

        cols = self.feat_cols + ([] if target_col is None else [target_col])
        missing_cols = set(cols) - set(df.columns)
        if len(missing_cols) > 0:
            raise ValueError(f"The column(s) '{missing_cols}' are specified "
                             f"but missing in the data frame")

        if (target_col is not None and self.col_to_stype[target_col]
                == torch_frame.multicategorical):
            raise ValueError(
                "Multilabel classification task is not yet supported.")

        self.text_embedder_cfg = text_embedder_cfg
        self.text_tokenizer_cfg = text_tokenizer_cfg
        self.col_to_sep = canonicalize_col_to_pattern(
            col_to_sep,
            [
                col for col, stype in self.col_to_stype.items()
                if stype == torch_frame.multicategorical
            ],
        )
        self.col_to_time_format = canonicalize_col_to_pattern(
            col_to_time_format,
            [
                col for col, stype in self.col_to_stype.items()
                if stype == torch_frame.timestamp
            ],
        )
        self._is_materialized: bool = False
        self._col_stats: Dict[str, Dict[StatType, Any]] = {}
        self._tensor_frame: Optional[TensorFrame] = None

    @staticmethod
    def download_url(
        url: str,
        root: str,
        filename: Optional[str] = None,
        *,
        log: bool = True,
    ) -> str:
        r"""Downloads the content of :obj:`url` to the specified folder
        :obj:`root`.

        Args:
            url (str): The URL.
            root (str): The root folder.
            filename (str, optional): If set, will rename the downloaded file.
                (default: :obj:`None`)
            log (bool, optional): If :obj:`False`, will not print anything to
                the console. (default: :obj:`True`)
        """
        return torch_frame.data.download_url(url, root, filename, log=log)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, index: IndexSelectType) -> "Dataset":
        is_col_select = isinstance(index, str)
        is_col_select |= (isinstance(index, (list, tuple)) and len(index) > 0
                          and isinstance(index[0], str))

        if is_col_select:
            return self.col_select(index)

        return self.index_select(index)

    @property
    def feat_cols(self) -> List[str]:
        r"""The input feature columns of the dataset."""
        cols = list(self.col_to_stype.keys())
        if self.target_col is not None:
            cols.remove(self.target_col)
        return cols

    @property
    def task_type(self) -> TaskType:
        r"""The task type of the dataset."""
        assert self.target_col is not None
        if self.col_to_stype[self.target_col] == torch_frame.categorical:
            if self.num_classes == 2:
                return TaskType.BINARY_CLASSIFICATION
            else:
                return TaskType.MULTICLASS_CLASSIFICATION
        elif self.col_to_stype[self.target_col] == torch_frame.numerical:
            return TaskType.REGRESSION
        else:
            raise ValueError("Task type cannot be inferred.")

    @property
    def num_rows(self):
        r"""The number of rows of the dataset."""
        return len(self.df)

    @property
    @requires_post_materialization
    def num_classes(self) -> int:
        if StatType.COUNT not in self.col_stats[self.target_col]:
            raise ValueError(
                f"num_classes attribute is only supported when the target "
                f"column ({self.target_col}) stats contains StatType.COUNT, "
                f"but only the following target column stats are calculated: "
                f"{list(self.col_stats[self.target_col].keys())}.")
        num_classes = len(self.col_stats[self.target_col][StatType.COUNT][0])
        assert num_classes > 1
        return num_classes

    # Materialization #########################################################

    def materialize(self, device: Optional[torch.device] = None,
                    path: Optional[str] = None) -> "Dataset":
        r"""Materializes the dataset into a tensor representation. From this
        point onwards, the dataset should be treated as read-only.

        Args:
            device (torch.device, optional): Device to load the
                :class:`TensorFrame` object. (default: :obj:`None`)
            path (str, optional): If path is specified and a cached file
                exists, this will try to load the saved the
                :class:`TensorFrame` object and :obj:`col_stats`.
                If :obj:`path` is specified but a cached file does not exist,
                this will perform materialization and then save the
                :class:`TensorFrame object and :obj:`col_stats` to :obj:`path`.
                If :obj:`path` is :obj:`None`, this will materialize the
                dataset without caching. (default: :obj:`None`)
        """
        if self.is_materialized:
            # Materialized without specifying path at first and materialize
            # again by specifying the path
            if path is not None and not osp.isfile(path):
                torch_frame.save(self._tensor_frame, self._col_stats, path)
            return self

        if path is not None and osp.isfile(path):
            # Load tensor_frame and col_stats
            self._tensor_frame, self._col_stats = torch_frame.load(
                path, device)
            # Instantiate the converter
            self._to_tensor_frame_converter = self._get_tensorframe_converter()
            # Mark the dataset has been materialized
            self._is_materialized = True
            return self

        # 1. Fill column statistics:
        for col, stype in self.col_to_stype.items():
            ser = self.df[col]
            self._col_stats[col] = compute_col_stats(
                ser,
                stype,
                sep=self.col_to_sep.get(col, None),
                time_format=self.col_to_time_format.get(col, None),
            )
            # For a target column, sort categories lexicographically such that
            # we do not accidentally swap labels in binary classification
            # tasks.
            if col == self.target_col and stype == torch_frame.categorical:
                index, value = self._col_stats[col][StatType.COUNT]
                if len(index) == 2:
                    ser = pd.Series(index=index, data=value).sort_index()
                    index, value = ser.index.tolist(), ser.values.tolist()
                    self._col_stats[col][StatType.COUNT] = (index, value)

        # 2. Create the `TensorFrame`:
        self._to_tensor_frame_converter = self._get_tensorframe_converter()
        self._tensor_frame = self._to_tensor_frame_converter(self.df, device)

        # 3. Update col stats based on `TensorFrame`:
        self._update_col_stats()

        # 4. Mark the dataset as materialized:
        self._is_materialized = True

        if path is not None:
            # Cache the dataset if user specifies the path
            torch_frame.save(self._tensor_frame, self._col_stats, path)

        return self

    def _get_tensorframe_converter(self) -> DataFrameToTensorFrameConverter:
        return DataFrameToTensorFrameConverter(
            col_to_stype=self.col_to_stype,
            col_stats=self._col_stats,
            target_col=self.target_col,
            col_to_sep=self.col_to_sep,
            text_embedder_cfg=self.text_embedder_cfg,
            text_tokenizer_cfg=self.text_tokenizer_cfg,
            col_to_time_format=self.col_to_time_format,
        )

    def _update_col_stats(self):
        r"""Set :obj:`col_stats` based on :obj:`tensor_frame`."""
        if torch_frame.text_embedded in self._tensor_frame.feat_dict:
            # Text embedding dimensionality is only available after the tensor
            # frame actually gets created, so we compute col_stats here.
            # For now, we set all EMB_DIM to be the same.
            # TODO: Extend this to allow different embedding dimensionalities
            # for different columns.
            emb_dim = self._tensor_frame.feat_dict[
                torch_frame.text_embedded].size(-1)
            for col_name in self._tensor_frame.col_names_dict[
                    torch_frame.text_embedded]:
                self._col_stats[col_name][StatType.EMB_DIM] = emb_dim

    @property
    def is_materialized(self) -> bool:
        r"""Whether the dataset is already materialized."""
        return self._is_materialized

    @property
    @requires_post_materialization
    def tensor_frame(self) -> TensorFrame:
        r"""Returns the :class:`TensorFrame` of the dataset."""
        return self._tensor_frame

    @property
    @requires_post_materialization
    def col_stats(self) -> Dict[str, Dict[StatType, Any]]:
        r"""Returns column-wise dataset statistics."""
        return self._col_stats

    # Indexing ################################################################

    @requires_post_materialization
    def index_select(self, index: IndexSelectType) -> "Dataset":
        r"""Returns a subset of the dataset from specified indices
        :obj:`index`.
        """
        if isinstance(index, int):
            index = [index]

        elif isinstance(index, slice):
            start, stop, step = index.start, index.stop, index.step
            # Allow floating-point slicing, e.g., dataset[:0.9]
            if isinstance(start, float):
                start = round(start * len(self))
            if isinstance(stop, float):
                stop = round(stop * len(self))
            index = slice(start, stop, step)

        dataset = copy.copy(self)

        iloc = index.cpu().numpy() if isinstance(index, Tensor) else index
        dataset.df = self.df.iloc[iloc]

        dataset._tensor_frame = self._tensor_frame[index]

        return dataset

    def shuffle(
        self, return_perm: bool = False
    ) -> Union["Dataset", Tuple["Dataset", Tensor]]:
        r"""Randomly shuffles the rows in the dataset."""
        perm = torch.randperm(len(self))
        dataset = self.index_select(perm)
        return (dataset, perm) if return_perm is True else dataset

    @requires_pre_materialization
    def col_select(self, cols: ColumnSelectType) -> "Dataset":
        r"""Returns a subset of the dataset from specified columns
        :obj:`cols`.
        """
        cols = [cols] if isinstance(cols, str) else cols

        if self.target_col is not None and self.target_col not in cols:
            cols.append(self.target_col)

        dataset = copy.copy(self)

        dataset.df = self.df[cols]
        dataset.col_to_stype = {col: self.col_to_stype[col] for col in cols}

        return dataset

    def get_split(self, split: str) -> "Dataset":
        r"""Returns a subset of the dataset that belongs to a given training
        split (as defined in :obj:`split_col`).

        Args:
            split (str): The split name (either :obj:`"train"`, :obj:`"val"`,
                or :obj:`"test"`.
        """
        if self.split_col is None:
            raise ValueError(
                f"'get_split' is not supported for '{self}' since 'split_col' "
                f"is not specified.")
        if split not in ["train", "val", "test"]:
            raise ValueError(f"The split named '{split}' is not available. "
                             f"Needs to be either 'train', 'val', or 'test'.")
        indices = self.df.index[self.df[self.split_col] ==
                                SPLIT_TO_NUM[split]].tolist()
        return self[indices]

    def split(self) -> Tuple["Dataset", "Dataset", "Dataset"]:
        r"""Splits the dataset into training, validation and test splits."""
        return (
            self.get_split("train"),
            self.get_split("val"),
            self.get_split("test"),
        )

    @property
    @requires_post_materialization
    def convert_to_tensor_frame(self) -> DataFrameToTensorFrameConverter:
        return self._to_tensor_frame_converter
