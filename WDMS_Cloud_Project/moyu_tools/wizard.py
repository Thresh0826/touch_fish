"""Local screenshot calibration: two captures, automatic hints and editable overlays."""
from __future__ import annotations

import copy
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from core.step_executor import StepContext
from moyu_tools.bridge import Bridge
from moyu_tools.calibration import FIELDS, REMOTE_FIELDS, automatic, canvas_rect, fill_derived, refresh_marks, screen_point, screen_rect, target_at
from moyu_tools.model import fail
from moyu_tools.native import Driver, LocalQuery, text_value
from moyu_tools.settings import save_profile, state_root, validate_profile
from services.event_log import error_info, record


class Wizard:
    def __init__(self, master, saved):
        self.master, self.saved = master, saved
        self.snapshots, self.markings = {}, {"wdms": {}, "remote": {}}
        self.manual = {"wdms": set(), "remote": set()}
        self.windows, self.busy, self.quitting = [], False, False
        self.role_choices = {}
        self.queue, self.cancel = queue.Queue(), threading.Event()
        state_root().mkdir(parents=True, exist_ok=True)
        self.log = state_root()/"calibration.jsonl"
        self.bridge = Bridge(self.log)
        self.driver = Driver(self.bridge, self.log)
        self.window = tk.Toplevel(master)
        self.window.title("摸鱼小工具 · 截图定位")
        self.window.transient(master)
        self.window.grab_set()
        width = max(760, min(1150, master.winfo_screenwidth()-60))
        height = max(480, min(780, master.winfo_screenheight()-90))
        self.preview_size = (width-260, height-180)
        self.window.geometry(f"{width}x{height}")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(self.window, padding=10)
        panel.pack(fill="both", expand=True)
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x")
        self.section = tk.StringVar(value="1. WDMS")
        role = ttk.Combobox(toolbar, textvariable=self.section, values=["1. WDMS", "2. 远程桌面"], width=14, state="readonly")
        role.pack(side="left")
        role.bind("<<ComboboxSelected>>", lambda event: self.show())
        self.choice = tk.StringVar()
        self.selector = ttk.Combobox(toolbar, textvariable=self.choice, width=44, state="readonly")
        self.selector.pack(side="left", padx=6, fill="x", expand=True)
        self.capture_button = ttk.Button(toolbar, text="获取／刷新截图", command=self.capture)
        self.capture_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="刷新窗口", command=self.list_windows).pack(side="left")
        self.prompt = tk.StringVar(value="选择WDMS窗口并获取截图；自动识别会画框，漏掉或错误的项目才需补选。")
        ttk.Label(panel, textvariable=self.prompt, wraplength=width-30, justify="left").pack(anchor="w", pady=8)
        footer = ttk.Frame(panel)
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        self.submit_enter = tk.BooleanVar(value=False)
        ttk.Checkbutton(footer, text="Everything需要回车查询", variable=self.submit_enter).pack(side="left")
        ttk.Button(footer, text="检查并保存定位", command=self.validate).pack(side="right")
        ttk.Button(footer, text="取消", command=self.close).pack(side="right", padx=8)
        body = ttk.Frame(panel)
        body.pack(fill="both", expand=True)
        self.fields = tk.Listbox(body, width=28, exportselection=False)
        self.fields.pack(side="left", fill="y", padx=(0, 8))
        self.fields.bind("<<ListboxSelect>>", lambda event: self.field_prompt())
        canvas_frame = ttk.Frame(body)
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_frame, background="#333333", highlightthickness=0)
        sx = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        sy = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        sx.pack(side="bottom", fill="x")
        sy.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.first, self.photo = None, None
        self.show()
        self.window.after(100, self.poll)
        self.list_windows()

    def key(self):
        return "wdms" if self.section.get().startswith("1.") else "remote"

    def definitions(self):
        return FIELDS if self.key() == "wdms" else REMOTE_FIELDS

    def job(self, action, kind):
        if self.busy:
            return
        self.busy = True
        self.capture_button.configure(state="disabled")
        def run():
            try:
                self.queue.put((kind, True, action()))
            except Exception as error:
                record(self.log, "calibration_failed", **error_info(error))
                self.queue.put((kind, False, error_info(error)["error"]))
        threading.Thread(target=run, daemon=True, name="moyu-snapshot").start()

    def list_windows(self):
        self.job(lambda: self.bridge.call("windows")["windows"], "windows")

    def capture(self):
        if self.busy:
            return
        try:
            root = self.windows[self.selector.current()]
            if self.selector.current() < 0:
                raise ValueError("请先选择窗口")
        except (IndexError, ValueError):
            messagebox.showinfo("选择窗口", "请先从窗口列表选择WDMS或远程桌面。", parent=self.window)
            return
        section = self.key()
        self.role_choices[section] = self.selector.current()
        self.window.withdraw()
        self.master.iconify()
        def read():
            context = StepContext(time.monotonic()+60, self.cancel.is_set, time.monotonic)
            self.driver.focus(root, context)
            self.driver.u.SetCursorPos(root["rect"][0]+6, root["rect"][1]+4)
            result = self.bridge.call("preview", {"root": root, "width": self.preview_size[0],
                                                   "height": self.preview_size[1]}, context)
            return section, result
        self.job(read, "capture")

    def show(self):
        if self.windows:
            chosen = self.role_choices.get(self.key())
            if chosen is None:
                words = ("医药", "WDMS") if self.key() == "wdms" else ("远程桌面", "Remote Desktop")
                candidates = [i for i, item in enumerate(self.windows) if any(word in item["title"] for word in words)]
                chosen = candidates[0] if len(candidates) == 1 else None
            if chosen is not None and chosen < len(self.windows):
                self.selector.current(chosen)
            else:
                self.selector.set("")
        selected = self.fields.curselection()
        index = selected[0] if selected else 0
        self.fields.delete(0, "end")
        found = self.markings[self.key()]
        for key, label, _ in self.definitions():
            self.fields.insert("end", ("✓ " if key in found else "  ")+label)
        self.fields.selection_set(min(index, len(self.definitions())-1))
        snapshot = self.snapshots.get(self.key())
        self.canvas.delete("all")
        if snapshot:
            self.photo = tk.PhotoImage(data=snapshot["image"], master=self.window)
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="image")
            self.canvas.configure(scrollregion=(0, 0, snapshot["width"], snapshot["height"]))
            for key, label, kind in self.definitions():
                if key not in found:
                    continue
                value = found[key]
                if kind == "point":
                    x, y = value["point"]
                    rect = value.get("accessible_rect", [x-6, y-6, x+6, y+6])
                else:
                    rect = value
                x1, y1, x2, y2 = canvas_rect(snapshot, rect)
                self.canvas.create_rectangle(x1, y1, x2, y2, outline="#18ef7c", width=2)
                self.canvas.create_text(x1+2, max(9, y1-7), text=label.split("（")[0], anchor="w", fill="#18ef7c")
        else:
            self.photo = None
            self.canvas.create_text(25, 30, text="先选择窗口，再点“获取／刷新截图”。", anchor="nw", fill="white")
        self.field_prompt()

    def field_prompt(self):
        if self.key() not in self.snapshots:
            self.prompt.set("先选择对应窗口并获取截图。能自动识别的位置会先标出，错误或缺失项才需要补选。")
            return
        selected = self.fields.curselection()
        if not selected:
            return
        _, label, kind = self.definitions()[selected[0]]
        self.prompt.set(("在截图上点击" if kind == "point" else "在截图上按住鼠标拖框选择")+label+
                        "。已自动识别的项目无需重选；截图只在本机内存显示，不保存或上传。")

    def coords(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def press(self, event):
        if not self.busy and self.key() in self.snapshots:
            self.first = self.coords(event)

    def motion(self, event):
        self.canvas.delete("drawing")
        selected = self.fields.curselection()
        if self.first and selected and self.definitions()[selected[0]][2] == "rect":
            self.canvas.create_rectangle(*self.first, *self.coords(event), outline="#ffcf40", width=2, tags="drawing")

    def release(self, event):
        selected = self.fields.curselection()
        if self.busy or not self.first or not selected:
            return
        first, self.first = self.first, None
        section = self.key()
        snapshot = self.snapshots[section]
        key, _, kind = self.definitions()[selected[0]]
        try:
            if kind == "rect":
                value = screen_rect(snapshot, first, self.coords(event))
            else:
                value = target_at(snapshot, screen_point(snapshot, *self.coords(event)))
            found = self.markings[section]
            found[key] = value
            self.manual[section].add(key)
            if key == "header" and "cell" not in self.manual[section]:
                found.pop("cell", None)
            if key == "search" and "anchor" not in self.manual[section]:
                found.pop("anchor", None)
            fill_derived(snapshot, found)
            self.show()
        except Exception as error:
            messagebox.showerror("请重新选择这一项", str(error), parent=self.window)

    def validate(self):
        if self.busy:
            return
        if set(self.snapshots) != {"wdms", "remote"}:
            messagebox.showinfo("还需另一张截图", "请分别选择WDMS窗口和远程桌面，各获取一张截图。", parent=self.window)
            return
        for section, definitions in (("wdms", FIELDS), ("remote", REMOTE_FIELDS)):
            missing = [label for key, label, _ in definitions if key not in self.markings[section]]
            if missing:
                self.section.set("1. WDMS" if section == "wdms" else "2. 远程桌面")
                self.show()
                messagebox.showinfo("只需补选缺失项", "请在截图中补选："+"、".join(missing), parent=self.window)
                return
        profile = {"schema": 3,
                   "wdms": {**copy.deepcopy(self.markings["wdms"]), "root": self.snapshots["wdms"]["root"],
                            "conditions_hash": "0"*64, "column_mode": "visible"},
                   "remote": {**copy.deepcopy(self.markings["remote"]), "root": self.snapshots["remote"]["root"],
                              "anchor_hash": "0"*64, "mode": "single_zip", "submit_enter": self.submit_enter.get()}}
        try:
            validate_profile(profile)
        except Exception as error:
            messagebox.showerror("请调整选中的位置", str(error), parent=self.window)
            return
        self.window.withdraw()
        self.master.iconify()
        def finish():
            context = StepContext(time.monotonic()+60, self.cancel.is_set, time.monotonic)
            query = LocalQuery(self.driver, profile)
            query.attach(context)
            query.query_button(context)
            query.date("start", context)
            query.date("end", context)
            query.contract(context)
            profile["wdms"]["conditions_hash"] = self.bridge.call("conditions", query.condition_payload(), context)["hash"]
            query.count(context)
            header = query.record("header", context)
            if text_value(header) != "送货单号":
                fail("MOYU_COLUMN_UNCONFIRMED", "表头不是送货单号，只需在WDMS截图重新选择表头。")
            if "cell" not in self.manual["wdms"] and header.get("rect"):
                rect = header["rect"]
                point = [(rect[0]+rect[2])//2, rect[3]+max(8, (rect[3]-rect[1])//2)]
                profile["wdms"]["cell"] = target_at(self.snapshots["wdms"], point)
            if query.count(context) == 1:
                query.snapshot(None, context)
            remote = profile["remote"]
            self.driver.focus(remote["root"], context)
            remote["anchor_hash"] = self.bridge.call("hash", {"root": remote["root"], "rect": remote["anchor"]}, context)["hash"]
            regions = self.bridge.call("ocr", {"root": remote["root"], "rects": [remote["results"]]}, context)["regions"]
            if not isinstance(regions, list) or len(regions) != 1:
                fail("MOYU_REMOTE_UNREADABLE", "结果区域无法读取，请在远程截图重新框选文件名列表。")
            if self.bridge.call("hash", {"root": remote["root"], "rect": remote["anchor"]}, context)["hash"] != remote["anchor_hash"]:
                fail("MOYU_REMOTE_LAYOUT_CHANGED", "自动固定区包含动态内容，只需重框远程固定菜单区域。")
            context.check()
            save_profile(profile)
            record(self.log, "snapshot_calibration_saved", schema=3, image_persisted=False)
            return profile
        self.job(finish, "saved")

    def poll(self):
        try:
            kind, ok, result = self.queue.get_nowait()
        except queue.Empty:
            self.window.after(100, self.poll)
            return
        self.busy = False
        self.capture_button.configure(state="normal")
        self.master.deiconify()
        self.window.deiconify()
        if self.quitting:
            self.close()
            return
        if not ok:
            messagebox.showerror("检查未通过", result+"\n已选项目保留，只需修改有问题的项。", parent=self.window)
        elif kind == "windows":
            self.windows = [item for item in result if item["pid"] != os.getpid()]
            self.selector["values"] = [item["title"] for item in self.windows]
            self.selector.set("")
            self.role_choices.clear()
            self.show()
        elif kind == "capture":
            section, snapshot = result
            previous = self.snapshots.get(section)
            same = previous and all(previous["root"][key] == snapshot["root"][key] for key in ("hwnd", "pid", "rect", "dpi"))
            found = automatic(snapshot) if section == "wdms" else {}
            if same:
                found.update(refresh_marks(snapshot, self.markings[section]))
            else:
                self.manual[section].clear()
            self.snapshots[section] = snapshot
            self.markings[section] = fill_derived(snapshot, found)
            self.section.set("1. WDMS" if section == "wdms" else "2. 远程桌面")
            self.show()
        elif kind == "saved":
            self.saved(result)
            self.close()
            return
        self.window.after(100, self.poll)

    def close(self):
        if self.busy:
            self.quitting = True
            self.cancel.set()
            self.prompt.set("正在结束本次只读检查，请稍候。")
            return
        self.bridge.close()
        self.snapshots.clear()
        self.photo = None
        self.master.deiconify()
        self.window.grab_release()
        self.window.destroy()
