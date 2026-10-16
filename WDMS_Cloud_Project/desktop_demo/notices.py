"""Passive progress and final notices; collection rules live in core."""
from __future__ import annotations
import ctypes
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional
from adapters import native_windows as probe
from services.worker_commands import native_trial_command as trial_command, native_trial_working_directory


def running_notice_window(args) -> int:
    """Separate process: passive native window, no Tk dependency or activation."""
    u = ctypes.WinDLL('user32', use_last_error=True)
    w = probe.wintypes
    definitions = {
        'CreateWindowExW': ([w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p],w.HWND),
        'SetWindowPos': ([w.HWND,w.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.UINT],w.BOOL),
        'SetWindowTextW': ([w.HWND,w.LPCWSTR],w.BOOL),
        'GetSystemMetrics': ([ctypes.c_int],ctypes.c_int),
        'PeekMessageW': ([ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT,w.UINT],w.BOOL),
        'TranslateMessage': ([ctypes.POINTER(w.MSG)],w.BOOL),
        'DispatchMessageW': ([ctypes.POINTER(w.MSG)],ctypes.c_ssize_t),
        'DestroyWindow': ([w.HWND],w.BOOL),
    }
    for name,(arguments,result) in definitions.items():
        getattr(u,name).argtypes=arguments
        getattr(u,name).restype=result
    text='库存采集中，请勿操作鼠标键盘\n\n请保持 WDMS 前台，不要切换窗口。\n无需点击，任务已经自动开始。\n请等待完成或停止提示。'
    # WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE; popup STATIC.
    hwnd=u.CreateWindowExW(0x08000088,'STATIC',text,0x80000000|0x00800000|1,
                          max(0,u.GetSystemMetrics(0)-380),10,370,125,None,None,None,None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # SWP_NOACTIVATE | SWP_SHOWWINDOW; never SetForegroundWindow here.
        if not u.SetWindowPos(hwnd,w.HWND(-1),0,0,0,0,0x0010|0x0040|0x0001|0x0002):
            raise ctypes.WinError(ctypes.get_last_error())
        Path(args.notice_ready).write_text('ready',encoding='utf-8')
        message=w.MSG()
        previous=None
        while not Path(args.notice_stop).exists():
            while u.PeekMessageW(ctypes.byref(message),None,0,0,1):
                u.TranslateMessage(ctypes.byref(message))
                u.DispatchMessageW(ctypes.byref(message))
            try:
                progress=Path(args.notice_progress).read_text(encoding='utf-8')
                if progress != previous:
                    u.SetWindowTextW(hwnd,text+'\n'+progress)
                    previous=progress
            except OSError:
                # Progress can be momentarily absent; the passive window reads it again next poll.
                pass
            time.sleep(0.1)
    finally:
        u.DestroyWindow(hwnd)
    return 0


class RunningNotice:
    def __init__(self, folder: Path) -> None:
        self.folder=folder
        self.process=None
        self.stop=folder/'notice.stop'
        self.ready=folder/'notice.ready'
        self.progress=folder/'notice_progress.txt'

    def start(self) -> None:
        if sys.platform != 'win32':
            return
        command=trial_command(['--_notice',
                 '--notice-stop',str(self.stop),'--notice-ready',str(self.ready),
                 '--notice-progress',str(self.progress)])
        try:
            with (self.folder/'notice_errors.txt').open('w',encoding='utf-8') as output:
                self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,stderr=output,cwd=str(native_trial_working_directory()))
            deadline=time.monotonic()+3
            while not self.ready.exists() and self.process.poll() is None and time.monotonic()<deadline:
                time.sleep(0.05)
            if not self.ready.exists():
                self.close()
                print('运行提示窗未能显示，任务仍会自动开始。请勿操作鼠标键盘。',flush=True)
        except OSError:
            self.close()
            print('运行提示窗未能显示，请勿操作鼠标键盘。',flush=True)

    def update(self, current: int, count: int, name: str) -> None:
        try:
            self.progress.write_text(f'进度：{current}/{count}  {name}',encoding='utf-8')
        except OSError:
            # Progress display is optional and cannot change the collection outcome.
            pass

    def restart(self) -> None:
        self.close()
        token=uuid.uuid4().hex[:8]
        self.stop=self.folder/f'notice_{token}.stop'
        self.ready=self.folder/f'notice_{token}.ready'
        self.start()

    def close(self) -> None:
        process=self.process
        if process is None:
            return
        try:
            self.stop.write_text('stop',encoding='utf-8')
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=1)
        except (OSError,subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
        finally:
            self.process=None


class ManagedNotice:
    """Progress for an enclosing workflow that owns the warning and result."""
    def __init__(self,progress: Optional[Path]) -> None:self.progress=progress
    def start(self) -> None:pass
    def close(self) -> None:pass
    def update(self,current: int,count: int,name: str) -> None:
        if self.progress:
            try:Path(self.progress).write_text(f'采集进度：{current}/{count}  {name}',encoding='utf-8')
            except OSError:
                # An enclosing workflow can remove progress files during shutdown; results use its log.
                pass


def show_notice(title: str, message: str, *, start: bool = False, failure: bool = False) -> bool:
    """Parent-only notice, before collection or after all workers stop."""
    if sys.platform != 'win32':
        print(f'{title}：{message}', flush=True)
        return True
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    box = user32.MessageBoxW
    box.argtypes = [probe.wintypes.HWND, probe.wintypes.LPCWSTR,
                    probe.wintypes.LPCWSTR, probe.wintypes.UINT]
    box.restype = ctypes.c_int
    flags = 0x10000 | 0x40000 | (0x31 if start else 0x10 if failure else 0x40)
    response = box(None, message, title, flags)
    if response == 0:
        print('弹窗显示失败，请查看命令行提示。', flush=True)
        return not start
    return response == 1
