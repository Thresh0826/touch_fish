"""Small cooperative step runner; blocking actions need supervised hard timeouts.

Deadlines are checked between synchronous actions and verification callbacks.
They cannot interrupt a blocking call; native actions must use the existing
supervised child-process boundary for a hard timeout. This module never starts
threads, kills threads, or retries an action.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from errors import WDMSError


@dataclass(frozen=True)
class Step:
    name: str
    action: Callable[["StepContext"], Any]
    verify: Callable[[Any], bool]
    timeout_seconds: float


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str
    value: Any
    elapsed_seconds: float


@dataclass(frozen=True)
class StepContext:
    deadline: float
    cancelled: Callable[[], bool]
    clock: Callable[[], float]

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def check(self) -> None:
        if self.cancelled():
            raise WDMSError("TASK_CANCELLED", "任务已取消。")
        if self.clock() >= self.deadline:
            raise WDMSError("STEP_TIMEOUT", "步骤已超过协作期限。")


class StepExecutor:
    def __init__(self, cancelled=None, emit=None, clock=time.monotonic):
        self.cancelled = cancelled if cancelled is not None else (lambda: False)
        self.emit = emit
        self.clock = clock

    @staticmethod
    def _validated_steps(steps: Iterable[Step]) -> tuple:
        try:
            items = tuple(steps)
        except Exception as error:
            raise WDMSError("STEP_INVALID", "步骤列表无效。") from error

        names = set()
        for step in items:
            if not isinstance(step, Step):
                raise WDMSError("STEP_INVALID", "步骤定义无效。")
            if not isinstance(step.name, str) or not step.name or step.name in names:
                raise WDMSError("STEP_INVALID", "步骤名称无效或重复。")
            names.add(step.name)
            try:
                valid_timeout = (
                    not isinstance(step.timeout_seconds, bool)
                    and math.isfinite(step.timeout_seconds)
                    and step.timeout_seconds > 0
                )
            except (TypeError, ValueError, OverflowError):
                valid_timeout = False
            if not valid_timeout:
                raise WDMSError("STEP_INVALID", "步骤期限必须是有限正数。")
            if not callable(step.action) or not callable(step.verify):
                raise WDMSError("STEP_INVALID", "步骤动作和验证函数必须可调用。")
        return items

    @staticmethod
    def _failure_status(error: WDMSError) -> str:
        if error.code == "TASK_CANCELLED":
            return "cancelled"
        if error.code == "STEP_TIMEOUT":
            return "timed_out"
        return "failed"

    def run(self, steps: Iterable[Step]) -> list:
        items = self._validated_steps(steps)
        results = []

        for step in items:
            started = self.clock()
            deadline = started + float(step.timeout_seconds)
            context = StepContext(deadline, self.cancelled, self.clock)
            callback_failed = False

            def elapsed() -> float:
                return max(0.0, self.clock() - started)

            def emit(event: str, status: str, error_code=None) -> None:
                nonlocal callback_failed
                if self.emit is None:
                    return
                fields = {
                    "step": step.name,
                    "status": status,
                    "elapsed_seconds": elapsed(),
                    "error_code": error_code,
                }
                try:
                    self.emit(event, fields)
                except KeyboardInterrupt as error:
                    callback_failed = True
                    raise WDMSError("TASK_CANCELLED", "步骤状态记录被中断。") from error
                except Exception as error:
                    callback_failed = True
                    raise WDMSError("STEP_EVENT_FAILED", "步骤状态记录失败。") from error

            try:
                context.check()
                emit("step_started", "running")
                value = step.action(context)
                context.check()
                confirmed = step.verify(value)
                context.check()
                if confirmed is not True:
                    raise WDMSError("STEP_UNCONFIRMED", "步骤结果未获确认。")
                emit("step_succeeded", "succeeded")
            except KeyboardInterrupt as error:
                failure = WDMSError("TASK_CANCELLED", "任务收到中断，已停止。")
                failure.__cause__ = error
            except WDMSError as error:
                failure = error
            except Exception as error:
                failure = WDMSError(
                    "STEP_FAILED",
                    "步骤 {} 执行失败（{}）。".format(step.name, type(error).__name__),
                )
                failure.__cause__ = error
            else:
                results.append(StepResult(step.name, "succeeded", value, elapsed()))
                continue

            if not callback_failed:
                try:
                    emit("step_failed", self._failure_status(failure), failure.code)
                except WDMSError as callback_error:
                    # An event callback that fails is not called again.
                    raise callback_error from failure
            raise failure

        return results
