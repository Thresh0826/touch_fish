"""Standalone row-driven desktop; business systems stay behind guarded ports."""
from __future__ import annotations

from dataclasses import asdict
import ctypes
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from moyu_tools.model import Plan, Policy
from moyu_tools.runtime import adopt_binding, make_request, rearm, run_child
from moyu_tools.settings import defaults, load_json, profile_digest, state_root
from moyu_tools.sheet import SheetPlan, import_sheet, sheet_names
from services.event_log import error_info
from services.status_manager import atomic_json_write


class App:
    def __init__(self, root):
        self.root, self.request = root, None
        self.running, self.importing, self.quitting = False, False, False
        self.source_pending = False
        self.messages = queue.Queue()
        self.config, self.profile = defaults(), None
        root.title("摸鱼小工具 · 表格逐行查询")
        height = max(500, min(700, root.winfo_screenheight()-90))
        root.geometry(f"920x{height}")
        root.minsize(790, 500)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(9, 5))
        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="摸鱼小工具", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        ttk.Label(panel, text="表格每行：当天日期＋合同编号 → 送货单号 → 远程ZIP → 本地保存",
                  foreground="#5e6878").pack(anchor="w", pady=(3, 8))
        try:
            if (state_root()/"settings.json").exists():
                candidate = load_json(state_root()/"settings.json")
                if candidate.get("plan") is not None and not isinstance(Plan.parse(candidate["plan"]), SheetPlan):
                    raise ValueError("这里只接续表格任务。旧日期任务留在旧版目录，不会被转换。")
                Policy(**candidate["policy"])
                if not isinstance(candidate.get("output"), str) or not isinstance(candidate.get("task"), str):
                    raise ValueError("设置中的路径格式无效。")
                self.config.update(candidate)
            if (state_root()/"binding.json").exists():
                candidate = load_json(state_root()/"binding.json")
                profile_digest(candidate)
                if candidate["schema"] == 3:
                    self.profile = candidate
        except Exception as error:
            messagebox.showwarning("需要检查设置", error_info(error)["error"], parent=root)
        # Stop stays outside the tabs and remains visible on small screens.
        actions = ttk.Frame(panel)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.trial = ttk.Button(actions, text="一行试跑／继续试跑", command=lambda: self.start(True))
        self.trial.pack(side="left")
        self.run = ttk.Button(actions, text="开始／继续整表", command=lambda: self.start(False), state="disabled")
        self.run.pack(side="left", padx=12)
        self.stop = ttk.Button(actions, text="停止", command=self.cancel, state="disabled")
        self.stop.pack(side="right")
        notebook = ttk.Notebook(panel)
        notebook.pack(fill="both", expand=True)
        task_tab = ttk.Frame(notebook)
        viewport = tk.Canvas(task_tab, highlightthickness=0, background=style.lookup("TFrame", "background"))
        scrollbar = ttk.Scrollbar(task_tab, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        viewport.pack(side="left", fill="both", expand=True)
        task = ttk.Frame(viewport, padding=10)
        item = viewport.create_window((0, 0), window=task, anchor="nw")
        task.bind("<Configure>", lambda event: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(item, width=event.width))
        setup = ttk.Frame(notebook, padding=12)
        notebook.add(task_tab, text="表格任务")
        notebook.add(setup, text="定位、等待与恢复")
        self.setup_buttons, self.editors = [], []
        self.source = tk.StringVar(value=self.config.get("source_file", ""))
        source_line = ttk.Frame(task)
        source_line.pack(fill="x")
        ttk.Label(source_line, text="来源表格").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(source_line, textvariable=self.source, state="readonly")
        entry.pack(side="left", fill="x", expand=True)
        self.button(source_line, "选择.xlsx…", self.browse_source).pack(side="left", padx=(8, 0))
        sheet_line = ttk.Frame(task)
        sheet_line.pack(fill="x", pady=6)
        ttk.Label(sheet_line, text="工作表").pack(side="left", padx=(0, 8))
        saved_sheet = (self.config.get("plan") or {}).get("sheet", "")
        self.sheet = tk.StringVar(value=saved_sheet)
        combo = ttk.Combobox(sheet_line, textvariable=self.sheet, values=[saved_sheet] if saved_sheet else [],
                             state="readonly", width=24)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda event: self.source_changed())
        self.sheet_box = combo
        self.editors.append((combo, "readonly"))
        self.button(sheet_line, "导入并预览", self.import_rows).pack(side="left", padx=8)
        self.source_status = tk.StringVar()
        ttk.Label(sheet_line, textvariable=self.source_status).pack(side="left")
        self.preview = ttk.Treeview(task, columns=("row", "date", "contract"), show="headings", height=4)
        for key, title, width in (("row", "原表行号", 100), ("date", "下单日期", 180), ("contract", "单据编号 → 合同编号", 300)):
            self.preview.heading(key, text=title)
            self.preview.column(key, width=width, minwidth=80)
        self.preview.pack(fill="x", pady=(0, 5))
        ttk.Label(task, text="按原表顺序，空白行不建任务；不排序、不合并同日记录。只读原表，续跑使用已保存的导入快照。",
                  wraplength=850, foreground="#5e6878").pack(anchor="w")
        self.output = tk.StringVar(value=self.config.get("output", ""))
        destination = ttk.Frame(task)
        destination.pack(fill="x", pady=8)
        ttk.Label(destination, text="本地目标文件夹").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(destination, textvariable=self.output)
        entry.pack(side="left", fill="x", expand=True)
        self.editors.append((entry, "normal"))
        self.button(destination, "选择…", self.browse).pack(side="left", padx=(8, 0))
        self.binding_status = tk.StringVar(value="定位已保存" if self.profile else "请在“定位、等待与恢复”页完成首次定位")
        ttk.Label(task, textvariable=self.binding_status, foreground="#5e6878", wraplength=850).pack(anchor="w")
        self.status = tk.StringVar(value="先导入并核对预览，再定位、一行试跑。成功取得一份ZIP后才启用整表。")
        ttk.Label(task, textvariable=self.status, wraplength=850, justify="left").pack(anchor="w", pady=8)
        self.progress = ttk.Progressbar(task, maximum=1)
        self.progress.pack(fill="x", pady=3)
        self.detail = tk.StringVar(value="尚未开始")
        ttk.Label(task, textvariable=self.detail, wraplength=850).pack(anchor="w")
        ttk.Label(setup, text="先手工登录WDMS，打开送货单检索，把“送货单号”列拖到前面。\n"
                  "远程桌面中打开Everything并显示完整ZIP文件名。截图定位后直接复制到目标文件夹。",
                  wraplength=820, justify="left").pack(anchor="w", pady=(0, 10))
        form = ttk.Frame(setup)
        form.pack(fill="x")
        self.fields = {}
        values = [("initial_wait", "查询初始等待（秒）"), ("query_timeout", "查询上限（秒）"),
                  ("search_timeout", "搜索上限（秒）"), ("copy_timeout", "复制上限（秒）")]
        for index, (key, title) in enumerate(values):
            column, row = (index % 2)*2, index//2
            ttk.Label(form, text=title).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=5)
            variable = tk.StringVar(value=str(self.config["policy"][key]))
            self.fields[key] = variable
            entry = ttk.Entry(form, textvariable=variable, width=16)
            entry.grid(row=row, column=column+1, sticky="ew", padx=(0, 20), pady=5)
            self.editors.append((entry, "normal"))
        controls = ttk.Frame(setup)
        controls.pack(fill="x", pady=10)
        for index, (title, action) in enumerate([
                ("首次定位／重新定位", self.calibrate), ("沿用旧进度", self.adopt), ("新建任务", self.new_task),
                ("确认重做当前行", self.recover), ("打开日志", self.open_logs), ("查看处理记录", self.show_records)]):
            self.button(controls, title, action).grid(row=index//3, column=index%3, padx=(0, 8), pady=5, sticky="ew")
        ttk.Label(setup, text="查询完成仍需观察本次忙碌结束，以及结果和行数稳定5秒。未找到会记到原表行号并继续；多条记录或单号不明会暂停。\n"
                  "续跑前确认上次查询、搜索和复制已结束。原表变更后重新导入会另建任务，旧任务和文件保留。",
                  wraplength=820, justify="left").pack(anchor="w", pady=8)
        self.refresh_preview()
        self.controls(False)
        root.after(200, self.poll)

    def button(self, parent, text, action):
        button = ttk.Button(parent, text=text, command=action)
        self.setup_buttons.append(button)
        return button

    def busy(self):
        return self.running or self.importing

    def controls(self, locked):
        for button in self.setup_buttons+[self.trial]:
            button.configure(state="disabled" if locked else "normal")
        for editor, state in self.editors:
            editor.configure(state="disabled" if locked else state)
        self.run.configure(state="disabled")
        if not locked and (self.source_pending or not self.config.get("plan")):
            self.trial.configure(state="disabled")
        self.stop.configure(state="normal" if self.running else "disabled")

    def refresh_preview(self):
        self.preview.delete(*self.preview.get_children())
        plan = self.config.get("plan")
        if plan:
            rows = Plan.parse(plan).rows
            self.source_status.set(f"共{len(rows)}条，预览前{min(20, len(rows))}条")
            for row in rows[:20]:
                self.preview.insert("", "end", values=(row.source_row, row.day.isoformat(), row.contract))
            self.progress["maximum"] = len(rows)
        else:
            self.source_status.set("尚未导入")

    def browse_source(self):
        if self.busy():
            return
        path = filedialog.askopenfilename(parent=self.root, title="选择已保存的.xlsx原表",
                                          filetypes=[("Excel工作簿", "*.xlsx")])
        if not path:
            return
        try:
            names = sheet_names(path)
            if not names:
                raise ValueError("该文件没有可见工作表。")
            self.source.set(path)
            self.sheet_box["values"] = names
            self.sheet.set(names[0])
            self.source_changed()
        except Exception as error:
            messagebox.showerror("表格未打开", error_info(error)["error"], parent=self.root)

    def source_changed(self):
        self.source_pending = True
        self.trial.configure(state="disabled")
        self.run.configure(state="disabled")
        self.status.set("已选择表格或工作表，尚未导入。请点击“导入并预览”；旧任务保留，当前不会运行旧预览中的记录。")

    def import_rows(self):
        if self.busy():
            return
        if self.config.get("task") and not messagebox.askyesno(
                "保留旧任务并重新导入", "重新导入会另建任务并从第一条开始。旧进度、暂存文件和目标文件均保留。确认重新导入？", parent=self.root):
            return
        path, sheet = self.source.get(), self.sheet.get()
        self.importing = True
        self.controls(True)
        self.status.set("正在只读导入表格，尚未操作WDMS或远程窗口。")
        def read():
            try:
                self.messages.put(("import", True, import_sheet(path, sheet)))
            except Exception as error:
                self.messages.put(("import", False, error_info(error)["error"]))
        threading.Thread(target=read, daemon=True, name="moyu-sheet-import").start()

    def browse(self):
        if not self.busy():
            folder = filedialog.askdirectory(parent=self.root, mustexist=True, title="选择已建立的本地目标文件夹")
            if folder:
                self.output.set(folder)

    def settings(self):
        if self.source_pending:
            raise ValueError("已选择新表格或工作表，请先点击“导入并预览”并核对，不能用旧预览启动。")
        if not self.config.get("plan") or not isinstance(Plan.parse(self.config["plan"]), SheetPlan):
            raise ValueError("请先选择.xlsx、工作表并点击“导入并预览”。")
        try:
            policy = dict(self.config["policy"])
            for key in self.fields:
                policy[key] = float(self.fields[key].get())
            policy = asdict(Policy(**policy))
        except (ValueError, KeyError, TypeError):
            raise ValueError("等待时间请填写有效秒数。")
        self.config.update(policy=policy, output=self.output.get().strip())
        return self.config

    def calibrate(self):
        if self.busy():
            return
        try:
            if sys.platform != "win32":
                raise ValueError("首次定位仅支持Windows；当前未操作任何窗口。")
            from moyu_tools.wizard import Wizard
            Wizard(self.root, self.bound)
        except Exception as error:
            messagebox.showerror("定位未开始", str(error), parent=self.root)

    def bound(self, profile):
        self.profile = profile
        self.run.configure(state="disabled")
        self.binding_status.set("定位已保存；续跑原任务先点“沿用旧进度”，本次定位须重新通过一行试跑。")

    def adopt(self):
        if self.busy() or not self.config.get("task") or self.profile is None:
            return
        text = ("确认上次WDMS查询、远程搜索和资源管理器复制均已结束。工具会核对相同的表格快照、目标文件夹及固定筛选条件。\n"
                "已完成行和文件保留，未完成查询可能需要重新提交。新定位须重新通过一行ZIP闭环。确认沿用？")
        if not messagebox.askyesno("核实后接续旧任务", text, parent=self.root):
            return
        try:
            adopt_binding(self.settings(), self.profile)
            self.run.configure(state="disabled")
            self.status.set("旧任务和已完成文件保留，请一行试跑验证本次窗口定位。")
        except Exception as error:
            messagebox.showerror("未关联旧进度", str(error), parent=self.root)

    def new_task(self):
        if self.busy():
            return
        if self.config.get("task") and not messagebox.askyesno("保留旧任务并新建",
                "旧进度和暂存文件保留，新任务从已导入快照的第一条开始。确定新建？", parent=self.root):
            return
        self.config["task"] = ""
        atomic_json_write(state_root()/"settings.json", self.config)
        self.status.set("已准备新任务。修改过原表须重新导入；核对预览和目标文件夹后先一行试跑。")
        self.progress["value"] = 0
        self.run.configure(state="disabled")

    def recover(self):
        if self.busy() or not self.config.get("task") or self.profile is None:
            return
        text = ("请先确认WDMS上次查询结束、资源管理器没有正在复制的文件。复制尚在进行时应等待后用“继续”。\n"
                "完整ZIP会由继续核实；无法继续的半成品会原样改名为recovery_开头保留，再允许重做当前行。确认重做？")
        if not messagebox.askyesno("人工确认后重做", text, parent=self.root):
            return
        try:
            rearm(self.settings(), self.profile)
            self.status.set("当前行已允许重做；点击试跑或继续。尚未自动操作WDMS。")
        except Exception as error:
            messagebox.showerror("没有重置", str(error), parent=self.root)

    def start(self, trial):
        if self.busy():
            return
        try:
            if sys.platform != "win32":
                raise ValueError("现场操作仅支持Windows；当前未连接WDMS。")
            if self.profile is None:
                raise ValueError("请先完成首次定位。")
            path, self.request = make_request(self.settings(), self.profile, trial=trial)
            self.config["task"] = self.request["task"]
            atomic_json_write(state_root()/"settings.json", self.config)
        except Exception as error:
            messagebox.showerror("未开始", str(error), parent=self.root)
            return
        self.running = True
        self.controls(True)
        self.status.set("正在运行，请保持键鼠空闲。")
        def run():
            try:
                self.messages.put(("run", True, run_child(path, self.request)))
            except Exception as error:
                self.messages.put(("run", False, error_info(error)["error"]))
        threading.Thread(target=run, daemon=True, name="moyu-supervisor").start()

    def cancel(self):
        if self.request and self.running:
            Path(self.request["cancel"]).touch()
            self.status.set("正在停止自动输入并保留当前行。WDMS查询或资源管理器复制可能仍在进行。")

    def open_logs(self):
        path = Path(self.config["task"]) if self.config.get("task") else state_root()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def show_records(self):
        if self.busy() or not self.config.get("task"):
            return
        try:
            value = load_json(Path(self.config["task"])/"progress.json")
        except Exception as error:
            messagebox.showerror("记录未打开", error_info(error)["error"], parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("本任务处理记录 · 只读")
        window.geometry("880x400")
        columns = ("row", "date", "contract", "state", "number", "file")
        table = ttk.Treeview(window, columns=columns, show="headings")
        for key, title, width in zip(columns, ("原表行号", "日期", "合同编号", "状态", "送货单号", "实际ZIP文件名"),
                                     (70, 110, 130, 90, 140, 250)):
            table.heading(key, text=title)
            table.column(key, width=width, minwidth=50)
        scroll = ttk.Scrollbar(window, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        for row in value["segments"]:
            status = {"done": "已保存", "empty": "未找到", "pending": "待处理"}.get(row["phase"], "未完成")
            table.insert("", "end", values=(row["source_row"], row["query_date"], row["contract"], status,
                                              row.get("number") or "", row.get("file", {}).get("name", "")))

    def poll(self):
        try:
            kind, ok, result = self.messages.get_nowait()
        except queue.Empty:
            pass  # Background import/supervision has not posted a terminal result.
        else:
            if kind == "import":
                self.importing = False
                if ok:
                    candidate = dict(self.config, plan=result.document(), task="", source_file=self.source.get(),
                                     output=self.output.get().strip())
                    try:
                        atomic_json_write(state_root()/"settings.json", candidate)
                    except Exception as error:
                        ok, result = False, error_info(error)["error"]
                    else:
                        self.config = candidate
                        self.source_pending = False
                        self.refresh_preview()
                        self.progress["value"] = 0
                        self.status.set("导入完成，原表未修改。请核对预览后定位并一行试跑。")
                if not ok:
                    self.status.set("导入未通过，原任务与进度保留；尚未操作WDMS。")
                    messagebox.showerror("表格未导入", result, parent=self.root)
            else:
                self.running = False
                self.status.set({"completed": "整表处理结束，请核对成功数、未找到数和目标文件。",
                                 "stopped": "已停止自动输入并保留进度；请等上次查询和复制结束后继续。",
                                 "trial_passed": "一行ZIP闭环已通过，请核对实际文件后再开始整表。",
                                 "trial_pending": "旧行已恢复；请继续试跑，验证本次窗口定位。",
                                 "trial_empty": "本行记为未找到，尚未通过ZIP试跑；可继续试跑下一行。"}.get(result, result))
                if not ok:
                    messagebox.showerror("本轮已停住", result+"\n当前行及已复制文件保留，可在“打开日志”中查看本轮目录。", parent=self.root)
            self.controls(False)
        if self.config.get("task"):
            try:
                value = load_json(Path(self.config["task"])/"progress.json")
                done = sum(row["phase"] == "done" for row in value["segments"])
                empty = sum(row["phase"] == "empty" for row in value["segments"])
                self.progress["maximum"], self.progress["value"] = len(value["segments"]), done+empty
                current = next((row for row in value["segments"] if row["phase"] not in ("done", "empty")), None)
                self.detail.set(f"已处理{done+empty}/{len(value['segments'])}条，成功{done}，未找到{empty}" +
                                (f"；当前原表第{current['source_row']}行，日期{current['query_date']}，合同编号{current['contract']}" if current else ""))
                if not self.busy():
                    passed = (value.get("trial_passed") is True and self.profile is not None
                              and not self.source_pending
                              and value["identity"]["binding"] == profile_digest(self.profile)
                              and value["identity"]["plan"] == self.config["plan"])
                    self.run.configure(state="normal" if passed else "disabled")
            except Exception:
                # A worker can be publishing; its final receipt remains authoritative.
                if not self.busy():
                    self.run.configure(state="disabled")
        if self.quitting and not self.busy():
            self.root.destroy()
            return
        self.root.after(250, self.poll)

    def close(self):
        if self.busy():
            if not messagebox.askyesno("停止并退出", "退出会停止脚本并保留当前行。WDMS及资源管理器不会被关闭。确定退出？", parent=self.root):
                return
            self.quitting = True
            self.cancel()
        else:
            self.root.destroy()


def main():
    if sys.platform == "win32":
        ctypes.WinDLL("user32", use_last_error=True).SetProcessDPIAware()
    root = tk.Tk()
    App(root)
    root.mainloop()
