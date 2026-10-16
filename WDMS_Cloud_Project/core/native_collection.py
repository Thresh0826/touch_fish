"""Native collection rules expressed through a session port, without Win32 or CLI."""
from __future__ import annotations
from typing import Optional

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from core.numbers import parse_nonnegative_integer, parse_nonnegative_quantity
from core.query_policy import DEFAULT_QUERY_POLICY
from core.step_executor import StepContext
from errors import WDMSError


class QuerySession(Protocol):
    def validate_controls(self): ...
    def owner(self): ...
    def counts(self): ...
    def cursor_state(self): ...
    def guard_cursor(self): ...
    def guard_observation(self): ...
    def button_enabled(self): ...
    def health(self, stage): ...
    def input_description(self, trigger): ...
    def prepare_input(self, owner, check): ...
    def send(self, trigger): ...
    def foreground_hwnd(self): ...


@dataclass(frozen=True)
class InventoryReading:
    owner: str
    whole: int
    loose: Decimal
    total_text: Optional[str] = None

    def __post_init__(self):
        if (not isinstance(self.owner, str) or not self.owner.strip()
                or type(self.whole) is not int or not isinstance(self.loose, Decimal)
                or self.total_text is not None and not isinstance(self.total_text, str)):
            raise WDMSError("TRIAL_RESULT_INVALID", "采集结果的货主或数值类型无效。")
        parse_nonnegative_integer(self.whole, "整件数")
        parse_nonnegative_quantity(self.loose, "散件数")

    def as_row(self):
        return {"owner": self.owner, "whole": str(self.whole), "loose": str(self.loose),
                "total": self.total_text, "requires_manual_review": True,
                "result_owner_independently_verified": False}


def count_key(values):
    return (parse_nonnegative_integer(values["whole"], "整件数"),
            parse_nonnegative_quantity(values["loose"], "散件数"))


def _settling_key(values):
    # An in-flight label may still be incomplete. Once stable, the final
    # sample must pass the strict parsers; it is never rounded or coerced.
    try:
        return count_key(values)
    except WDMSError as error:
        if error.code not in ("INVALID_COUNT", "COUNT_PRECISION_RISK"):
            raise
        return ("unparsed", values["whole"], values["loose"])


def wait_until_idle(session, owner, limit_seconds, *, emit, clock, sleep, check=lambda: None):
    started = None
    while True:
        check()
        session.guard_cursor()
        cursor = session.cursor_state()
        if cursor.get("cursor_visible") and not cursor["standard_wait_cursor"]:
            waited = 0.0 if started is None else clock() - started
            if started is not None:
                emit("query_idle_recovered", owner=owner, waited_seconds=round(waited, 1))
            check()
            session.guard_cursor()
            return waited
        if started is None:
            started = clock()
            emit("query_idle_wait", owner=owner, limit_seconds=limit_seconds)
        waited = clock() - started
        if waited >= limit_seconds:
            emit("query_idle_wait_timeout", owner=owner, waited_seconds=round(waited, 1))
            raise WDMSError("QUERY_ALREADY_RUNNING", "WDMS空闲状态持续未确认，停止且未发送查询。")
        sleep(min(0.2, limit_seconds - waited))


def wait_cursor_and_values(session, owner, deadline, *, emit, clock, sleep,
                           settle_seconds=5.0, check=lambda: None):
    saw_wait = False
    stable_since = None
    previous_cursor = None
    previous_values = None
    previous_key = None
    next_sample = 0.0
    while clock() < deadline:
        check()
        session.guard_cursor()
        cursor = session.cursor_state()
        waiting = cursor["standard_wait_cursor"]
        if cursor != previous_cursor:
            emit("query_cursor_state", **cursor)
            previous_cursor = cursor
        if waiting or not cursor.get("cursor_visible"):
            saw_wait = saw_wait or waiting
            stable_since = None
        if clock() >= next_sample:
            try:
                if session.owner() != owner:
                    raise WDMSError("QUERY_OWNER_MISMATCH", "查询期间选中货主变化。")
                values = session.counts()
                key = _settling_key(values)
                if values != previous_values:
                    emit("query_settle_values", **values)
                    previous_values = values
                if key != previous_key:
                    stable_since = None
                    previous_key = key
                if saw_wait and not waiting and cursor.get("cursor_visible"):
                    if stable_since is None:
                        stable_since = clock()
                    elif clock() - stable_since >= settle_seconds:
                        check()
                        session.guard_cursor()
                        emit("query_cursor_settled", stable_seconds=settle_seconds,
                             cursor_busy_idle_observed=True, result_owner_independently_verified=False)
                        return
            except WDMSError as error:
                if error.code != "NATIVE_TEXT_READ_FAILED":
                    raise
                stable_since = None
                emit("query_settle_read_unavailable", error_code=error.code, error=error.message,
                     winerror=getattr(error, "winerror", None), hwnd=getattr(error, "hwnd", None))
            next_sample = clock() + 0.5
        sleep(min(0.05, max(0.0, deadline - clock())))
    raise WDMSError("QUERY_CURSOR_NOT_SETTLED", "未在时限内观察到等待光标结束且读数稳定；没有重试查询。")


def query_current_owner(session, timeout, trigger, completion_signal, *, emit, clock, sleep,
                        expected_owner=None, check=lambda: None, policy=DEFAULT_QUERY_POLICY):
    check()
    session.validate_controls()
    owner = session.owner()
    if not isinstance(owner, str) or not owner.strip():
        raise WDMSError("OWNER_SELECTION_FAILED", "当前货主为空，未查询。")
    if expected_owner is not None and owner != expected_owner:
        raise WDMSError("QUERY_OWNER_MISMATCH", "查询前货主与本轮目标不一致，未查询。")
    if not session.button_enabled():
        raise WDMSError("QUERY_ALREADY_RUNNING", "查询按钮当前不可用，未发送新的查询。")
    session.health("before_single_query")
    emit("before_query_click", owner=owner, **session.input_description(trigger))
    deadline = clock() + timeout
    session.prepare_input(owner, check)
    check()
    session.send(trigger)
    emit("query_input_sent", owner=owner,
         action_strategy="sendinput_alt_f" if trigger == "alt-f" else "sendinput_button_center")
    saw_busy = False
    if completion_signal == "cursor":
        wait_cursor_and_values(session, owner, deadline, emit=emit, clock=clock, sleep=sleep,
                               settle_seconds=policy.settle_seconds, check=check)
    else:
        previous = None
        while clock() < deadline:
            check()
            session.guard_observation()
            enabled = session.button_enabled()
            if enabled != previous:
                emit("query_button_state", enabled=enabled)
                previous = enabled
            if not enabled:
                saw_busy = True
            elif saw_busy:
                break
            sleep(min(0.03, max(0.0, deadline - clock())))
        else:
            emit("query_observation_inconclusive", saw_disabled=saw_busy)
            raise WDMSError("QUERY_TRANSITION_NOT_OBSERVED", "未观察到查询按钮不可用后恢复；没有自动重试查询。")
    session.health("after_single_query")
    samples = []
    values = None
    for index in range(policy.sample_count):
        check()
        if clock() >= deadline:
            raise WDMSError("QUERY_OBSERVATION_TIMEOUT", "查询后读数超过观察时限。")
        if session.owner() != owner:
            raise WDMSError("QUERY_OWNER_MISMATCH", "观察期间选中货主变化，停止读取。")
        if completion_signal == "cursor":
            session.guard_cursor()
            cursor = session.cursor_state()
            if cursor["standard_wait_cursor"] or not cursor.get("cursor_visible"):
                raise WDMSError("QUERY_STATE_CHANGED", "最终采样期间空闲光标已变化，停止读取。")
        values = session.counts()
        samples.append(count_key(values))
        if not session.button_enabled():
            raise WDMSError("QUERY_STATE_CHANGED", "采样期间按钮再次不可用，停止观察。")
        emit("query_result_sample", index=index, owner=owner, whole=str(samples[-1][0]),
             loose=str(samples[-1][1]), total=values.get("total"))
        if index + 1 < policy.sample_count:
            sleep(min(policy.sample_interval, max(0.0, deadline-clock())))
    if any(sample != samples[0] for sample in samples[1:]):
        raise WDMSError("QUERY_VALUES_UNSTABLE", "整件数或散件数的连续采样不一致，需要人工核对。")
    reading = InventoryReading(owner, *samples[-1], total_text=values.get("total"))
    emit("single_query_observation_complete", **reading.as_row(), button_busy_idle_observed=saw_busy,
         cursor_busy_idle_observed=completion_signal == "cursor")
    return reading


def collect_one(document, expected_names, index, client, policy, context, *, emit, clock, sleep):
    """Drive one owner through bounded preparation, idle waiting and one query."""
    if type(index) is not int or not 0 <= index < len(expected_names):
        raise WDMSError("OWNER_INDEX_INVALID", "货主索引不在本轮名单范围内，未操作WDMS。")
    preparation = StepContext(min(context.deadline, clock() + policy.preparation_seconds),
                              context.cancelled, clock)
    client.bind_context(preparation)
    preparation.check()
    client.connect(document)
    emit("trial_stage_started", stage="定位采集控件")
    client.locate_controls(min(25.0, preparation.remaining()))
    preparation.check()
    emit("trial_stage_started", stage="读取实际货主名单")
    names = tuple(client.read_names())
    if names != tuple(expected_names) or len(names) != 24 or len(set(names)) != 24:
        raise WDMSError("OWNER_LIST_CHANGED", "现场货主清单与本轮24项名单不同，停止且未查询。")
    target = names[index]
    emit("trial_stage_started", stage="切换货主")
    preparation.check()
    client.select_owner(index, target)
    client.place_query_cursor()
    preparation.check()
    session = client.query_session(index)
    client.bind_context(context)
    wait_until_idle(session, target, policy.idle_seconds, emit=emit, clock=clock,
                    sleep=sleep, check=context.check)
    emit("trial_stage_started", stage="触发查询并读取结果")
    query_context = StepContext(min(context.deadline, clock() + policy.query_seconds),
                                context.cancelled, clock)
    client.bind_context(query_context)
    reading = query_current_owner(session, policy.query_seconds, "alt-f", "cursor", emit=emit,
                                  clock=clock, sleep=sleep, expected_owner=target,
                                  check=query_context.check, policy=policy)
    query_context.check()
    emit("trial_owner_complete", index=index, **reading.as_row())
    return reading


def observe_query_signals(session, duration, trigger, *, emit, clock, sleep):
    """Diagnostic timeline; does not assert query completion or result ownership."""
    session.validate_controls()
    owner = session.owner()
    if not isinstance(owner, str) or not owner.strip() or not session.button_enabled():
        raise WDMSError("QUERY_ALREADY_RUNNING", "货主为空或查询按钮不可用，未触发查询。")
    before = session.counts()
    emit("query_signal_baseline", owner=owner, **before, **session.cursor_state())
    session.health("before_signal_observation")
    session.prepare_input(owner, lambda:None)
    session.send(trigger)
    started = clock()
    previous = None
    previous_values = before
    next_values = started
    changed = False
    while clock() - started < duration:
        session.guard_observation()
        state = {"button_enabled":session.button_enabled(), "foreground_hwnd":session.foreground_hwnd(),
                 **session.cursor_state()}
        if state != previous:
            emit("query_signal_state", elapsed=round(clock()-started,3), **state)
            previous = state
        if clock() >= next_values:
            try:
                if session.owner() != owner:
                    raise WDMSError("QUERY_OWNER_MISMATCH", "观察期间选中货主变化。")
                values = session.counts()
                if values != previous_values:
                    emit("query_signal_values_changed", elapsed=round(clock()-started,3), **values)
                    changed = changed or values != before
                    previous_values = values
            except WDMSError as error:
                if error.code != "NATIVE_TEXT_READ_FAILED":
                    raise
                emit("query_signal_read_unavailable", error_code=error.code, error=error.message)
            next_values = clock() + 0.5
        sleep(0.05)
    emit("query_signal_observation_complete", owner=owner, duration=duration,
         values_changed=changed, last_observed_values=previous_values,
         query_completion_verified=False, requires_manual_review=True)
