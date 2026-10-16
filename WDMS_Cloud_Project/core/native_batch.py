"""Batch orchestration over worker, progress, diagnostics and report ports."""
from __future__ import annotations
from typing import Optional
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from core.collection_contracts import CollectionJob, ProgressNotice, WorkerJob
from errors import WDMSError
from services import native_results, owner_catalog
from services.event_log import error_info
from core.report_contract import inventory_report_title

TERMINAL_WORKER_EVENTS = {'probe_error', 'probe_timeout', 'probe_interrupted',
                          'probe_start_failed', 'probe_cleanup_failed', 'trial_worker_failed'}

@dataclass(frozen=True)
class BatchResult:
    exit_code: int
    owner_count: int
    report: Optional[Path] = None

@dataclass(frozen=True)
class CollectionDependencies:
    run_worker: Callable
    emit: Callable
    read_events: Callable
    write_excel: Callable
    notify: Callable
    clock: Optional[Callable] = None


def execute_collection(job: CollectionJob, dependencies: CollectionDependencies,
                       notice: ProgressNotice) -> BatchResult:
    clock = dependencies.clock or time.monotonic
    args = job
    folder, owners, names = job.folder, job.owners, job.names
    managed=getattr(args,'managed_notices',False)
    cancel_file=getattr(args,'cancel_file',None)
    cancel_path=Path(cancel_file).resolve() if cancel_file is not None else None
    deadline=job.deadline

    def notify(title,message,**kwargs):
        if managed:print(f'{title}：{message}',flush=True)
        else:dependencies.notify(title,message,**kwargs)

    def stop_reason():
        if cancel_path is not None and cancel_path.exists():
            return ('TASK_CANCELLED','已收到本轮取消请求。')
        if deadline is not None and clock()>=deadline:
            return ('STEP_TIMEOUT','本轮执行期限已到。')
        return None

    def stop_run(reason,checkpoint,report=None,manifest_published=False):
        nonlocal manifest_published_state
        error_code,message=reason
        event='trial_cancelled' if error_code=='TASK_CANCELLED' else 'trial_timeout'
        if manifest_published and getattr(args,'result_manifest',None) is not None:
            try:
                Path(args.result_manifest).unlink()
                manifest_published_state=False
            except FileNotFoundError:
                manifest_published_state=False
            except Exception:
                dependencies.emit(folder/'trial_error.jsonl','trial_manifest_cleanup_failed',
                             error_code='TRIAL_MANIFEST_CLEANUP_FAILED',checkpoint=checkpoint)
        dependencies.emit(folder/'trial_error.jsonl',event,error_code=error_code,error=message,checkpoint=checkpoint)
        notice.close()
        completed=len(rows)
        if report is None:
            result_text='未生成本轮完整测试表。'
        else:
            result_text=(f'测试表已保存：{report}\n'
                         '本轮结果未确认，不视为完整交付。')
        notify('库存采集已停止',
               f'已完成 {completed}/{args.limit} 个货主。\n原因：{message}\n{result_text}\n'
               f'现在可以操作鼠标键盘。日志目录：{folder}',failure=True)
        return BatchResult(2, len(rows), report)

    manifest_published_state=False
    rows=[]
    print('这是开发测试，数据需人工核对。请保持 WDMS 前台，不操作鼠标键盘。',flush=True)
    print(f'证据目录：{folder}',flush=True)
    for index in range(args.limit):
        reason=stop_reason()
        if reason:
            return stop_run(reason,'before_owner_start')
        notice.update(index+1,args.limit,names[index])
        print(f'[{index+1}/{args.limit}] 查询：{names[index]}',flush=True)
        log=folder/f'owner_{index+1:02d}.jsonl'
        stderr_path=folder/f'owner_{index+1:02d}.stderr.txt'
        dependencies.emit(log,'trial_owner_start',index=index,owner=names[index])
        timeout=job.policy.worker_seconds
        if deadline is not None:
            timeout=min(timeout,deadline-clock())
        worker = WorkerJob(uuid.uuid4().hex, job.context.run_id, index, owners, job.owners_sha256,
                           log, folder/f'owner_{index+1:02d}.result.json', stderr_path, job.title,
                           job.policy, min(clock() + job.policy.worker_seconds - job.policy.cleanup_seconds,
                                           deadline if deadline is not None else float('inf')),
                           cancel_path, names[index])
        owner_catalog.read_plan(owners, job.owners_sha256)
        code=dependencies.run_worker(worker, timeout,
                                     cancelled=(lambda:cancel_path.exists()) if cancel_path is not None else None)
        reason=stop_reason()
        if reason:
            return stop_run(reason,'after_worker_return')
        reading=None
        failure=None
        try:
            reading=native_results.read_worker_result(worker)
        except WDMSError as error:
            failure=error
        if type(code) is not int or code != 0 or failure is not None:
            events=dependencies.read_events(log)
            errors=[e for e in events if e.get('error_code') and e.get('event') in TERMINAL_WORKER_EVENTS]
            if failure is not None and failure.code != 'TRIAL_RESULT_UNCONFIRMED':
                info=error_info(failure)
            elif errors:
                info={key:errors[-1][key] for key in ('error_code','error') if key in errors[-1]}
            else:
                info={'error_code':'TRIAL_WORKER_EXITED' if code != 0 else 'TRIAL_WORKER_UNCONFIRMED',
                      'error':f'第{index+1}个货主退出码为{code}，未取得本轮完整采集凭证；详见{log.name}和{stderr_path.name}。'}
            dependencies.emit(log,'trial_worker_failed',**info,index=index,exit_code=code,stderr_file=stderr_path.name)
            print(json.dumps(info,ensure_ascii=False),flush=True)
            print('测试已停止，不自动重试；未生成完整测试表。',flush=True)
            notice.close()
            notify('库存采集已停止',
                f'已完成 {len(rows)}/{args.limit} 个货主。\n停止于：{names[index]}\n原因：{info.get("error",info["error_code"])}\n\n'
                f'未生成完整测试表，不会自动重试。现在可以操作鼠标键盘。\n日志目录：{folder}', failure=True)
            return BatchResult(2, len(rows))
        rows.append(reading.as_row())
        print(f"  整件={rows[-1]['whole']} 散件={rows[-1]['loose']}",flush=True)
    reason=stop_reason()
    if reason:
        return stop_run(reason,'after_collection')
    result=folder/f'{inventory_report_title(job.context.report_date)}.xlsx'
    try:
        dependencies.write_excel(rows,result,task_date=job.context.report_date)
        reason=stop_reason()
        if reason:
            return stop_run(reason,'after_excel_write',report=result)
        (folder/'trial_results.json').write_text(json.dumps({'formal_report':False,'requires_manual_review':True,'result_owner_independently_verified':False,'rows':[{k:r[k] for k in ('owner','whole','loose')} for r in rows]},ensure_ascii=False,indent=2),encoding='utf-8')
        reason=stop_reason()
        if reason:
            return stop_run(reason,'before_manifest_publish',report=result)
        if args.result_manifest:
            temporary=args.result_manifest.with_suffix('.tmp')
            temporary.write_text(json.dumps({'report':str(result),'row_count':len(rows),'requires_manual_review':True,'report_date':job.context.report_date.isoformat(),'run_id':job.context.run_id},ensure_ascii=False),encoding='utf-8')
            temporary.replace(args.result_manifest)
            manifest_published_state=True
    except Exception as error:
        dependencies.emit(folder/'trial_error.jsonl', 'trial_report_failed', **error_info(error))
        print(f'结果文件保存失败：{type(error).__name__}。日志目录：{folder}', flush=True)
        notice.close()
        notify('库存采集未完成',
            f'已采样 {len(rows)} 个货主，但结果文件未能完整保存。\n'
            f'现在可以操作鼠标键盘，请保留日志并反馈错误。\n日志目录：{folder}', failure=True)
        return BatchResult(2, len(rows), result)
    notice.close()
    reason=stop_reason()
    if reason:
        return stop_run(reason,'before_final_success',report=result,
                        manifest_published=manifest_published_state)
    print(f'测试采样完成：{result}\n这不是正式报表，请按货主人工核对。',flush=True)
    notify('库存采集完成',
        f'已完成 {len(rows)}/{args.limit} 个货主，测试表已保存。\n\n结果文件：{result}\n\n'
        '现在可以操作鼠标键盘。请人工核对货主、整件数、散件数。')
    return BatchResult(0, len(rows), result)
