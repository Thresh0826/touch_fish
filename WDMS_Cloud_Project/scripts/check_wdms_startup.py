"""Read-only native login/navigation inspection. No credentials read or clicks."""
from __future__ import annotations
import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import check_wdms_window as probe
from scripts.run_native_trial import TrialNativeWindows
from errors import WDMSError


def find_stage_window(api, stage, log):
    found=[]
    errors=[]
    visited=[0]
    titles=('用户登录',) if stage=='login' else ('医药企业仓储运输管理系统',)
    def callback(hwnd,unused):
        visited[0]+=1
        if visited[0]>1024:
            errors.append(WDMSError('WINDOW_ENUM_LIMIT','顶层窗口数量超过检查上限。'))
            return False
        try:
            if api.user32.IsWindowVisible(hwnd) and api.title(hwnd) in titles:
                found.append(api.metadata(int(hwnd),include_title=True))
        except Exception as error:
            errors.append(error)
            return False
        return True
    record_callback=api.callback_type(callback)
    probe.record(log,'before_startup_window_enumeration',stage=stage)
    ok=api.user32.EnumWindows(record_callback,0)
    if errors:
        raise errors[0]
    if not ok:
        raise WDMSError('WINDOW_ENUM_FAILED','窗口枚举失败，未继续读取。')
    if len(found)!=1:
        raise WDMSError('STARTUP_WINDOW_NOT_UNIQUE',f'需要唯一的可见{titles[0]}窗口，当前找到 {len(found)} 个。')
    window=found[0]
    if window['minimized']:
        raise WDMSError('WDMS_WINDOW_MINIMIZED','请恢复 WDMS 窗口后检查。')
    probe.record(log,'startup_window_found',stage=stage,**window)
    return window


def child(args):
    log=Path(args.log)
    try:
        probe.require_usable_environment(probe.inspect_environment())
        api=TrialNativeWindows()
        root=find_stage_window(api,args.stage,log)
        api.health(root['hwnd'],log,'startup_initial')
        probe.check_native_controls(api,root,log,max_depth=8,budget=25,read_labels=True)
        probe.record(log,'startup_probe_complete',stage=args.stage,credential_fields_read=False,
                     uia_scan_performed=False,actions_performed=False)
        return 0
    except Exception as error:
        probe.record(log,'probe_error',**probe.error_info(error))
        return 2


def main(argv=None):
    parser=argparse.ArgumentParser(description='只读检查登录窗口或菜单页面，不读取密码、不点击、不登录。')
    parser.add_argument('--stage',choices=('login','home','stock','query'),required=True)
    parser.add_argument('--out',type=Path,default=ROOT/'test_logs')
    parser.add_argument('--_child',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--log',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args._child:
        return child(args)
    args.out.mkdir(parents=True,exist_ok=True)
    log=(args.out/f"startup_{args.stage}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.jsonl").resolve()
    probe.record(log,'startup_probe_start',stage=args.stage)
    print('只读原生控件检查：不读取账号密码输入框、不点击、不触发查询。',flush=True)
    print(f'证据：{log}',flush=True)
    command=[sys.executable,str(Path(__file__).resolve()),'--_child','--stage',args.stage,'--log',str(log)]
    code=probe.supervise(command,log,40)
    events=probe.read_events(log)
    if code!=0 or not any(e['event']=='startup_probe_complete' for e in events):
        for e in events:
            if e.get('error_code'):
                print(json.dumps(e,ensure_ascii=False))
        print('检查未完成，请上传日志。')
        return 2
    for e in events:
        if e['event']=='native_text' and e['text'].strip():
            print(f"{e['text']!r} rect={e['rect']}")
    print('检查完成。请上传 JSONL；这里没有执行登录或菜单导航。')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
