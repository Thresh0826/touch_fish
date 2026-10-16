"""Worker composition: load the plan, run core rules and publish one receipt."""
from __future__ import annotations

import json
import sys
import time

from core import native_collection
from core.step_executor import StepContext
from services import event_log, native_results, owner_catalog


def run_owner_worker(job, client, *, emit=event_log.record):
    stage = "读取名单快照"

    def record(event, **fields):
        nonlocal stage
        if event == "trial_stage_started":
            stage = fields["stage"]
        emit(job.log, event, **fields)

    try:
        record("trial_stage_started", stage=stage)
        document, names, unused = owner_catalog.read_plan(job.owners, job.owners_sha256 or None)
        deadline = job.deadline
        if deadline is None:
            deadline = time.monotonic() + job.policy.worker_seconds - job.policy.cleanup_seconds
        context = StepContext(deadline,
                              lambda: job.cancel_file is not None and job.cancel_file.exists(),
                              time.monotonic)
        client.request_guard = lambda: owner_catalog.read_plan(job.owners, job.owners_sha256 or None)
        reading = native_collection.collect_one(document, names, job.index, client, job.policy,
                                                context, emit=record, clock=time.monotonic, sleep=time.sleep)
        context.check()
        owner_catalog.read_plan(job.owners, job.owners_sha256 or None)
        native_results.write_worker_result(job, reading=reading, stage=stage)
        return 0
    except Exception as error:
        info = event_log.error_info(error)
        info.setdefault("error", f"{stage}失败（{type(error).__name__}），请查看本轮证据。")
        try:
            native_results.write_worker_result(job, failure=info, stage=stage)
        except Exception as receipt_error:
            print(json.dumps({"event": "worker_receipt_failed", "stage": stage,
                              **event_log.error_info(receipt_error)}, ensure_ascii=False), file=sys.stderr, flush=True)
        try:
            record("probe_error", **info)
        except OSError:
            print(json.dumps({"event": "probe_error", "stage": stage, **info}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
