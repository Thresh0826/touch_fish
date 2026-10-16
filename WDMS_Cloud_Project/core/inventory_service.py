"""Ordered, fail-closed inventory collection."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from adapters.base import InventoryResult, Owner, WDMSAdapter
from core.numbers import parse_nonnegative_integer, parse_nonnegative_quantity
from errors import WDMSError


ProgressCallback = Optional[Callable[[int, int], None]]


class InventoryService:
    def __init__(self, adapter: WDMSAdapter, query_timeout_seconds: float = 15):
        self.adapter = adapter
        self.query_timeout_seconds = query_timeout_seconds

    def collect(self, expected_owners: Sequence[Owner], limit: Optional[int] = None, progress: ProgressCallback = None) -> List[InventoryResult]:
        self.adapter.connect()
        self.adapter.login()
        self.adapter.navigate()
        actual = list(self.adapter.list_owners())
        self._validate_owner_roster(actual, expected_owners)
        target = list(expected_owners[:limit] if limit is not None else expected_owners)
        if not target:
            raise WDMSError("EMPTY_SCOPE", "本次任务没有可采集的货主。")

        collected: List[InventoryResult] = []
        seen = set()
        for owner in target:
            if owner.name in seen:
                raise WDMSError("OWNER_DUPLICATE", "采集清单包含重复货主。")
            seen.add(owner.name)
            self.adapter.select_owner(owner)
            ticket = self.adapter.start_query(owner, self.query_timeout_seconds)
            result = self.adapter.read_result(ticket, self.query_timeout_seconds)
            self._validate_result(owner, ticket.request_id, result)
            collected.append(result)
            if progress:
                progress(len(collected), len(target))

        if [row.owner.name for row in collected] != [owner.name for owner in target]:
            raise WDMSError("OWNER_SET_MISMATCH", "采集结果货主集合或顺序与预期清单不一致。")
        return collected

    @staticmethod
    def _validate_owner_roster(actual: Sequence[Owner], expected: Sequence[Owner]) -> None:
        actual_names = [owner.name for owner in actual]
        expected_names = [owner.name for owner in expected]
        if len(actual_names) != 24 or len(set(actual_names)) != 24:
            raise WDMSError("OWNER_LIST_INVALID", "WDMS 货主清单缺失、重复或数量不符。")
        if set(actual_names) != set(expected_names):
            raise WDMSError("OWNER_LIST_MISMATCH", "WDMS 货主清单与人工确认清单不一致。")

    @staticmethod
    def _validate_result(expected: Owner, request_id: str, result: InventoryResult) -> None:
        if result.request_id != request_id:
            raise WDMSError("QUERY_TICKET_MISMATCH", "查询结果与本次请求标识不一致。")
        if result.owner != expected or result.owner.name != expected.name:
            raise WDMSError("QUERY_OWNER_MISMATCH", "查询结果货主与目标货主不一致。")
        parse_nonnegative_integer(result.whole_count, "整件数")
        parse_nonnegative_quantity(result.loose_count, "散件数")
