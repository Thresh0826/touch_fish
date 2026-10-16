"""Serial date/task orchestration. No GUI, OS APIs, implicit retries or login."""
from __future__ import annotations

import re
import time
from pathlib import Path

from core.step_executor import StepContext
from errors import WDMSError
from moyu_tools.files import matches, publish, wait_file
from moyu_tools.direct_copy import destination, target_matches, wait_target
from moyu_tools.model import fail, zip_name


class Engine:
    def __init__(self, plan, policy, store, query, remote, *, cancelled=lambda: False,
                 emit=lambda *a, **k: None, clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 on_context=lambda seconds: None):
        self.plan, self.policy, self.store = plan, policy, store
        self.query, self.remote = query, remote
        self.cancelled, self.emit = cancelled, emit
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.force_trial_io = False
        self.on_context = on_context

    def context(self, seconds):
        self.on_context(seconds)
        return StepContext(self.clock() + seconds, self.cancelled, self.clock)

    def await_query(self, index, segment, context):
        row = self.store.value["segments"][index]
        saw_busy = row.get("busy_seen", False)
        previous = None
        stable_since = None
        # Wall-clock changes must not shorten the 30s minimum. Across restarts,
        # only a persisted observation can waive a new full initial wait.
        waited_before = min(row.get("initial_wait_seconds", 0), self.policy.initial_wait)
        started = self.clock() - waited_before
        while True:
            context.check()
            signal = self.query.signal(segment, context)
            context.check()
            if signal["busy"]:
                if not saw_busy:
                    saw_busy = True
                    self.store.update(index, "querying", busy_seen=True)
                    self.emit("query_busy_observed", segment=index)
                stable_since = None
            elif not signal["responsive"]:
                stable_since = None
            elif saw_busy and self.clock() - started >= self.policy.initial_wait:
                if row.get("initial_wait_seconds", 0) < self.policy.initial_wait:
                    self.store.update(index, "querying", initial_wait_seconds=self.policy.initial_wait)
                try:
                    snapshot = self.query.snapshot(segment, context)
                except WDMSError as error:
                    if error.code != "MOYU_READ_PENDING":
                        raise
                    stable_since = None
                    self.sleep(min(self.policy.poll, context.remaining()))
                    continue
                context.check()
                count, number = snapshot["count"], snapshot.get("number")
                if type(count) is not int or count < 0:
                    fail("MOYU_COUNT_INVALID", "本段行数未能完整读取。")
                if count > 1:
                    fail("MOYU_MULTIPLE_ROWS", "本段出现多条记录，与单条查询约定不符；没有漏选或继续复制。")
                if count == 1 and (not isinstance(number, str) or
                                   not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", number)):
                    fail("MOYU_NUMBER_INVALID", "送货单号不是完整可确认的单个值，未搜索。")
                key = (count, number if count else None)
                if key != previous or stable_since is None:
                    previous, stable_since = key, self.clock()
                elif self.clock() - stable_since >= self.policy.settle:
                    try:
                        self.query.confirm(segment, context)
                        self.query.guard_conditions(context)
                        context.check()
                        final_signal = self.query.signal(segment, context)
                        if final_signal["busy"] or not final_signal["responsive"]:
                            stable_since = None
                            continue
                        final = self.query.snapshot(segment, context)
                    except WDMSError as error:
                        if error.code != "MOYU_READ_PENDING":
                            raise
                        stable_since = None
                        continue
                    if (final["count"], final.get("number") if final["count"] else None) != key:
                        stable_since, previous = None, None
                        continue
                    context.check()
                    self.store.update(index, "queried", count=count, number=key[1],
                                      query_binding=self.store.value["identity"]["binding"])
                    self.emit("query_confirmed", segment=index, count=count)
                    return
            else:
                stable_since = None
            self.sleep(min(self.policy.poll, context.remaining()))

    def query_segment(self, index, segment):
        row = self.store.value["segments"][index]
        context = self.context(self.policy.preparation if row["phase"] == "pending" else self.policy.query_timeout)
        self.query.attach(context)
        if row["phase"] == "pending":
            self.query.set_dates(segment, context)
            self.query.confirm(segment, context)
            context.check()
            self.store.update(index, "query_intent", busy_seen=False, initial_wait_seconds=0,
                              submitted_wall=self.wall())
            context = self.context(self.policy.query_timeout)
            self.query.click(context)
            # Observe immediately: do not sleep 30s before recording a fast busy cycle.
            signal = self.query.signal(segment, context)
            self.store.update(index, "querying", busy_seen=bool(signal["busy"]))
        else:
            self.query.confirm(segment, context)
            signal = self.query.signal(segment, context)
            if not row.get("busy_seen") and not signal["busy"] and signal["responsive"]:
                fail("MOYU_QUERY_UNCERTAIN", "上次查询是否提交无法证明；请等WDMS结束后使用“确认重查当前段”，未自动重发。")
            if signal["busy"]:
                self.store.update(index, "querying", busy_seen=True)
        self.await_query(index, segment, context)

    def collect_file(self, index):
        if self.store.value["identity"].get("copy_mode") == "direct":
            return self.collect_direct(index)
        row = self.store.value["segments"][index]
        folder = self.store.stage_folder(index)
        output = Path(self.store.value["identity"]["output"])
        copy_context = None
        if row["phase"] in ("queried", "searching", "selected"):
            known = self.store.receipt(row["number"])
            if known is not None and not self.force_trial_io:
                if not matches(output/known["name"], known, self.context(self.policy.copy_timeout).check):
                    fail("MOYU_COMPLETED_CHANGED", "本任务以前复制的文件已变化，停止且不盲目跳过。")
                self.store.update(index, "done", file=known, reused=True)
                return
            folder.mkdir(parents=True, exist_ok=True)
            if folder.is_symlink() or list(folder.iterdir()):
                fail("MOYU_STAGING_EXISTS", "尚未发送本轮复制，暂存目录却已有文件；保留并停止。")
            self.store.update(index, "searching")
            context = self.context(self.policy.search_timeout)
            selected = self.remote.search(row["number"], context)
            context.check()
            zip_name(selected["name"])
            if selected.get("size") is not None and (type(selected["size"]) is not int or selected["size"] < 0):
                fail("MOYU_CLIPBOARD_INVALID", "文件剪贴板大小无效，未粘贴。")
            self.store.update(index, "selected", selected=selected,
                              source_binding=self.store.value["identity"]["binding"])
            # Persist intent before opening Explorer or issuing Ctrl+V. An ambiguous
            # crash is reconciled by inspecting this folder, never by pasting again.
            self.store.update(index, "copy_intent")
            copy_context = self.context(self.policy.copy_timeout)
            self.remote.paste(folder, selected, copy_context)
            self.store.update(index, "copying")
        if row["phase"] in ("copy_intent", "copying"):
            folder.mkdir(parents=True, exist_ok=True)
            selected = row["selected"]
            copy_context = copy_context or self.context(self.policy.copy_timeout)
            context = copy_context
            receipt = wait_file(folder, selected["name"], selected.get("size"), context,
                                clock=self.clock, sleep=self.sleep, stable_seconds=self.policy.file_settle)
            self.store.update(index, "verified", file=receipt)
        if row["phase"] == "verified":
            context = copy_context or self.context(self.policy.copy_timeout)
            receipt = row["file"]
            # A verified receipt saved before publication permits reconciliation
            # of a crash immediately after the exclusive move/link.
            if (output/receipt["name"]).exists() and not (folder/receipt["name"]).exists():
                if not matches(output/receipt["name"], receipt, context.check):
                    fail("MOYU_NAME_CONFLICT", "目标同名文件与本任务凭证不符，未覆盖。")
            else:
                existing = row.get("publication_receipt")
                if existing is None:
                    if (output/receipt["name"]).exists():
                        known_file = self.store.file_receipt(receipt["name"])
                        if known_file == receipt and matches(output/receipt["name"], receipt, context.check):
                            self.store.update(index, "done", reused=True)
                            return
                        fail("MOYU_NAME_CONFLICT", "目标已有同名文件且无一致的本任务凭证，未覆盖。")
                    self.store.update(index, "verified", publication_receipt=receipt)
                publish(folder, output, receipt, row.get("publication_receipt"), context.check)
            self.store.update(index, "done")
            self.emit("file_completed", segment=index, size=receipt["size"])

    def collect_direct(self, index):
        row = self.store.value["segments"][index]
        output = Path(self.store.value["identity"]["output"])
        if row["phase"] in ("queried", "searching", "selected"):
            known = self.store.receipt(row["number"])
            if known is not None and not self.force_trial_io:
                if not target_matches(output/known["name"], known, self.context(self.policy.copy_timeout).check):
                    fail("MOYU_COMPLETED_CHANGED", "本任务已复制的文件发生变化，未跳过。")
                self.store.update(index, "done", file=known, reused=True)
                return
            self.store.update(index, "searching")
            context = self.context(self.policy.search_timeout)
            selected = self.remote.search(row["number"], context)
            zip_name(selected["name"])
            if selected.get("size") is not None and (type(selected["size"]) is not int or selected["size"] < 0):
                fail("MOYU_CLIPBOARD_INVALID", "所选文件大小未确认，未粘贴。")
            self.store.update(index, "selected", selected=selected, source_binding=self.store.value["identity"]["binding"])
            target = destination(output, selected["name"])
            if target.exists() or target.is_symlink():
                known = self.store.file_receipt(selected["name"])
                if known is not None and target_matches(target, known, context.check):
                    self.store.update(index, "done", file=known, reused=True,
                                      source_binding=None if self.force_trial_io else row["source_binding"])
                    return
                fail("MOYU_NAME_CONFLICT", "目标已有同名文件，已暂停；没有覆盖或继续粘贴。")
            self.store.update(index, "copy_intent", destination_absent=True)
            context = self.context(self.policy.copy_timeout)
            self.remote.paste(output, selected, context)
            self.store.update(index, "copying")
        else:
            context = self.context(self.policy.copy_timeout)
        if row["phase"] in ("copy_intent", "copying"):
            if row.get("destination_absent") is not True:
                fail("MOYU_COPY_UNCERTAIN", "缺少本轮目标文件状态记录，未重新粘贴。")
            receipt = wait_target(output/row["selected"]["name"], row["selected"].get("size"), context,
                                  clock=self.clock, sleep=self.sleep, stable_seconds=self.policy.file_settle)
            self.store.update(index, "done", file=receipt)
            self.emit("file_completed", segment=index, size=receipt["size"])

    def run(self, *, trial=False):
        value = self.store.value
        if not trial and not value["trial_passed"]:
            fail("MOYU_TRIAL_REQUIRED", "请先通过一段查询和一份ZIP的单段试跑，再运行完整范围。")
        self.force_trial_io = trial and not value["trial_passed"]
        value.update(status="running", error=None)
        self.store.commit()
        try:
            for index, segment in enumerate(self.plan.segments()):
                self.context(self.policy.preparation).check()
                row = value["segments"][index]
                if row.get("rearm_pending"):
                    fail("MOYU_RECOVERY_PENDING", "人工恢复尚未完成，请确认上次操作结束后再次使用恢复按钮；未自动重查或重贴。")
                if row["phase"] in ("done", "empty"):
                    checker = target_matches if value["identity"].get("copy_mode") == "direct" else matches
                    if row["phase"] == "done" and not checker(
                            Path(value["identity"]["output"])/row["file"]["name"], row["file"],
                            self.context(self.policy.copy_timeout).check):
                        fail("MOYU_COMPLETED_CHANGED", "已完成文件丢失或变化，未跳过。")
                    if (trial and row["phase"] == "done" and not value["trial_passed"]
                            and row.get("query_binding") == value["identity"]["binding"]
                            and row.get("source_binding") == value["identity"]["binding"]):
                        # Recover a crash after the file receipt but before the trial gate.
                        value.update(trial_passed=True, status="trial_passed")
                        self.store.commit()
                        return "trial_passed"
                    continue
                self.emit("segment_started", segment=index, total=len(value["segments"]), phase=row["phase"])
                if row["phase"] in ("pending", "query_intent", "querying"):
                    self.query_segment(index, segment)
                if row["phase"] == "queried" and row["count"] == 0:
                    self.store.update(index, "empty")
                else:
                    self.collect_file(index)
                if trial:
                    fresh = (row["phase"] == "done" and row.get("query_binding") == value["identity"]["binding"]
                             and row.get("source_binding") == value["identity"]["binding"])
                    value["trial_passed"] = fresh or value["trial_passed"]
                    value["status"] = ("trial_passed" if value["trial_passed"] else
                                       "trial_empty" if row["phase"] == "empty" else "trial_pending")
                    self.store.commit()
                    return value["status"]
            value["status"] = "completed"
            self.store.commit()
            return "completed"
        except WDMSError as error:
            self.store.pause(error)
            self.emit("task_paused", error_code=error.code, error=error.message)
            raise
