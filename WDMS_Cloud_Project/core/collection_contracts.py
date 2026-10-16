"""Immutable requests shared by collection, worker supervision and reports."""
from __future__ import annotations
from typing import Optional, Protocol

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.query_policy import QueryPolicy
from errors import WDMSError
import math


@dataclass(frozen=True)
class RunContext:
    run_id: str
    report_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id or not isinstance(self.report_date, date):
            raise WDMSError("COLLECTION_REQUEST_INVALID", "采集任务编号和报表日期无效。")


@dataclass(frozen=True)
class CollectionRequest:
    limit: int
    out: Path
    report_date: date
    run_id: str
    owners: Optional[Path] = None
    owners_sha256: Optional[str] = None
    title: str = "医药企业仓储运输管理系统"
    query_timeout: int = 90
    managed_notices: bool = False
    workflow_progress: Optional[Path] = None
    result_manifest: Optional[Path] = None
    cancel_file: Optional[Path] = None
    execution_deadline: Optional[float] = None

    def __post_init__(self) -> None:
        RunContext(self.run_id, self.report_date)
        if type(self.limit) is not int or self.limit not in (1, 3, 24):
            raise WDMSError("COLLECTION_REQUEST_INVALID", "采集范围必须是1、3或24个货主。")
        if type(self.query_timeout) is not int or not 30 <= self.query_timeout <= 120:
            raise WDMSError("COLLECTION_REQUEST_INVALID", "单货主查询时限必须在30至120秒之间。")
        if self.execution_deadline is not None and (isinstance(self.execution_deadline, bool)
                or not isinstance(self.execution_deadline, (int, float)) or not math.isfinite(self.execution_deadline)):
            raise WDMSError("COLLECTION_REQUEST_INVALID", "采集期限必须是有限的monotonic时间。")


@dataclass(frozen=True)
class StageRequest:
    output: Path
    deadline: float
    cancel_file: Path
    snapshot: Optional[Path] = None
    reuse_session: bool = False


@dataclass(frozen=True)
class WorkerJob:
    worker_id: str
    run_id: str
    index: int
    owners: Path
    owners_sha256: str
    log: Path
    result: Path
    stderr: Path
    title: str
    policy: QueryPolicy
    deadline: Optional[float] = None
    cancel_file: Optional[Path] = None
    expected_owner: Optional[str] = None


@dataclass(frozen=True)
class CollectionJob:
    context: RunContext
    folder: Path
    owners: Path
    owners_sha256: str
    names: tuple[str, ...]
    limit: int
    title: str
    policy: QueryPolicy
    result_manifest: Optional[Path] = None
    cancel_file: Optional[Path] = None
    deadline: Optional[float] = None
    managed_notices: bool = False


class ProgressNotice(Protocol):
    progress: Optional[Path]

    def update(self, current: int, count: int, name: str) -> None: ...
    def close(self) -> None: ...
