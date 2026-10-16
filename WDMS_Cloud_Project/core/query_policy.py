"""One source of collection budgets; durations use a monotonic clock."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from errors import WDMSError


@dataclass(frozen=True)
class NativeMessagePolicy:
    """Per-message milliseconds; watchdog budgets remain in seconds."""
    health_ms: int = 500
    text_read_ms: int = 100
    combo_read_ms: int = 100
    combo_select_ms: int = 500
    owner_notify_ms: int = 1000
    login_label_ms: int = 2500
    field_write_ms: int = 1000

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 1 <= value <= 5000
               for value in vars(self).values()):
            raise WDMSError("QUERY_POLICY_INVALID", "原生消息时限须为1至5000毫秒的整数。")


@dataclass(frozen=True)
class QueryPolicy:
    preparation_seconds: float = 40.0
    idle_seconds: float = 20.0
    query_seconds: float = 90.0
    cleanup_seconds: float = 10.0
    report_seconds: float = 60.0
    settle_seconds: float = 5.0
    sample_count: int = 3
    sample_interval: float = 0.25
    messages: NativeMessagePolicy = field(default_factory=NativeMessagePolicy)

    def __post_init__(self) -> None:
        durations = (self.preparation_seconds, self.idle_seconds, self.query_seconds,
                     self.cleanup_seconds, self.report_seconds, self.settle_seconds,
                     self.sample_interval)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in durations):
            raise WDMSError("QUERY_POLICY_INVALID", "查询与采集时限必须是有限正数。")
        if type(self.sample_count) is not int or self.sample_count < 3:
            raise WDMSError("QUERY_POLICY_INVALID", "采集须至少进行三次稳定采样。")
        if not isinstance(self.messages, NativeMessagePolicy):
            raise WDMSError("QUERY_POLICY_INVALID", "原生消息时限配置无效。")

    @property
    def worker_seconds(self) -> float:
        return self.preparation_seconds + self.idle_seconds + self.query_seconds + self.cleanup_seconds

    def collection_seconds(self, count: int) -> float:
        if type(count) is not int or not 1 <= count <= 24:
            raise WDMSError("QUERY_POLICY_INVALID", "采集数量必须在1至24之间。")
        return count * self.worker_seconds + self.report_seconds


DEFAULT_QUERY_POLICY = QueryPolicy()
