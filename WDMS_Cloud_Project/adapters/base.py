"""Shared, platform-neutral data types and adapter contract."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Protocol, Sequence


@dataclass(frozen=True)
class Owner:
    owner_id: str
    name: str


@dataclass(frozen=True)
class QueryTicket:
    request_id: str
    owner_id: str
    owner_name: str
    started_monotonic: float
    evidence: Dict[str, Any]


@dataclass(frozen=True)
class InventoryResult:
    request_id: str
    owner: Owner
    whole_count: int
    loose_count: int | Decimal
    evidence: Dict[str, Any]


class WDMSAdapter(Protocol):
    def connect(self) -> None: ...
    def login(self) -> None: ...
    def navigate(self) -> None: ...
    def list_owners(self) -> Sequence[Owner]: ...
    def select_owner(self, owner: Owner) -> None: ...
    def start_query(self, owner: Owner, timeout_seconds: float) -> QueryTicket: ...
    def read_result(self, ticket: QueryTicket, timeout_seconds: float) -> InventoryResult: ...
    def close(self) -> None: ...
