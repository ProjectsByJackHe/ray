import ray
import copy
from collections import deque
from ray.data.block import (
    Block,
    BlockAccessor,
    BlockMetadata,
)
from ray.data._internal.stats import StatsDict
from ray.data._internal.execution.interfaces import (
    PhysicalOperator,
    RefBundle,
)
from ray.data._internal.remote_fn import cached_remote_fn
from ray.types import ObjectRef
from typing import (
    Deque,
    List,
    Optional,
    Tuple,
)


class LimitOperator(PhysicalOperator):
    """Physical operator for limit."""

    def __init__(
        self,
        limit: int,
        input_op: PhysicalOperator,
    ):
        self._limit = limit
        self._consumed_rows = 0
        self._buffer: Deque[RefBundle] = deque()
        self._name = f"Limit[limit={limit}]"
        self._output_metadata: List[BlockMetadata] = []
        self._num_outputs_total = input_op.num_outputs_total()
        if self._num_outputs_total is not None:
            self._num_outputs_total = min(self._num_outputs_total, limit)
        super().__init__(self._name, [input_op])

    def _limit_reached(self) -> bool:
        return self._consumed_rows >= self._limit

    def add_input(self, refs: RefBundle, input_index: int) -> None:
        assert not self.completed()
        assert input_index == 0, input_index
        if self._limit_reached():
            return
        out_blocks: List[ObjectRef[Block]] = []
        out_metadata: List[BlockMetadata] = []
        for block, metadata in refs.blocks:
            num_rows = metadata.num_rows
            assert num_rows is not None
            if self._consumed_rows + num_rows <= self._limit:
                self._consumed_rows += num_rows
                out_blocks.append(block)
                out_metadata.append(metadata)
                self._output_metadata.append(metadata)
            else:
                # Slice the last block.
                def slice_fn(block, metadata, num_rows) -> Tuple[Block, BlockMetadata]:
                    block = BlockAccessor.for_block(block).slice(0, num_rows, copy=True)
                    metadata = copy.deepcopy(metadata)
                    metadata.num_rows = num_rows
                    metadata.size_bytes = BlockAccessor.for_block(block).size_bytes()
                    return block, metadata

                block, metadata_ref = cached_remote_fn(slice_fn, num_returns=2).remote(
                    block,
                    metadata,
                    self._limit - self._consumed_rows,
                )
                out_blocks.append(block)
                metadata = ray.get(metadata_ref)
                out_metadata.append(metadata)
                self._output_metadata.append(metadata)
                break
        out_refs = RefBundle(
            list(zip(out_blocks, out_metadata)),
            owns_blocks=refs.owns_blocks,
        )
        self._buffer.append(out_refs)

    def has_next(self) -> bool:
        return len(self._buffer) > 0

    def get_next(self) -> RefBundle:
        return self._buffer.popleft()

    def get_stats(self) -> StatsDict:
        return {self._name: self._output_metadata}

    def num_outputs_total(self) -> Optional[int]:
        if self._limit_reached():
            return self._limit
        else:
            return self._num_outputs_total
