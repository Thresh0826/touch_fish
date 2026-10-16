"""Windows-only WDMS adapter using a hard-supervised child worker."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from adapters.base import InventoryResult, Owner, QueryTicket
from adapters.supervisor import JsonLineWorker
from core.numbers import parse_nonnegative_integer, parse_nonnegative_quantity
from errors import WDMSError


class RealWDMSAdapter:
    def __init__(self, config: Dict[str, Any], project_root: Path):
        if sys.platform != "win32":
            raise WDMSError("REAL_MODE_WINDOWS_ONLY", "Real 模式只能在 Windows 上执行。")
        self.config = config
        config_path = Path(str(config["_config_path"])).resolve()
        command = [sys.executable, "-m", "adapters.worker", "--config", str(config_path)]
        self._worker = JsonLineWorker(command, cwd=str(project_root))
        self._task_deadline: Optional[float] = None
        self._timeout = float(config.get("real", {}).get("action_timeout_seconds", 20))

    def set_task_deadline(self, deadline: float) -> None:
        self._task_deadline = deadline

    def _request(self, action: str, payload: Optional[Dict[str, Any]] = None, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        budget = timeout_seconds if timeout_seconds is not None else self._timeout
        if self._task_deadline is not None:
            budget = min(budget, self._task_deadline - time.monotonic())
        return self._worker.request(action, payload, budget)

    def connect(self) -> None:
        startup_budget = float(self.config.get("real", {}).get("startup_timeout_seconds", 30)) + 5.0
        self._request("connect", timeout_seconds=max(self._timeout, startup_budget))

    def login(self) -> None:
        self._request("login")

    def navigate(self) -> None:
        self._request("navigate")

    def list_owners(self) -> Sequence[Owner]:
        result = self._request("list_owners")
        rows = result.get("owners")
        if not isinstance(rows, list):
            raise WDMSError("OWNER_LIST_INVALID", "Real 适配器未返回可验证的货主清单。")
        return tuple(Owner(str(row["id"]), str(row["name"])) for row in rows if isinstance(row, dict) and row.get("name"))

    def select_owner(self, owner: Owner) -> None:
        self._request("select_owner", {"owner_id": owner.owner_id, "owner_name": owner.name}, min(self._timeout, 8.0))

    def start_query(self, owner: Owner, timeout_seconds: float) -> QueryTicket:
        result = self._request(
            "start_query",
            {"owner_id": owner.owner_id, "owner_name": owner.name},
            min(self._timeout, 8.0),
        )
        request_id = str(result.get("request_id", ""))
        if not request_id:
            raise WDMSError("QUERY_TICKET_INVALID", "Real 适配器未返回本次查询标识。")
        return QueryTicket(request_id, owner.owner_id, owner.name, time.monotonic(), dict(result.get("evidence", {})))

    def read_result(self, ticket: QueryTicket, timeout_seconds: float) -> InventoryResult:
        query_budget = float(timeout_seconds)
        result = self._request(
            "read_result",
            {"request_id": ticket.request_id, "owner_id": ticket.owner_id, "owner_name": ticket.owner_name, "timeout_seconds": query_budget},
            query_budget + 1.0,
        )
        if result.get("request_id") != ticket.request_id:
            raise WDMSError("QUERY_TICKET_MISMATCH", "Real 查询结果与本次请求标识不一致。")
        owner = Owner(str(result.get("owner_id", "")), str(result.get("owner_name", "")))
        whole = parse_nonnegative_integer(result.get("whole_count"), "整件数")
        loose = parse_nonnegative_quantity(result.get("loose_count"), "散件数")
        evidence = result.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise WDMSError("QUERY_EVIDENCE_UNVERIFIED", "Real 查询结果缺少完成及货主归属证据。")
        return InventoryResult(ticket.request_id, owner, whole, loose, evidence)

    def close(self) -> None:
        self._worker.close()
