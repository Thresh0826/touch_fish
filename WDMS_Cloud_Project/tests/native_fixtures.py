"""Synthetic worker receipts for orchestration tests; no Windows actions."""
from decimal import Decimal
from pathlib import Path
from unittest import mock

from core.collection_contracts import WorkerJob
from core.native_collection import InventoryReading
from core.query_policy import QueryPolicy
from services.native_results import write_worker_result


def isolate_trial_notices(test_case):
    """Synthetic batches never launch a native banner or a modal result box.

    Tests of the notice implementation use their own OS/process boundaries;
    this fixture belongs only to synthetic collection orchestration tests.
    """
    from scripts import run_native_trial as trial
    replacements = []
    for name in ('RunningNotice', 'show_notice'):
        patcher = mock.patch.object(trial, name)
        replacements.append(patcher.start())
        test_case.addCleanup(patcher.stop)
    return tuple(replacements)


def job_from_command(command):
    def argument(name, default=None):
        return command[command.index(name) + 1] if name in command else default
    log = Path(argument("--log"))
    return WorkerJob(argument("--worker-id"), argument("--run-id"), int(argument("--index")),
                     Path(argument("--owners")), argument("--owners-sha256"), log,
                     Path(argument("--worker-result")), log.with_suffix(".stderr.txt"),
                     argument("--title"), QueryPolicy(query_seconds=int(argument("--query-timeout"))),
                     float(argument("--execution-deadline")) if "--execution-deadline" in command else None,
                     Path(argument("--cancel-file")) if "--cancel-file" in command else None)


def publish_completion(command, owner, whole="1", loose="2.54", total="3.54"):
    job = job_from_command(command)
    reading = InventoryReading(owner, int(whole), Decimal(loose), total)
    write_worker_result(job, reading=reading, stage="触发查询并读取结果")


def publish_failure(command, code, message):
    write_worker_result(job_from_command(command), failure={"error_code": code, "error": message},
                        stage="触发查询并读取结果")


def collection_args(request):
    """Keep old stage fixtures usable while testing the typed collection port."""
    if isinstance(request, list):
        return request
    args = ["--limit", str(request.limit), "--out", str(request.out),
            "--report-date", request.report_date.isoformat(), "--run-id", request.run_id]
    for option, value in (("--owners", request.owners), ("--owners-sha256", request.owners_sha256),
                          ("--workflow-progress", request.workflow_progress), ("--result-manifest", request.result_manifest),
                          ("--cancel-file", request.cancel_file), ("--execution-deadline", request.execution_deadline)):
        if value is not None:
            args += [option, str(value)]
    if request.managed_notices:
        args.append("--managed-notices")
    return args
