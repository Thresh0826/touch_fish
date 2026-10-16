"""One-click development workflow with verified stage boundaries."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence
from core.collection_contracts import ProgressNotice

from core.step_executor import Step, StepExecutor
from desktop_demo import login, navigation, owner_discovery
from desktop_demo.settings import load_settings, settings_path, validate_executable, validate_output
from errors import WDMSError
from scripts import check_wdms_window as probe
from scripts import run_native_trial as trial
from services.task_lock import TaskLock
from services import owner_catalog
from core.query_policy import DEFAULT_QUERY_POLICY
from core.collection_contracts import CollectionRequest
from core import native_workflow
from core.collection_contracts import RunContext
from services.stage_invocation import invoke_stage
from services.workflow_evidence import stage_events, stage_failure
from services.workflow_evidence import copy_collection_evidence as archive_evidence

validated_owners = native_workflow.validated_owners
validate_limit = native_workflow.validate_limit
call_runner = native_workflow.call_runner


def copy_collection_evidence(source: Path, destination: Path, log: Path) -> None:
    return archive_evidence(source, destination, log, emit=probe.record)


def run_stages(folder: Path, output: Path, limit: int, notice: ProgressNotice, *,
               cancel_file: Optional[Path] = None, owners: Optional[Sequence[str]] = None,
               config=None, report_date: Optional[date] = None) -> Path:
    dependencies = native_workflow.WorkflowDependencies(
        lambda request: invoke_stage(login.main, request),
        lambda request: invoke_stage(navigation.main, request),
        lambda request: invoke_stage(owner_discovery.main, request),
        lambda request: trial.run_collection(request),
        lambda: load_settings(), probe.record)
    context = RunContext(Path(folder).name, report_date or date.today())
    return native_workflow.run_stages(folder, output, limit, notice, dependencies, context,
                                     cancel_file=cancel_file, owners=owners, config=config)


WORKFLOW_REVISION = "one-click-steps-v7.5-network-login-wait"
DEFAULT_OWNERS_PATH = trial.ROOT / "scripts" / "native_trial_owners.json"
FATAL_STAGE_EVENTS = {"probe_error", "probe_timeout", "probe_interrupted", "probe_start_failed", "probe_cleanup_failed"}
FATAL_COLLECTION_EVENTS = FATAL_STAGE_EVENTS | {"trial_worker_failed", "trial_cancelled", "trial_timeout",
                                               "trial_report_failed", "trial_manifest_cleanup_failed"}


@dataclass(frozen=True)
class WorkflowInputs:
    output: Path
    owners: Optional[tuple[str, ...]] = None


def read_owners(path: Path) -> tuple[str, ...]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict):
            raise ValueError("owners must be an object")
        return validated_owners(document.get("owners"))
    except WDMSError:
        raise
    except FileNotFoundError as error:
        raise WDMSError("WORKFLOW_OWNERS_MISSING", "找不到真实scripts/native_trial_owners.json，请保留原现场名单；未启动WDMS。") from error
    except (OSError, UnicodeError, ValueError) as error:
        raise WDMSError("WORKFLOW_OWNERS_INVALID", "货主名单无法完整读取；未启动WDMS。") from error


def preflight(config, owners_path: Optional[Path], limit: int, log: Path) -> WorkflowInputs:
    """Check all local prerequisites before any WDMS interaction."""
    validate_limit(limit)
    probe.record(log, "workflow_preflight_started")
    probe.require_usable_environment(probe.inspect_environment())
    try:
        validate_executable(config.wdms_exe)
    except (AttributeError, ValueError, OSError) as error:
        raise WDMSError("WORKFLOW_EXECUTABLE_INVALID", "配置的WDMS程序不存在或不是.exe；未启动WDMS。") from error
    names = read_owners(owners_path) if owners_path is not None else None
    try:
        from services import trial_excel_reporter as trial_excel_reporter
    except ImportError as error:
        raise WDMSError("WORKFLOW_REPORT_DEPENDENCY_MISSING", "报表依赖不可用，请检查现有Python环境；未启动WDMS。") from error
    try:
        output = Path(validate_output(config.output_dir)).resolve()
    except (AttributeError, ValueError, OSError) as error:
        raise WDMSError("WORKFLOW_OUTPUT_INVALID", "报表目录不可写；未启动WDMS。") from error
    probe.record(log, "workflow_preflight_complete", owner_count=len(names) if names is not None else None,
                 selected_count=limit, owners_source="explicit" if names is not None else "wdms")
    return WorkflowInputs(output, names)


def verify_report(report: Path, expected_owners: Sequence[str], report_date: Optional[date] = None) -> None:
    from services.trial_excel_reporter import verify_report as verify
    return verify(report, expected_owners, report_date or date.today())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="一键登录、导航、库存采集并保存开发测试表。")
    parser.add_argument("--limit", type=int, choices=(3, 24), default=24)
    parser.add_argument("--out", type=Path, default=settings_path().parent / "workflow_logs")
    parser.add_argument("--cancel-file", type=Path, default=None)
    parser.add_argument("--owners", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    folder = args.out.resolve() / f"flow_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    log = folder / "flow.jsonl"
    cancel_path = args.cancel_file.resolve() if args.cancel_file is not None else folder / "cancel.request"
    probe.record(
        log,
        "automatic_workflow_start",
        revision=WORKFLOW_REVISION,
        owner_count=args.limit,
        cancel_file=str(cancel_path),
        folder=folder.name,
    )

    report = None
    failure = None
    notice = None
    cleanup_error = None
    try:
        with TaskLock(settings_path().parent / "workflow.lock"):
            try:
                config = load_settings()
                if config is None:
                    raise WDMSError("SETTINGS_MISSING", "请先保存本机配置。")
                if cancel_path.exists():
                    raise WDMSError("TASK_CANCELLED", "任务已取消，未启动WDMS。")
                inputs = preflight(config, args.owners, args.limit, log)
                notice = trial.RunningNotice(folder)
                notice.start()
                report = run_stages(folder, inputs.output, args.limit, notice,
                                    cancel_file=cancel_path, owners=inputs.owners, config=config)
            finally:
                if notice is not None:
                    try:
                        notice.close()
                    except Exception:
                        cleanup_error = WDMSError("WORKFLOW_NOTICE_CLEANUP_FAILED", "运行提示未能正常关闭，请查看本轮记录。")
    except KeyboardInterrupt:
        error = WDMSError("TASK_CANCELLED", "任务收到中断，已停止。")
        info = probe.error_info(error)
        probe.record(log, "probe_error", **info)
        failure = info.get("error", "执行未完成，请查看日志。")
    except Exception as error:
        info = probe.error_info(error)
        probe.record(log, "probe_error", **info)
        failure = info.get("error", "执行未完成，请查看日志。")

    if cleanup_error is not None:
        probe.record(log, "workflow_notice_cleanup_failed", **probe.error_info(cleanup_error))
        if failure is None:
            failure = cleanup_error.message
            if report is not None:
                failure += f"\n测试表已保存，仍需人工核对：{report}"
    if failure:
        failed = [event for event in probe.read_events(log) if event.get("event") == "step_failed"]
        if failed:
            failure = f"停止阶段：{failed[-1]['step']}（{failed[-1]['error_code']}）\n{failure}"
        trial.show_notice(
            "自动流程已停止",
            f"{failure}\n\n未继续后续步骤。现在可以操作鼠标键盘。\n日志目录：{folder}",
            failure=True,
        )
        return 2
    trial.show_notice(
        "自动流程已完成",
        f"已完成 {args.limit} 个货主，表格已保存。\n\n{report}\n\n现在可以操作鼠标键盘。开发测试表仍需人工核对。",
    )
    return 0


# Compatibility exports used by staged diagnostics and field scripts.
__all__ = ['CollectionRequest', 'DEFAULT_QUERY_POLICY', 'Step', 'StepExecutor', 'WorkflowInputs', 'copy_collection_evidence', 'main', 'owner_catalog', 'preflight', 'read_owners', 'run_stages', 'shutil', 'stage_events', 'stage_failure', 'verify_report', 'WORKFLOW_REVISION', 'DEFAULT_OWNERS_PATH', 'FATAL_STAGE_EVENTS', 'FATAL_COLLECTION_EVENTS']
