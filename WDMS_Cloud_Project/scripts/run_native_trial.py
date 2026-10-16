"""Staged native WDMS trial. Outputs manual-review data, never a formal report."""
from __future__ import annotations
import argparse
import ctypes
import faulthandler
import math
import subprocess
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import check_wdms_window as probe
from errors import WDMSError
from services import owner_catalog
from services.worker_commands import native_trial_command as trial_command, worker_command
from adapters import native_windows
from adapters.native_windows import TrialNativeWindows
from desktop_demo.notices import running_notice_window, RunningNotice, ManagedNotice, show_notice
from core import native_collection, native_batch
from core.query_policy import QueryPolicy, DEFAULT_QUERY_POLICY
from core.collection_contracts import WorkerJob, CollectionJob, CollectionRequest, RunContext
from services import native_worker, native_results


def combo_send(api, hwnd, message, parameter=0):
    return native_windows.combo_send(api, hwnd, message, parameter)


def select_owner(api, main, combo, index, expected, log):
    return native_windows.select_owner(api, main, combo, index, expected, log, send=combo_send)


TERMINAL_WORKER_EVENTS = {'probe_error', 'probe_timeout', 'probe_interrupted',
                          'probe_start_failed', 'probe_cleanup_failed', 'trial_worker_failed'}
IDLE_WAIT_SECONDS = DEFAULT_QUERY_POLICY.idle_seconds


def wait_until_idle(session, log, owner, limit_seconds=DEFAULT_QUERY_POLICY.idle_seconds, *, check=lambda: None):
    return native_collection.wait_until_idle(session, owner, limit_seconds,
        emit=lambda event, **data: probe.record(log, event, **data),
        clock=time.monotonic, sleep=time.sleep, check=check)


def child(args):
    if getattr(args, "_child", False):
        faulthandler.enable()
    log = Path(args.log)
    job = WorkerJob(
        worker_id=getattr(args, "worker_id", None) or uuid.uuid4().hex,
        run_id=getattr(args, "run_id", None) or uuid.uuid4().hex,
        index=getattr(args, "index", 0), owners=Path(args.owners),
        owners_sha256=getattr(args, "owners_sha256", None) or "",
        log=log, result=Path(getattr(args, "worker_result", None) or log.with_suffix(".result.json")),
        stderr=log.with_suffix(".stderr.txt"), title=getattr(args, "title", "医药企业仓储运输管理系统"),
        policy=QueryPolicy(query_seconds=getattr(args, "query_timeout", 90)),
        deadline=getattr(args, "execution_deadline", None),
        cancel_file=Path(args.cancel_file) if getattr(args, "cancel_file", None) else None,
    )
    client = native_windows.NativeTrialClient(log, job.title, api_factory=TrialNativeWindows,
        scanner=probe.check_native_controls, selector=select_owner,
        message_policy=job.policy.messages,
        environment_check=lambda: probe.require_usable_environment(probe.inspect_environment()))
    return native_worker.run_owner_worker(job, client, emit=probe.record)


def write_trial_excel(rows, path, task_date=None):
    from services.trial_excel_reporter import write_trial_excel as publish_trial_excel

    return publish_trial_excel(rows, path, task_date=task_date)


def report_title(task_date=None):
    """《x年x月x日物流库存》：工作表名与结果文件名共用同一标题。"""
    from services.trial_excel_reporter import inventory_report_title

    return inventory_report_title(task_date or date.today())


def main(argv=None):
    parser=argparse.ArgumentParser(description='按现场名单依次测试货主；输出待人工核对的测试表，不发布正式报表。')
    parser.add_argument('--limit', type=int, choices=(1,3,24), default=3)
    parser.add_argument('--out', type=Path, default=ROOT/'test_logs')
    parser.add_argument('--owners', type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--owners-sha256', help=argparse.SUPPRESS)
    parser.add_argument('--title', default='医药企业仓储运输管理系统')
    parser.add_argument('--query-timeout', type=int, choices=range(30,121), default=90)
    parser.add_argument('--_notice', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--notice-stop', help=argparse.SUPPRESS)
    parser.add_argument('--notice-ready', help=argparse.SUPPRESS)
    parser.add_argument('--notice-progress', help=argparse.SUPPRESS)
    parser.add_argument('--_child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--index', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--log', help=argparse.SUPPRESS)
    parser.add_argument('--worker-result', help=argparse.SUPPRESS)
    parser.add_argument('--worker-id', help=argparse.SUPPRESS)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    parser.add_argument('--report-date', type=date.fromisoformat, help=argparse.SUPPRESS)
    parser.add_argument('--managed-notices',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--workflow-progress',help=argparse.SUPPRESS)
    parser.add_argument('--result-manifest',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--cancel-file',type=Path,default=None)
    parser.add_argument('--execution-deadline',type=float,default=None)
    args=parser.parse_args(argv)
    if args.execution_deadline is not None and not math.isfinite(args.execution_deadline):
        parser.error('--execution-deadline 必须是有限的 monotonic 绝对期限。')
    if args._notice:
        return running_notice_window(args)
    if args._child:
        return child(args)
    request = CollectionRequest(args.limit, args.out, args.report_date or date.today(),
        args.run_id or uuid.uuid4().hex, args.owners, args.owners_sha256, args.title,
        args.query_timeout, args.managed_notices,
        Path(args.workflow_progress) if args.workflow_progress else None,
        args.result_manifest, args.cancel_file, args.execution_deadline)
    return run_collection(request)


def run_collection(request):
    args = request
    folder=args.out.resolve()/f'native_trial_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}'
    folder.mkdir(parents=True)
    try:
        try:
            from services import trial_excel_reporter as trial_excel_reporter
        except ImportError as error:
            raise WDMSError('WORKFLOW_REPORT_DEPENDENCY_MISSING', '缺少报表依赖openpyxl，未开始WDMS操作；请检查已有Python环境。') from error
        if args.owners is None:
            from desktop_demo import owner_discovery
            from desktop_demo.settings import load_settings
            config=load_settings()
            if config is None:raise WDMSError('SETTINGS_MISSING','请先保存本机配置。')
            owners=folder/'owners_snapshot.json'
            capture_args=['--out',str(folder/'owners'),'--snapshot',str(owners)]
            if args.cancel_file is not None:capture_args+=['--cancel-file',str(args.cancel_file)]
            if args.execution_deadline is not None:capture_args+=['--execution-deadline',str(args.execution_deadline)]
            if owner_discovery.main(capture_args)!=0:
                raise WDMSError('OWNER_CAPTURE_FAILED','货主名单读取未完成，未开始查询；详情见本轮owners记录。')
            names,digest=owner_catalog.prepare_collection(owners,config,
                cancelled=lambda:args.cancel_file is not None and args.cancel_file.exists())
        else:
            owners=args.owners.resolve()
            unused,names,digest=owner_catalog.read_plan(owners,args.owners_sha256)
    except Exception as error:
        probe.record(folder/'trial_error.jsonl','probe_error',**probe.error_info(error))
        show_notice('库存采集已停止',f'名单未完成读取或校验，未开始查询。\n{getattr(error,"message","请查看本轮记录。") }\n日志目录：{folder}',failure=True)
        return 2
    notice=ManagedNotice(args.workflow_progress) if args.managed_notices else RunningNotice(folder)
    try:
        notice.start()
        return run_batch(args,folder,owners,names,notice,owners_sha256=digest)
    except Exception as error:
        info = probe.error_info(error)
        info.setdefault('error', f'采集未完成（{type(error).__name__}），详情见本轮记录。')
        probe.record(folder/'trial_error.jsonl','probe_error',**info)
        notice.close()
        message = f'{info["error"]}\n没有重试查询。日志目录：{folder}'
        if args.managed_notices:
            print(f'库存采集已停止：{message}',flush=True)
        else:
            show_notice('库存采集已停止',message,failure=True)
        return 2
    finally:
        notice.close()


def run_batch(args, folder, owners, names, notice, *, owners_sha256=None):
    context = RunContext(getattr(args, "run_id", None) or folder.name,
                         getattr(args, "report_date", None) or date.today())
    job = CollectionJob(context, folder, Path(owners), owners_sha256 or args.owners_sha256, tuple(names),
                        args.limit, args.title, QueryPolicy(query_seconds=args.query_timeout),
                        getattr(args, "result_manifest", None), getattr(args, "cancel_file", None),
                        getattr(args, "execution_deadline", None), getattr(args, "managed_notices", False))
    return collect(job, notice).exit_code


def collect(job, notice):
    def run_worker(worker, timeout, cancelled=None):
        command = worker_command(worker)
        keywords = {"stderr_path": worker.stderr}
        if cancelled is not None:
            keywords["cancelled"] = cancelled
        return probe.supervise(command, worker.log, timeout, **keywords)
    dependencies = native_batch.CollectionDependencies(run_worker, probe.record, probe.read_events,
                                                        write_trial_excel, show_notice)
    return native_batch.execute_collection(job, dependencies, notice)

if __name__=='__main__':
    raise SystemExit(main())


# Compatibility exports used by staged diagnostics and field scripts.
__all__ = ['child', 'collect', 'combo_send', 'ctypes', 'main', 'native_results', 'report_title', 'run_batch', 'run_collection', 'select_owner', 'subprocess', 'trial_command', 'wait_until_idle', 'write_trial_excel']
