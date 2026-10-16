from __future__ import annotations
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from desktop_demo.settings import load_settings, save_settings, settings_path, validate_executable, validate_output
from desktop_demo.credentials import WindowsCredentials

RESOURCE_ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
BG='#f3f6fa'
INK='#16243a'
MUTED='#64748b'
BLUE='#2563eb'

class App:
    def __init__(self,root):
        self.root=root
        self.process=None
        self.output_handle=None
        self.step=0
        self.config=None
        self.status=tk.StringVar(value='请选择操作。')
        self.exe=tk.StringVar()
        self.username=tk.StringVar()
        self.password=tk.StringVar()
        self.output=tk.StringVar(value=str(Path.home()/'Documents'/'WDMS报表'))
        self.scope=tk.StringVar(value='全部 24 个货主')
        root.title('WDMS 库存采集 · 首次配置 Demo')
        root.geometry('960x650')
        root.minsize(900,620)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW',self.close)
        style=ttk.Style()
        style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10))
        style.configure('TFrame',background=BG)
        style.configure('Card.TFrame',background='white')
        style.configure('TLabel',background=BG,foreground=INK)
        style.configure('Card.TLabel',background='white',foreground=INK)
        style.configure('Muted.TLabel',background='white',foreground=MUTED)
        style.configure('TEntry',padding=8)
        style.configure('TButton',padding=(16,10))
        style.configure('Primary.TButton',background=BLUE,foreground='white',borderwidth=0)
        style.map('Primary.TButton',background=[('active','#1d4ed8'),('disabled','#94a3b8')])
        self.header=ttk.Frame(root,padding=(30,22))
        self.header.pack(fill='x')
        ttk.Label(self.header,text='WDMS 库存采集',font=('Microsoft YaHei UI',22,'bold')).pack(anchor='w')
        ttk.Label(self.header,text='货主 · 整件数 · 散件数',foreground=MUTED).pack(anchor='w',pady=(5,0))
        self.body=ttk.Frame(root,padding=(30,0,30,20))
        self.body.pack(fill='both',expand=True)
        try:
            self.config=load_settings()
        except ValueError as error:
            messagebox.showwarning('需要重新配置',str(error),parent=root)
        if self.config:
            self.home()
        else:
            default=Path.home()/'Desktop'/'WDMS安装包'/'WDMS客户端自动更新'/'Client'/'App.exe'
            if default.exists():self.exe.set(str(default))
            self.wizard()

    def clear(self):
        for child in self.body.winfo_children():child.destroy()

    def card(self,parent=None):
        card=ttk.Frame(parent or self.body,style='Card.TFrame',padding=24)
        card.pack(fill='both',expand=True)
        return card

    def paragraph(self,parent,text,muted=False):
        ttk.Label(parent,text=text,style='Muted.TLabel' if muted else 'Card.TLabel',wraplength=590,justify='left').pack(anchor='w',pady=(0,14))

    def wizard(self):
        self.clear()
        self.root.title('WDMS 库存采集 · 首次配置 Demo')
        sidebar=ttk.Frame(self.body,width=180)
        sidebar.pack(side='left',fill='y',padx=(0,24))
        titles=['WDMS 程序','登录账号','报表位置','确认配置']
        for index,title in enumerate(titles):
            ttk.Label(sidebar,text=f'{index+1}  {title}',foreground=BLUE if index==self.step else MUTED,font=('Microsoft YaHei UI',11,'bold' if index==self.step else 'normal')).pack(anchor='w',pady=13)
        panel=self.card()
        ttk.Label(panel,text=titles[self.step],style='Card.TLabel',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',pady=(0,18))
        if self.step==0:
            self.paragraph(panel,'选择这台电脑上的 WDMS 客户端。以后无需重复选择。')
            ttk.Entry(panel,textvariable=self.exe,width=60).pack(fill='x',pady=(8,12))
            ttk.Button(panel,text='选择程序…',command=self.browse_exe).pack(anchor='w')
            self.paragraph(panel,'WDMS 需已安装，并能连接仓库内网。',True)
        elif self.step==1:
            self.paragraph(panel,'账号密码只保存在当前电脑，用于自动启动和登录测试。')
            ttk.Label(panel,text='WDMS 账号',style='Card.TLabel').pack(anchor='w')
            ttk.Entry(panel,textvariable=self.username).pack(fill='x',pady=(6,18))
            ttk.Label(panel,text='密码',style='Card.TLabel').pack(anchor='w')
            ttk.Entry(panel,textvariable=self.password,show='●').pack(fill='x',pady=(6,12))
            self.paragraph(panel,'密码保存至 Windows 凭据管理器，不写入配置文件或日志。'+('留空可保留原密码；更换账号需重新填写。' if self.config else ''),True)
        elif self.step==2:
            self.paragraph(panel,'选择库存表和本轮记录的保存位置。保存前会检查文件夹能否写入。')
            ttk.Entry(panel,textvariable=self.output,width=60).pack(fill='x',pady=(8,12))
            ttk.Button(panel,text='选择文件夹…',command=self.browse_output).pack(anchor='w')
        else:
            self.paragraph(panel,'确认以下信息后，保存本机配置。')
            for title,value in [('WDMS 程序',self.exe.get()),('账号',self.username.get()),('报表位置',self.output.get())]:
                ttk.Label(panel,text=title,style='Muted.TLabel').pack(anchor='w',pady=(8,3))
                ttk.Label(panel,text=value,style='Card.TLabel',wraplength=540).pack(anchor='w')
            self.paragraph(panel,'保存后点“一键运行”生成表格。货主名单自动读取，生成Excel后一起核对名称和数据；本次保存不会提交登录。',True)
        actions=ttk.Frame(panel,style='Card.TFrame')
        actions.pack(side='bottom',fill='x',pady=(20,0))
        if self.step>0:
            ttk.Button(actions,text='上一步',command=lambda:self.move(-1)).pack(side='left')
        elif self.config:
            ttk.Button(actions,text='返回主界面',command=self.home).pack(side='left')
        ttk.Button(actions,text='保存配置' if self.step==3 else '下一步',style='Primary.TButton',command=self.save if self.step==3 else lambda:self.move(1)).pack(side='right')

    def move(self,direction):
        try:
            if direction>0:
                if self.step==0:self.exe.set(validate_executable(self.exe.get()))
                elif self.step==1:
                    if not self.username.get().strip():raise ValueError('请填写 WDMS 账号。')
                    if not self.password.get() and (not self.config or self.username.get().strip()!=self.config.username):
                        raise ValueError('请填写密码。')
                elif self.step==2:self.output.set(validate_output(self.output.get()))
            self.step+=direction
            self.wizard()
        except ValueError as error:
            messagebox.showwarning('请检查配置',str(error),parent=self.root)

    def save(self):
        try:
            credentials=WindowsCredentials()
            self.config=save_settings(self.exe.get(),self.username.get(),self.password.get(),self.output.get(),credentials,self.config)
            self.password.set('')
            self.status.set('配置已保存。选择货主范围后点“一键运行”，结束后核对表格。')
            self.home()
        except ValueError as error:
            messagebox.showerror('配置未保存',str(error),parent=self.root)
        except OSError:
            messagebox.showerror('配置未保存','本机配置文件无法写入，请检查目录权限。',parent=self.root)

    def browse_exe(self):
        value=filedialog.askopenfilename(parent=self.root,title='选择 WDMS 客户端',filetypes=[('Windows 程序','*.exe')])
        if value:self.exe.set(value)

    def browse_output(self):
        value=filedialog.askdirectory(parent=self.root,title='选择报表保存文件夹')
        if value:self.output.set(value)

    def home(self):
        self.password.set('')
        self.clear()
        card=self.card()
        ttk.Label(card,text='本机配置已保存',style='Card.TLabel',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',pady=(0,16))
        for title,value in [('WDMS 程序',self.config.wdms_exe),('登录账号',self.config.username),('报表位置',self.config.output_dir)]:
            self.paragraph(card,f'{title}：{value}')
        self.paragraph(card,'点击“一键运行”后自动登录、进入检索、读取货主并保存表格。运行期间不要操作鼠标键盘，结束后在Excel核对货主名称与数据。当前为开发测试。',True)
        ttk.Label(card,textvariable=self.status,style='Card.TLabel',wraplength=820).pack(anchor='w',pady=(8,18))
        top=ttk.Frame(card,style='Card.TFrame');top.pack(fill='x',pady=(0,16))
        ttk.Button(top,text='打开报表目录',command=self.open_folder).pack(side='left',padx=(0,10))
        ttk.Button(top,text='打开运行日志',command=self.open_logs).pack(side='left',padx=(0,10))
        ttk.Button(top,text='修改设置',command=self.edit_settings).pack(side='left')
        actions=ttk.Frame(card,style='Card.TFrame');actions.pack(side='bottom',fill='x')
        ttk.Combobox(actions,textvariable=self.scope,values=['前 3 个货主','全部 24 个货主'],state='readonly',width=18).pack(side='left',padx=14)
        ttk.Button(actions,text='一键运行：登录并生成表格',style='Primary.TButton',command=self.full_workflow).pack(side='right',padx=(0,10))

    def edit_settings(self):
        self.exe.set(self.config.wdms_exe)
        self.username.set(self.config.username)
        self.output.set(self.config.output_dir)
        self.password.set('')
        self.step=0
        self.wizard()

    def login(self):
        self.start_window_task('login')

    def navigation(self):
        self.start_window_task('navigation')

    def full_workflow(self):
        self.start_window_task('workflow')

    def start_window_task(self,task):
        try:
            self.window_task=task
            prefix={'login':'auto_login_','navigation':'auto_navigation_','workflow':'flow_'}[task]
            route={'login':'--native-login','navigation':'--native-navigate','workflow':'--native-workflow'}[task]
            logs=settings_path().parent/(task+'_logs')
            logs.mkdir(parents=True,exist_ok=True)
            self.login_folder=logs
            self.task_log_pattern=prefix+'*/flow.jsonl' if task=='workflow' else prefix+'*.jsonl'
            self.previous_login_logs=set(logs.glob(self.task_log_pattern))
            self.run_log=logs/f'console_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.txt'
            self.output_handle=self.run_log.open('w',encoding='utf-8')
            if getattr(sys,'frozen',False):
                command=[sys.executable,route,'--out',str(logs)]
            else:
                command=[sys.executable,str(RESOURCE_ROOT/'scripts'/'WDMS_Demo.py'),route,'--out',str(logs)]
            if task=='workflow':command.extend(['--limit','3' if self.scope.get()=='前 3 个货主' else '24'])
            env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
            self.root.withdraw()
            self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=self.output_handle,
                stderr=subprocess.STDOUT,env=env,cwd=str(RESOURCE_ROOT))
            self.root.after(300,self.poll_login)
        except Exception:
            if self.output_handle:self.output_handle.close();self.output_handle=None
            self.root.deiconify()
            messagebox.showerror('无法启动流程','请检查本机配置和运行环境。',parent=self.root)

    def poll_login(self):
        if self.process.poll() is None:
            self.root.after(300,self.poll_login)
            return
        code=self.process.returncode
        self.process=None
        if self.output_handle:self.output_handle.close();self.output_handle=None
        task=self.window_task
        logs=set(self.login_folder.glob(self.task_log_pattern))-self.previous_login_logs
        message='运行结束，尚未确认成功，请查看本轮记录。'
        for path in sorted(logs):
            if path.stem.endswith('_home') or '_scan_' in path.stem:continue
            for line in path.read_text(encoding='utf-8').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('error_code'):message=event.get('error','流程未完成，请查看本轮记录。')
                elif event.get('event')=='automatic_login_complete' and code==0:
                    message='自动启动和登录测试通过。请继续测试“自动进入检索”。'
                elif event.get('event')=='automatic_navigation_complete' and code==0:
                    message='自动导航通过，已进入在库商品检索。此轮没有查询或采集，可继续测试采集。'
                elif event.get('event')=='automatic_workflow_complete' and code==0:
                    message=f'全流程完成，共 {event["owner_count"]} 个货主。表格：{event["report"]}'
        self.status.set(message+f' 日志目录：{self.login_folder}')
        self.root.deiconify()
        self.home()
        if task!='workflow':messagebox.showinfo('登录测试结果' if task=='login' else '导航测试结果',message,parent=self.root)

    def launch_wdms(self):
        try:
            exe=validate_executable(self.config.wdms_exe)
            import psutil
            for process in psutil.process_iter(['exe']):
                try:
                    if process.info['exe'] and os.path.normcase(process.info['exe'])==os.path.normcase(exe):
                        self.status.set('WDMS 已在运行，请手动登录并进入检索页面。')
                        return
                except (psutil.AccessDenied,psutil.NoSuchProcess):
                    # An inaccessible or vanished process is skipped while checking other candidates.
                    pass
            subprocess.Popen([exe],cwd=str(Path(exe).parent))
            self.status.set('WDMS 已启动。此 Demo 请先手动登录并进入检索页面。')
        except (ValueError,OSError,ImportError):
            messagebox.showerror('无法启动','请检查 WDMS 程序路径和本机依赖。',parent=self.root)

    def open_folder(self):
        try:
            os.startfile(validate_output(self.config.output_dir))
        except (OSError,ValueError,AttributeError):
            messagebox.showerror('无法打开目录','请检查报表保存位置。',parent=self.root)

    def open_logs(self):
        try:
            folder=settings_path().parent/'workflow_logs'
            folder.mkdir(parents=True,exist_ok=True)
            os.startfile(str(folder))
        except (OSError,AttributeError):
            messagebox.showerror('无法打开日志','请检查本机运行日志目录。',parent=self.root)

    def simulate(self):
        try:
            from scripts.run_native_trial import write_trial_excel
            from openpyxl import load_workbook
            names=[f'模拟货主{i:02d}' for i in range(1,25)]
            folder=Path(validate_output(self.config.output_dir))/'模拟演示'
            folder.mkdir(exist_ok=True)
            path=folder/f'模拟演示_非真实库存_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx'
            rows=[{'owner':name,'whole':str((i+1)*10),'loose':str(Decimal(i)+Decimal('0.54'))} for i,name in enumerate(names)]
            write_trial_excel(rows,path)
            book=load_workbook(path)
            book.active.title='模拟数据_非真实库存'
            book.save(path);book.close()
            self.status.set(f'模拟演示完成，共 24 行，全部是模拟数据：{path}')
        except Exception:
            messagebox.showerror('模拟演示失败','请检查输出目录是否可写及 Excel 文件是否被占用。',parent=self.root)

    def collect(self):
        from scripts.run_native_trial import trial_command
        try:
            folder=Path(validate_output(self.config.output_dir))
            self.previous_reports=set(folder.glob('native_trial_*/*.xlsx'))
            logs=settings_path().parent/'run_logs'
            logs.mkdir(parents=True,exist_ok=True)
            self.run_log=logs/f'run_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.txt'
            self.output_handle=self.run_log.open('w',encoding='utf-8')
            count=3 if self.scope.get()=='前 3 个货主' else 24
            command=trial_command(['--limit',str(count),'--out',str(folder)])
            env=os.environ.copy();env['PYTHONIOENCODING']='utf-8'
            self.root.withdraw()
            self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=self.output_handle,
                stderr=subprocess.STDOUT,env=env,cwd=str(RESOURCE_ROOT))
            self.root.after(300,self.poll)
        except Exception:
            if self.output_handle:self.output_handle.close();self.output_handle=None
            self.root.deiconify()
            messagebox.showerror('无法开始采集','请检查输出目录和运行环境。',parent=self.root)

    def poll(self):
        if self.process.poll() is None:
            self.root.after(300,self.poll)
            return
        code=self.process.returncode
        self.process=None
        if self.output_handle:self.output_handle.close();self.output_handle=None
        reports=set(Path(self.config.output_dir).glob('native_trial_*/*.xlsx'))-self.previous_reports
        if code==0 and len(reports)==1:
            self.status.set(f'采集完成，请人工核对三列表格：{next(iter(reports))}')
        else:
            self.status.set(f'采集未完整完成，请查看记录：{self.run_log}')
        self.root.deiconify()
        self.home()

    def close(self):
        if self.process and self.process.poll() is None:
            return
        self.password.set('')
        self.root.destroy()


def main():
    if sys.platform!='win32':
        raise SystemExit('请在 Windows 电脑上运行此 Demo。')
    # One GUI per current-user configuration, to avoid duplicate collection runs.
    import ctypes,hashlib
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    name='Local\\WDMSInventoryDemo_'+hashlib.sha256(str(settings_path()).encode()).hexdigest()[:16]
    handle=kernel.CreateMutexW(None,False,name)
    if not handle:raise SystemExit('无法创建应用实例锁。')
    try:
        duplicate=ctypes.get_last_error()==183
        root=tk.Tk()
        if duplicate:
            root.withdraw()
            messagebox.showinfo('程序已运行','请使用已经打开的 WDMS 库存采集窗口。',parent=root)
            root.destroy()
            return
        App(root)
        root.mainloop()
    finally:
        kernel.CloseHandle(handle)
