import copy
import logging
import time
import threading
from typing import (
    List,
    Dict,
    Optional,
    Iterator,
    Callable,
    Any,
    Union,
    TYPE_CHECKING,
)

import ray

from ray.data.dataset_iterator import DatasetIterator
from ray.data.block import Block, DataBatch
from ray.data.context import DatasetContext
from ray.data._internal.execution.streaming_executor import StreamingExecutor
from ray.data._internal.execution.legacy_compat import (
    execute_to_legacy_bundle_iterator,
)
from ray.data._internal.block_batching import batch_block_refs
from ray.data._internal.execution.operators.output_splitter import OutputSplitter
from ray.data._internal.execution.interfaces import NodeIdStr, RefBundle
from ray.types import ObjectRef
from ray.util.debug import log_once
from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

if TYPE_CHECKING:
    import pyarrow
    from ray.data import Dataset

logger = logging.getLogger(__name__)


BLOCKED_CLIENT_WARN_TIMEOUT = 30


class StreamSplitDatasetIterator(DatasetIterator):
    """Implements a collection of iterators over a shared data stream."""

    @staticmethod
    def create(
        base_dataset: "Dataset",
        n: int,
        equal: bool,
        locality_hints: Optional[List[NodeIdStr]],
    ) -> List["StreamSplitDatasetIterator"]:
        """Create a split iterator from the given base Dataset and options.

        See also: `Dataset.streaming_split`.
        """
        ctx = DatasetContext.get_current()

        # To avoid deadlock, the concurrency on this actor must be set to at least `n`.
        coord_actor = SplitCoordinator.options(
            max_concurrency=n,
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                ray.get_runtime_context().get_node_id(), soft=False
            ),
        ).remote(ctx, base_dataset, n, equal, locality_hints)

        return [
            StreamSplitDatasetIterator(base_dataset, coord_actor, i) for i in range(n)
        ]

    def __init__(
        self,
        base_dataset: "Dataset",
        coord_actor: ray.actor.ActorHandle,
        output_split_idx: int,
    ):
        self._base_dataset = base_dataset
        self._coord_actor = coord_actor
        self._output_split_idx = output_split_idx

    def iter_batches(
        self,
        *,
        prefetch_blocks: int = 0,
        batch_size: int = 256,
        batch_format: Optional[str] = "default",
        drop_last: bool = False,
        local_shuffle_buffer_size: Optional[int] = None,
        local_shuffle_seed: Optional[int] = None,
        _collate_fn: Optional[Callable[[DataBatch], Any]] = None,
    ) -> Iterator[DataBatch]:
        """Implements DatasetIterator."""

        def gen_blocks() -> Iterator[ObjectRef[Block]]:
            cur_epoch = ray.get(
                self._coord_actor.start_epoch.remote(self._output_split_idx)
            )
            future: ObjectRef[
                Optional[ObjectRef[Block]]
            ] = self._coord_actor.get.remote(cur_epoch, self._output_split_idx)
            while True:
                block_ref: Optional[ObjectRef[Block]] = ray.get(future)
                if not block_ref:
                    break
                else:
                    future = self._coord_actor.get.remote(
                        cur_epoch, self._output_split_idx
                    )
                    yield block_ref

        yield from batch_block_refs(
            gen_blocks(),
            stats=None,
            prefetch_blocks=prefetch_blocks,
            batch_size=batch_size,
            batch_format=batch_format,
            drop_last=drop_last,
            collate_fn=_collate_fn,
            shuffle_buffer_min_size=local_shuffle_buffer_size,
            shuffle_seed=local_shuffle_seed,
        )

    def stats(self) -> str:
        """Implements DatasetIterator."""
        return self._base_dataset.stats()

    def schema(self) -> Union[type, "pyarrow.lib.Schema"]:
        """Implements DatasetIterator."""
        return self._base_dataset.schema()


@ray.remote(num_cpus=0)
class SplitCoordinator:
    """Coordinator actor for routing blocks to output splits.

    This actor runs a streaming executor locally on its main thread. Clients can
    retrieve results via actor calls running on other threads.
    """

    def __init__(
        self,
        ctx: DatasetContext,
        dataset: "Dataset",
        n: int,
        equal: bool,
        locality_hints: Optional[List[NodeIdStr]],
    ):
        # Automatically set locality with output to the specified location hints.
        if locality_hints:
            ctx.execution_options.locality_with_output = locality_hints
            logger.info(f"Auto configuring locality_with_output={locality_hints}")

        DatasetContext._set_current(ctx)
        self._base_dataset = dataset
        self._n = n
        self._equal = equal
        self._locality_hints = locality_hints
        self._lock = threading.RLock()

        # Guarded by self._lock.
        self._next_bundle: Dict[int, RefBundle] = {}
        self._unfinished_clients_in_epoch = n
        self._cur_epoch = -1

        def gen_epochs():
            while True:
                executor = StreamingExecutor(copy.deepcopy(ctx.execution_options))

                def add_split_op(dag):
                    return OutputSplitter(dag, n, equal, locality_hints)

                output_iterator = execute_to_legacy_bundle_iterator(
                    executor,
                    dataset._plan,
                    True,
                    dataset._plan._dataset_uuid,
                    dag_rewrite=add_split_op,
                )
                yield output_iterator

        self._next_epoch = gen_epochs()
        self._output_iterator = None

    def start_epoch(self, split_idx: int) -> str:
        """Called to start an epoch.

        Returns:
            UUID for the epoch, which must be used when accessing results via get().
        """

        # Wait for all clients to arrive at the barrier before starting a new epoch.
        epoch_id = self._barrier(split_idx)
        return epoch_id

    def get(self, epoch_id: int, output_split_idx: int) -> Optional[ObjectRef[Block]]:
        """Blocking get operation.

        This is intended to be called concurrently from multiple clients.
        """

        if epoch_id != self._cur_epoch:
            raise ValueError(
                "Invalid iterator: the datastream has moved on to another epoch."
            )

        try:
            # Ensure there is at least one bundle.
            with self._lock:
                if output_split_idx in self._next_bundle:
                    next_bundle = self._next_bundle[output_split_idx]
                else:
                    next_bundle = None

            # Fetch next bundle if needed.
            if next_bundle is None:
                # This is a BLOCKING call, so do it outside the lock.
                next_bundle = self._output_iterator.get_next(output_split_idx)

            block = next_bundle.blocks.pop()[0]

            # Accumulate any remaining blocks in next_bundle map as needed.
            with self._lock:
                self._next_bundle[output_split_idx] = next_bundle
                if not next_bundle.blocks:
                    del self._next_bundle[output_split_idx]

            return block
        except StopIteration:
            return None

    def _barrier(self, split_idx: int) -> int:
        """Arrive and block until the start of the given epoch."""

        # Decrement and await all clients to arrive here.
        with self._lock:
            starting_epoch = self._cur_epoch
            self._unfinished_clients_in_epoch -= 1

        start_time = time.time()
        while (
            self._cur_epoch == starting_epoch and self._unfinished_clients_in_epoch != 0
        ):
            if time.time() - start_time > BLOCKED_CLIENT_WARN_TIMEOUT:
                if log_once(f"stream_split_blocked_{split_idx}_{starting_epoch}"):
                    logger.warning(
                        f"StreamSplitDatasetIterator(epoch={starting_epoch}, "
                        f"split={split_idx}) blocked waiting on other clients "
                        f"for more than {BLOCKED_CLIENT_WARN_TIMEOUT}s. All "
                        "clients must read from the DatasetIterator splits at "
                        "the same time. This warning will not be printed again "
                        "for this epoch."
                    )
            time.sleep(0.1)

        # Advance to the next epoch.
        with self._lock:
            if self._cur_epoch == starting_epoch:
                self._cur_epoch += 1
                self._unfinished_clients_in_epoch = self._n
                self._output_iterator = next(self._next_epoch)

        assert self._output_iterator is not None
        return starting_epoch + 1
