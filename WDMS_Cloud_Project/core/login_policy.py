"""Bounded cold-start readiness; selection rules and click messages are separate."""
from dataclasses import dataclass
import math

from errors import WDMSError


@dataclass(frozen=True)
class LoginReadinessPolicy:
    startup_seconds: float = 90.0
    window_stable_seconds: float = 2.0
    department_minimum_seconds: float = 5.0
    department_stable_seconds: float = 2.0
    poll_seconds: float = 0.25
    post_change_seconds: float = 30.0
    completion_seconds: float = 120.0
    completion_poll_seconds: float = 0.2
    home_scan_interval_seconds: float = 2.0
    worker_seconds: float = 420.0
    workflow_seconds: float = 425.0

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in vars(self).values()):
            raise WDMSError('LOGIN_POLICY_INVALID', '登录就绪等待时限必须是有限正数。')


DEFAULT_LOGIN_READINESS = LoginReadinessPolicy()
