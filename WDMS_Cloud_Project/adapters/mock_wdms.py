"""A deterministic local WDMS simulation with explicit evidence and fault injection."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional, Sequence

from adapters.base import InventoryResult, Owner, QueryTicket
from errors import WDMSError


MOCK_RECORDS = tuple(
    (f"模拟货主{i:02d}", *((0, 0) if i == 1 else (120, 8) if i in (2, 3) else ((i * 137) % 10000, (i * 23) % 1000)))
    for i in range(1, 25)
)


class MockWDMSAdapter:
    def __init__(self, faults: Optional[Dict[str, Any]] = None):
        self.faults = faults or {}
        self._owners = [Owner(name, name) for name, _, _ in MOCK_RECORDS]
        self._selected: Optional[Owner] = None
        self._generation = 0
        self._connected = False
        self._pending: Dict[str, Dict[str, Any]] = {}

    def connect(self) -> None:
        self._connected = True

    def login(self) -> None:
        if not self._connected:
            raise WDMSError("ADAPTER_NOT_CONNECTED", "Mock 适配器尚未连接。")

    def navigate(self) -> None:
        if not self._connected:
            raise WDMSError("ADAPTER_NOT_CONNECTED", "Mock 适配器尚未连接。")

    def list_owners(self) -> Sequence[Owner]:
        if self.faults.get("duplicate_owner_list"):
            return list(self._owners) + [self._owners[0]]
        if self.faults.get("missing_owner_list"):
            return list(self._owners[:-1])
        return tuple(self._owners)

    def select_owner(self, owner: Owner) -> None:
        self._ensure_connected()
        if owner.name in self.faults.get("select_fail", []):
            raise WDMSError("OWNER_SELECTION_FAILED", "无法确认已选择目标货主。")
        matches = [candidate for candidate in self._owners if candidate.name == owner.name]
        if len(matches) != 1:
            raise WDMSError("OWNER_SELECTION_FAILED", "目标货主缺失或存在歧义。")
        self._selected = matches[0]

    def start_query(self, owner: Owner, timeout_seconds: float) -> QueryTicket:
        self._ensure_connected()
        if self._selected != owner:
            raise WDMSError("OWNER_SELECTION_FAILED", "查询前未能确认目标货主。")
        if owner.name in self.faults.get("timeout", []):
            raise WDMSError("QUERY_TIMEOUT", "Mock 查询超过配置的时限。")
        request_id = str(uuid.uuid4())
        before = self._generation
        if owner.name not in self.faults.get("stale_result", []):
            self._generation += 1
        record = next((r for r in MOCK_RECORDS if r[0] == owner.name), None)
        if record is None:
            raise WDMSError("OWNER_NOT_EXPECTED", "Mock 查询货主不在模拟清单中。")
        result_owner = owner
        if owner.name in self.faults.get("wrong_owner", []):
            result_owner = next(candidate for candidate in self._owners if candidate != owner)
        whole, loose = record[1], record[2]
        if owner.name in self.faults.get("missing_whole_count", []):
            whole = None
        ticket = QueryTicket(request_id, owner.owner_id, owner.name, time.monotonic(), {"generation_before": before})
        self._pending[request_id] = {
            "ticket": ticket,
            "owner": result_owner,
            "whole": whole,
            "loose": loose,
            "complete": owner.name not in self.faults.get("false_complete", []),
        }
        return ticket

    def read_result(self, ticket: QueryTicket, timeout_seconds: float) -> InventoryResult:
        self._ensure_connected()
        pending = self._pending.pop(ticket.request_id, None)
        if pending is None:
            raise WDMSError("QUERY_TICKET_INVALID", "查询请求凭据无效或已使用。")
        if not pending["complete"]:
            raise WDMSError("QUERY_NOT_COMPLETE", "Mock 没有观察到本次查询完成。")
        if self._generation <= ticket.evidence["generation_before"]:
            raise WDMSError("QUERY_EVIDENCE_STALE", "Mock 结果代次未更新，拒绝读取旧结果。")
        if pending["owner"] != self._selected or pending["owner"].name != ticket.owner_name:
            raise WDMSError("QUERY_OWNER_MISMATCH", "Mock 当前结果货主与目标不一致。")
        if owner_in_fault(self.faults, "read_fail", ticket.owner_name):
            raise WDMSError("RESULT_READ_FAILED", "Mock 库存字段读取失败。")
        whole = pending["whole"]
        loose = pending["loose"]
        if owner_in_fault(self.faults, "mutate_during_read", ticket.owner_name):
            self._generation += 1
        if self._generation <= ticket.evidence["generation_before"]:
            raise WDMSError("QUERY_EVIDENCE_STALE", "Mock 结果在读取期间发生变化，拒绝发布。")
        if owner_in_fault(self.faults, "mutate_during_read", ticket.owner_name):
            raise WDMSError("QUERY_EVIDENCE_CHANGED", "Mock 结果在读取期间改变，拒绝发布。")
        evidence = {"generation": self._generation, "owner_id": pending["owner"].owner_id, "complete": True}
        return InventoryResult(ticket.request_id, pending["owner"], whole, loose, evidence)  # type: ignore[arg-type]

    def close(self) -> None:
        self._connected = False
        self._pending.clear()

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise WDMSError("ADAPTER_NOT_CONNECTED", "Mock 适配器尚未连接。")


def owner_in_fault(faults: Dict[str, Any], key: str, owner_name: str) -> bool:
    return owner_name in faults.get(key, [])
