import abc
import numpy as np
import sys
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union, Iterator

from ray.data.block import BlockAccessor, DataBatch, T
from ray.data.row import TableRow
from ray.util.annotations import PublicAPI
from ray.data._internal.util import _is_tensor_schema

if TYPE_CHECKING:
    import pyarrow
    import tensorflow as tf
    import torch
    from ray.data._internal.torch_iterable_dataset import TorchTensorBatchType
    from ray.data.dataset import TensorFlowTensorBatchType


if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal


@PublicAPI(stability="beta")
class DatasetIterator(abc.ABC):
    """An iterator for reading items from a :class:`~Dataset` or
    :class:`~DatasetPipeline`.

    For Datasets, each iteration call represents a complete read of all items in the
    Dataset. For DatasetPipelines, each iteration call represents one pass (epoch)
    over the base Dataset. Note that for DatasetPipelines, each pass iterates over
    the original Dataset, instead of a window (if ``.window()`` was used).

    If using Ray AIR, each trainer actor should get its own iterator by calling
    :meth:`session.get_dataset_shard("train")
    <ray.air.session.get_dataset_shard>`.

    Examples:
        >>> import ray
        >>> ds = ray.data.range(5)
        >>> ds
        Dataset(num_blocks=5, num_rows=5, schema=<class 'int'>)
        >>> ds.iterator()
        DatasetIterator(Dataset(num_blocks=5, num_rows=5, schema=<class 'int'>))
        >>> ds = ds.repeat(); ds
        DatasetPipeline(num_windows=inf, num_stages=2)
        >>> ds.iterator()
        DatasetIterator(DatasetPipeline(num_windows=inf, num_stages=2))

    .. tip::
        For debugging purposes, use
        :meth:`~ray.air.util.check_ingest.make_local_dataset_iterator` to create a
        local `DatasetIterator` from a :class:`~ray.data.Dataset`, a
        :class:`~ray.data.Preprocessor`, and a :class:`~ray.air.DatasetConfig`.
    """

    @abc.abstractmethod
    def iter_batches(
        self,
        *,
        prefetch_blocks: int = 0,
        batch_size: int = 256,
        batch_format: Literal["default", "numpy", "pandas"] = "default",
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
        _collate_fn: Optional[Callable[[DataBatch], Any]] = None,
    ) -> Iterator[DataBatch]:
        """Return a local batched iterator over the dataset.

        Examples:
            >>> import ray
            >>> for batch in ray.data.range(
            ...     1000000
            ... ).iterator().iter_batches(): # doctest: +SKIP
            ...     print(batch) # doctest: +SKIP

        Time complexity: O(1)

        Args:
            prefetch_blocks: The number of blocks to prefetch ahead of the
                current block during the scan.
            batch_size: The number of rows in each batch, or None to use entire blocks
                as batches (blocks may contain different number of rows).
                The final batch may include fewer than ``batch_size`` rows if
                ``drop_last`` is ``False``. Defaults to 256.
            batch_format: The format in which to return each batch.
                Specify "default" to use the default block format (promoting
                tables to Pandas and tensors to NumPy), "pandas" to select
                ``pandas.DataFrame``, "pyarrow" to select ``pyarrow.Table``, or "numpy"
                to select ``numpy.ndarray`` for tensor datasets and
                ``Dict[str, numpy.ndarray]`` for tabular datasets. Default is "default".
            drop_last: Whether to drop the last batch if it's incomplete.
            local_shuffle_buffer_size: If non-None, the data will be randomly shuffled
                using a local in-memory shuffle buffer, and this value will serve as the
                minimum number of rows that must be in the local in-memory shuffle
                buffer in order to yield a batch. When there are no more rows to add to
                the buffer, the remaining rows in the buffer will be drained.
            local_shuffle_seed: The seed to use for the local random shuffle.

        Returns:
            An iterator over record batches.
        """
        raise NotImplementedError

    def iter_rows(self, *, prefetch_blocks: int = 0) -> Iterator[Union[T, TableRow]]:
        """Return a local row iterator over the dataset.

        If the dataset is a tabular dataset (Arrow/Pandas blocks), dict-like mappings
        :py:class:`~ray.data.row.TableRow` are yielded for each row by the iterator.
        If the dataset is not tabular, the raw row is yielded.

        Examples:
            >>> import ray
            >>> dataset = ray.data.range(10)
            >>> next(iter(dataset.iterator().iter_rows()))
            0

        Time complexity: O(1)

        Args:
            prefetch_blocks: The number of blocks to prefetch ahead of the
                current block during the scan.

        Returns:
            An iterator over rows of the dataset.
        """
        target_format = self._default_batch_format()
        for batch in self.iter_batches(
            batch_size=None, prefetch_blocks=prefetch_blocks, batch_format=target_format
        ):
            batch = BlockAccessor.for_block(BlockAccessor.batch_to_block(batch))
            for row in batch.iter_rows():
                yield row

    @abc.abstractmethod
    def stats(self) -> str:
        """Returns a string containing execution timing information."""
        raise NotImplementedError

    @abc.abstractmethod
    def schema(self) -> Union[type, "pyarrow.lib.Schema"]:
        """Return the schema of the dataset iterated over."""
        raise NotImplementedError

    def iter_torch_batches(
        self,
        *,
        prefetch_blocks: int = 0,
        batch_size: Optional[int] = 256,
        dtypes: Optional[Union["torch.dtype", Dict[str, "torch.dtype"]]] = None,
        device: Optional[str] = None,
        collate_fn: Optional[
            Callable[[Union[np.ndarray, Dict[str, np.ndarray]]], Any]
        ] = None,
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
    ) -> Iterator["TorchTensorBatchType"]:
        """Return a local batched iterator of Torch Tensors over the dataset.

        This iterator will yield single-tensor batches if the underlying dataset
        consists of a single column; otherwise, it will yield a dictionary of
        column-tensors. If looking for more flexibility in the tensor conversion (e.g.
        casting dtypes) or the batch format, try using `.iter_batches` directly.

        Examples:
            >>> import ray
            >>> for row in ray.data.range(
            ...     1000000
            ... ).iterator().iter_rows(): # doctest: +SKIP
            ...     print(row) # doctest: +SKIP

        Time complexity: O(1)

        Args:
            prefetch_blocks: The number of blocks to prefetch ahead of the
                current block during the scan.
            batch_size: The number of rows in each batch, or None to use entire blocks
                as batches (blocks may contain different number of rows).
                The final batch may include fewer than ``batch_size`` rows if
                ``drop_last`` is ``False``. Defaults to 256.
            dtypes: The Torch dtype(s) for the created tensor(s); if None, the dtype
                will be inferred from the tensor data.
            device: The device on which the tensor should be placed; if None, the Torch
                tensor will be constructed on the CPU.
            collate_fn: A function to convert a Numpy batch to a PyTorch tensor batch.
                Potential use cases include collating along a dimension other than the
                first, padding sequences of various lengths, or generally handling
                batches of different length tensors. If not provided, the default
                collate function is used which simply converts the batch of numpy
                arrays to a batch of PyTorch tensors. This API is still experimental
                and is subject to change.
            drop_last: Whether to drop the last batch if it's incomplete.
            local_shuffle_buffer_size: If non-None, the data will be randomly shuffled
                using a local in-memory shuffle buffer, and this value will serve as the
                minimum number of rows that must be in the local in-memory shuffle
                buffer in order to yield a batch. When there are no more rows to add to
                the buffer, the remaining rows in the buffer will be drained. This
                buffer size must be greater than or equal to ``batch_size``, and
                therefore ``batch_size`` must also be specified when using local
                shuffling.
            local_shuffle_seed: The seed to use for the local random shuffle.

        Returns:
            An iterator over Torch Tensor batches.
        """

        from ray.air._internal.torch_utils import (
            convert_ndarray_batch_to_torch_tensor_batch,
            get_device,
        )

        # Automatically move torch tensors to the appropriate device.
        if device is None:
            default_device = get_device()
            if default_device.type != "cpu":
                device = default_device

        if collate_fn is not None and (dtypes is not None or device is not None):
            raise ValueError(
                "collate_fn cannot be used with dtypes and device. It is expected that"
                "the provided `collate_fn` will move the output Torch tensors to the"
                "appropriate dtype and device."
            )

        if collate_fn is None:

            def collate_fn(batch: Union[np.ndarray, Dict[str, np.ndarray]]):
                return convert_ndarray_batch_to_torch_tensor_batch(
                    batch, dtypes=dtypes, device=device
                )

        yield from self.iter_batches(
            prefetch_blocks=prefetch_blocks,
            batch_size=batch_size,
            batch_format="numpy",
            drop_last=drop_last,
            local_shuffle_buffer_size=local_shuffle_buffer_size,
            local_shuffle_seed=local_shuffle_seed,
            _collate_fn=collate_fn,
        )

    def iter_tf_batches(
        self,
        *,
        prefetch_blocks: int = 0,
        batch_size: Optional[int] = 256,
        dtypes: Optional[Union["tf.dtypes.DType", Dict[str, "tf.dtypes.DType"]]] = None,
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
    ) -> Iterator["TensorFlowTensorBatchType"]:
        """Return a local batched iterator of TensorFlow Tensors over the dataset.

        This iterator will yield single-tensor batches of the underlying dataset
        consists of a single column; otherwise, it will yield a dictionary of
        column-tensors.

        .. tip::
            If you don't need the additional flexibility provided by this method,
            consider using :meth:`~ray.data.Dataset.to_tf` instead. It's easier
            to use.

        Examples:
            >>> import ray
            >>> for batch in ray.data.range( # doctest: +SKIP
            ...     12,
            ... ).iter_tf_batches(batch_size=4):
            ...     print(batch.shape) # doctest: +SKIP
            (4, 1)
            (4, 1)
            (4, 1)

        Time complexity: O(1)

        Args:
            prefetch_blocks: The number of blocks to prefetch ahead of the
                current block during the scan.
            batch_size: The number of rows in each batch, or None to use entire blocks
                as batches (blocks may contain different number of rows).
                The final batch may include fewer than ``batch_size`` rows if
                ``drop_last`` is ``False``. Defaults to 256.
            dtypes: The TensorFlow dtype(s) for the created tensor(s); if None, the
                dtype will be inferred from the tensor data.
            drop_last: Whether to drop the last batch if it's incomplete.
            local_shuffle_buffer_size: If non-None, the data will be randomly shuffled
                using a local in-memory shuffle buffer, and this value will serve as the
                minimum number of rows that must be in the local in-memory shuffle
                buffer in order to yield a batch. When there are no more rows to add to
                the buffer, the remaining rows in the buffer will be drained. This
                buffer size must be greater than or equal to ``batch_size``, and
                therefore ``batch_size`` must also be specified when using local
                shuffling.
            local_shuffle_seed: The seed to use for the local random shuffle.

        Returns:
            An iterator over TensorFlow Tensor batches.
        """
        from ray.air._internal.tensorflow_utils import (
            convert_ndarray_batch_to_tf_tensor_batch,
        )

        for batch in self.iter_batches(
            prefetch_blocks=prefetch_blocks,
            batch_size=batch_size,
            batch_format="numpy",
            drop_last=drop_last,
            local_shuffle_buffer_size=local_shuffle_buffer_size,
            local_shuffle_seed=local_shuffle_seed,
        ):
            yield convert_ndarray_batch_to_tf_tensor_batch(batch, dtypes=dtypes)

    def to_torch(
        self,
        *,
        label_column: Optional[str] = None,
        feature_columns: Optional[
            Union[List[str], List[List[str]], Dict[str, List[str]]]
        ] = None,
        label_column_dtype: Optional["torch.dtype"] = None,
        feature_column_dtypes: Optional[
            Union["torch.dtype", List["torch.dtype"], Dict[str, "torch.dtype"]]
        ] = None,
        batch_size: int = 1,
        prefetch_blocks: int = 0,
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
        unsqueeze_label_tensor: bool = True,
        unsqueeze_feature_tensors: bool = True,
    ) -> "torch.utils.data.IterableDataset":
        """Return a Torch IterableDataset over this dataset.

        This is only supported for datasets convertible to Arrow records.

        It is recommended to use the returned ``IterableDataset`` directly
        instead of passing it into a torch ``DataLoader``.

        Each element in IterableDataset will be a tuple consisting of 2
        elements. The first item contains the feature tensor(s), and the
        second item is the label tensor. Those can take on different
        forms, depending on the specified arguments.

        For the features tensor (N is the ``batch_size`` and n, m, k
        are the number of features per tensor):

        * If ``feature_columns`` is a ``List[str]``, the features will be
          a tensor of shape (N, n), with columns corresponding to
          ``feature_columns``

        * If ``feature_columns`` is a ``List[List[str]]``, the features will be
          a list of tensors of shape [(N, m),...,(N, k)], with columns of each
          tensor corresponding to the elements of ``feature_columns``

        * If ``feature_columns`` is a ``Dict[str, List[str]]``, the features
          will be a dict of key-tensor pairs of shape
          {key1: (N, m),..., keyN: (N, k)}, with columns of each
          tensor corresponding to the value of ``feature_columns`` under the
          key.

        If ``unsqueeze_label_tensor=True`` (default), the label tensor will be
        of shape (N, 1). Otherwise, it will be of shape (N,).
        If ``label_column`` is specified as ``None``, then no column from the
        ``Dataset`` will be treated as the label, and the output label tensor
        will be ``None``.

        Note that you probably want to call ``.split()`` on this dataset if
        there are to be multiple Torch workers consuming the data.

        Time complexity: O(1)

        Args:
            label_column: The name of the column used as the
                label (second element of the output list). Can be None for
                prediction, in which case the second element of returned
                tuple will also be None.
            feature_columns: The names of the columns
                to use as the features. Can be a list of lists or
                a dict of string-list pairs for multi-tensor output.
                If None, then use all columns except the label column as
                the features.
            label_column_dtype: The torch dtype to
                use for the label column. If None, then automatically infer
                the dtype.
            feature_column_dtypes: The dtypes to use for the feature
                tensors. This should match the format of ``feature_columns``,
                or be a single dtype, in which case it will be applied to
                all tensors. If None, then automatically infer the dtype.
            batch_size: How many samples per batch to yield at a time.
                Defaults to 1.
            prefetch_blocks: The number of blocks to prefetch ahead of
                the current block during the scan.
            drop_last: Set to True to drop the last incomplete batch,
                if the dataset size is not divisible by the batch size. If
                False and the size of dataset is not divisible by the batch
                size, then the last batch will be smaller. Defaults to False.
            local_shuffle_buffer_size: If non-None, the data will be randomly shuffled
                using a local in-memory shuffle buffer, and this value will serve as the
                minimum number of rows that must be in the local in-memory shuffle
                buffer in order to yield a batch. When there are no more rows to add to
                the buffer, the remaining rows in the buffer will be drained. This
                buffer size must be greater than or equal to ``batch_size``, and
                therefore ``batch_size`` must also be specified when using local
                shuffling.
            local_shuffle_seed: The seed to use for the local random shuffle.
            unsqueeze_label_tensor: If set to True, the label tensor
                will be unsqueezed (reshaped to (N, 1)). Otherwise, it will
                be left as is, that is (N, ). In general, regression loss
                functions expect an unsqueezed tensor, while classification
                loss functions expect a squeezed one. Defaults to True.
            unsqueeze_feature_tensors: If set to True, the features tensors
                will be unsqueezed (reshaped to (N, 1)) before being concatenated into
                the final features tensor. Otherwise, they will be left as is, that is
                (N, ). Defaults to True.

        Returns:
            A torch IterableDataset.
        """
        import torch

        from ray.air._internal.torch_utils import convert_pandas_to_torch_tensor
        from ray.data._internal.torch_iterable_dataset import TorchIterableDataset

        # If an empty collection is passed in, treat it the same as None
        if not feature_columns:
            feature_columns = None

        if feature_column_dtypes and not isinstance(feature_column_dtypes, torch.dtype):
            if isinstance(feature_columns, dict):
                if not isinstance(feature_column_dtypes, dict):
                    raise TypeError(
                        "If `feature_columns` is a dict, "
                        "`feature_column_dtypes` must be None, `torch.dtype`,"
                        f" or dict, got {type(feature_column_dtypes)}."
                    )
                if set(feature_columns) != set(feature_column_dtypes):
                    raise ValueError(
                        "`feature_columns` and `feature_column_dtypes` "
                        "must have the same keys."
                    )
                if any(not subcolumns for subcolumns in feature_columns.values()):
                    raise ValueError("column list may not be empty")
            elif isinstance(feature_columns[0], (list, tuple)):
                if not isinstance(feature_column_dtypes, (list, tuple)):
                    raise TypeError(
                        "If `feature_columns` is a list of lists, "
                        "`feature_column_dtypes` must be None, `torch.dtype`,"
                        f" or a sequence, got {type(feature_column_dtypes)}."
                    )
                if len(feature_columns) != len(feature_column_dtypes):
                    raise ValueError(
                        "`feature_columns` and `feature_column_dtypes` "
                        "must have the same length."
                    )
                if any(not subcolumns for subcolumns in feature_columns):
                    raise ValueError("column list may not be empty")

        def make_generator():
            for batch in self.iter_batches(
                batch_size=batch_size,
                batch_format="pandas",
                prefetch_blocks=prefetch_blocks,
                drop_last=drop_last,
                local_shuffle_buffer_size=local_shuffle_buffer_size,
                local_shuffle_seed=local_shuffle_seed,
            ):
                if label_column:
                    label_tensor = convert_pandas_to_torch_tensor(
                        batch,
                        [label_column],
                        label_column_dtype,
                        unsqueeze=unsqueeze_label_tensor,
                    )
                    batch.pop(label_column)
                else:
                    label_tensor = None

                if isinstance(feature_columns, dict):
                    features_tensor = {
                        key: convert_pandas_to_torch_tensor(
                            batch,
                            feature_columns[key],
                            feature_column_dtypes[key]
                            if isinstance(feature_column_dtypes, dict)
                            else feature_column_dtypes,
                            unsqueeze=unsqueeze_feature_tensors,
                        )
                        for key in feature_columns
                    }
                else:
                    features_tensor = convert_pandas_to_torch_tensor(
                        batch,
                        columns=feature_columns,
                        column_dtypes=feature_column_dtypes,
                        unsqueeze=unsqueeze_feature_tensors,
                    )

                yield (features_tensor, label_tensor)

        return TorchIterableDataset(make_generator)

    def to_tf(
        self,
        feature_columns: Union[str, List[str]],
        label_columns: Union[str, List[str]],
        *,
        prefetch_blocks: int = 0,
        batch_size: int = 1,
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
    ) -> "tf.data.Dataset":
        """Return a TF Dataset over this dataset.

        .. warning::
            If your dataset contains ragged tensors, this method errors. To prevent
            errors, resize tensors or
            :ref:`disable tensor extension casting <disable_tensor_extension_casting>`.

        Examples:
            >>> import ray
            >>> ds = ray.data.read_csv(
            ...     "s3://anonymous@air-example-data/iris.csv"
            ... )
            >>> it = ds.iterator(); it
            DatasetIterator(Dataset(
               num_blocks=1,
               num_rows=150,
               schema={
                  sepal length (cm): double,
                  sepal width (cm): double,
                  petal length (cm): double,
                  petal width (cm): double,
                  target: int64
               }
            ))

            If your model accepts a single tensor as input, specify a single feature column.

            >>> it.to_tf(feature_columns="sepal length (cm)", label_columns="target")  # doctest: +SKIP
            <_OptionsDataset element_spec=(TensorSpec(shape=(None,), dtype=tf.float64, name='sepal length (cm)'), TensorSpec(shape=(None,), dtype=tf.int64, name='target'))>

            If your model accepts a dictionary as input, specify a list of feature columns.

            >>> it.to_tf(["sepal length (cm)", "sepal width (cm)"], "target")  # doctest: +SKIP
            <_OptionsDataset element_spec=({'sepal length (cm)': TensorSpec(shape=(None,), dtype=tf.float64, name='sepal length (cm)'), 'sepal width (cm)': TensorSpec(shape=(None,), dtype=tf.float64, name='sepal width (cm)')}, TensorSpec(shape=(None,), dtype=tf.int64, name='target'))>

            If your dataset contains multiple features but your model accepts a single
            tensor as input, combine features with
            :class:`~ray.data.preprocessors.Concatenator`.

            >>> from ray.data.preprocessors import Concatenator
            >>> preprocessor = Concatenator(output_column_name="features", exclude="target")
            >>> it = preprocessor.transform(ds).iterator()
            >>> it
            DatasetIterator(Concatenator
            +- Dataset(
                  num_blocks=1,
                  num_rows=150,
                  schema={
                     sepal length (cm): double,
                     sepal width (cm): double,
                     petal length (cm): double,
                     petal width (cm): double,
                     target: int64
                  }
               ))
            >>> it.to_tf("features", "target")  # doctest: +SKIP
            <_OptionsDataset element_spec=(TensorSpec(shape=(None, 4), dtype=tf.float64, name='features'), TensorSpec(shape=(None,), dtype=tf.int64, name='target'))>

        Args:
            feature_columns: Columns that correspond to model inputs. If this is a
                string, the input data is a tensor. If this is a list, the input data
                is a ``dict`` that maps column names to their tensor representation.
            label_column: Columns that correspond to model targets. If this is a
                string, the target data is a tensor. If this is a list, the target data
                is a ``dict`` that maps column names to their tensor representation.
            prefetch_blocks: The number of blocks to prefetch ahead of the
                current block during the scan.
            batch_size: Record batch size. Defaults to 1.
            drop_last: Set to True to drop the last incomplete batch,
                if the dataset size is not divisible by the batch size. If
                False and the size of dataset is not divisible by the batch
                size, then the last batch will be smaller. Defaults to False.
            local_shuffle_buffer_size: If non-None, the data will be randomly shuffled
                using a local in-memory shuffle buffer, and this value will serve as the
                minimum number of rows that must be in the local in-memory shuffle
                buffer in order to yield a batch. When there are no more rows to add to
                the buffer, the remaining rows in the buffer will be drained. This
                buffer size must be greater than or equal to ``batch_size``, and
                therefore ``batch_size`` must also be specified when using local
                shuffling.
            local_shuffle_seed: The seed to use for the local random shuffle.

        Returns:
            A ``tf.data.Dataset`` that yields inputs and targets.
        """  # noqa: E501

        from ray.air._internal.tensorflow_utils import (
            get_type_spec,
            convert_ndarray_to_tf_tensor,
        )

        try:
            import tensorflow as tf
        except ImportError:
            raise ValueError("tensorflow must be installed!")

        if self._is_tensor_dataset():
            raise NotImplementedError(
                "`to_tf` doesn't support single-column tensor datasets. Call the "
                "more-flexible `iter_batches` instead."
            )

        schema = self.schema()
        if isinstance(schema, type):
            raise NotImplementedError(
                "`to_tf` doesn't support simple datasets. Call `map_batches` and "
                "convert your data to a tabular format. Alternatively, call the more-"
                "flexible `iter_batches` in place of `to_tf`."
            )

        valid_columns = schema.names

        def validate_column(column: str) -> None:
            if column not in valid_columns:
                raise ValueError(
                    f"You specified '{column}' in `feature_columns` or "
                    f"`label_columns`, but there's no column named '{column}' in the "
                    f"dataset. Valid column names are: {valid_columns}."
                )

        def validate_columns(columns: Union[str, List]) -> None:
            if isinstance(columns, list):
                for column in columns:
                    validate_column(column)
            else:
                validate_column(columns)

        validate_columns(feature_columns)
        validate_columns(label_columns)

        def convert_batch_to_tensors(
            batch: Dict[str, np.ndarray],
            *,
            columns: Union[str, List[str]],
            type_spec: Union[tf.TypeSpec, Dict[str, tf.TypeSpec]],
        ) -> Union[tf.Tensor, Dict[str, tf.Tensor]]:
            if isinstance(columns, str):
                return convert_ndarray_to_tf_tensor(batch[columns], type_spec=type_spec)
            return {
                column: convert_ndarray_to_tf_tensor(
                    batch[column], type_spec=type_spec[column]
                )
                for column in columns
            }

        def generator():
            for batch in self.iter_batches(
                prefetch_blocks=prefetch_blocks,
                batch_size=batch_size,
                drop_last=drop_last,
                local_shuffle_buffer_size=local_shuffle_buffer_size,
                local_shuffle_seed=local_shuffle_seed,
                batch_format="numpy",
            ):
                assert isinstance(batch, dict)
                features = convert_batch_to_tensors(
                    batch, columns=feature_columns, type_spec=feature_type_spec
                )
                labels = convert_batch_to_tensors(
                    batch, columns=label_columns, type_spec=label_type_spec
                )
                yield features, labels

        feature_type_spec = get_type_spec(schema, columns=feature_columns)
        label_type_spec = get_type_spec(schema, columns=label_columns)
        output_signature = (feature_type_spec, label_type_spec)

        dataset = tf.data.Dataset.from_generator(
            generator, output_signature=output_signature
        )

        options = tf.data.Options()
        options.experimental_distribute.auto_shard_policy = (
            tf.data.experimental.AutoShardPolicy.OFF
        )
        return dataset.with_options(options)

    def iter_epochs(self, max_epoch: int = -1) -> None:
        raise DeprecationWarning(
            "If you are using AIR, note that session.get_dataset_shard() "
            "returns a ray.data.DatasetIterator instead of a "
            "DatasetPipeline as of Ray 2.3. "
            "To iterate over one epoch of data, use iter_batches(), "
            "iter_torch_batches(), or to_tf()."
        )

    def _is_tensor_dataset(self) -> bool:
        """Return ``True`` if this is an iterator over a tensor dataset."""
        schema = self.schema()
        if schema is None or isinstance(schema, type):
            return False
        return _is_tensor_schema(schema.names)

    def _default_batch_format(self) -> Literal["default", "pandas", "pyarrow", "numpy"]:
        """Returns the best batch format that lines up with the dataset format.

        NOTE: Return "default" here. Subclass can override this method to decide best
        batch format.
        """
        return "default"
