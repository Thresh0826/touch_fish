"""Temporary synthetic task ports; never initialize native APIs or real accounts."""
from datetime import date
from io import BytesIO
from pathlib import Path
import zipfile

from moyu_tools.engine import Engine
from moyu_tools.model import Plan, Policy
from moyu_tools.store import Store


class Clock:
    def __init__(self):
        self.now = 0.0
        self.hooks = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)
        for hook in self.hooks:
            hook(self.now)


def zip_bytes(text=b"synthetic delivery document"):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("synthetic.txt", text)
    return output.getvalue()


class Query:
    def __init__(self, clock, count=1, number="001234"):
        self.clock, self.count, self.number = clock, count, number
        self.started, self.clicks = None, 0
        self.dates, self.confirmations = [], 0
        self.busy_seconds, self.never_busy, self.never_responds = 2, False, False

    def attach(self, context):
        context.check()

    def set_dates(self, segment, context):
        context.check()
        self.dates.append(segment)

    def confirm(self, segment, context):
        context.check()
        self.confirmations += 1

    def guard_conditions(self, context):
        context.check()

    def click(self, context):
        context.check()
        self.started = self.clock()
        self.clicks += 1

    def signal(self, segment, context):
        context.check()
        return {"busy": not self.never_busy and self.started is not None and self.clock()-self.started < self.busy_seconds,
                "responsive": not self.never_responds}

    def snapshot(self, segment, context):
        context.check()
        return {"count": self.count, "number": self.number if self.count else None}


class Remote:
    def __init__(self):
        self.searches, self.pastes = [], []
        self.data = zip_bytes()
        self.name = "987654.zip"  # Deliberately unrelated to delivery 001234.
        self.after_paste = None

    def search(self, number, context):
        context.check()
        self.searches.append(number)
        return {"name": self.name, "size": len(self.data), "sequence": len(self.searches)+10}

    def paste(self, folder, selected, context):
        context.check()
        self.pastes.append(folder)
        (Path(folder)/selected["name"]).write_bytes(self.data)
        if self.after_paste:
            self.after_paste()


def environment(folder, plan=None, binding_digest="synthetic-binding", condition_signature=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder/"output"
    output.mkdir(exist_ok=True)
    clock = Clock()
    plan = plan or Plan(date(2024, 2, 25), date(2024, 3, 2), 7)
    store = Store(folder/"task")
    store.load_or_create(plan, output, binding_digest, condition_signature)
    query, remote = Query(clock), Remote()
    policy = Policy(initial_wait=3, settle=1, query_timeout=12, search_timeout=8,
                    copy_timeout=8, file_settle=0.5, poll=0.1)
    engine = Engine(plan, policy, store, query, remote, clock=clock, sleep=clock.sleep, wall=clock)
    return engine, store, query, remote, clock, output


def profile():
    root = {"hwnd": 100, "pid": 10, "class_name": "SyntheticWDMS", "rect": [0, 0, 900, 700],
            "dpi": 96, "visible": True, "minimized": False}
    wdms = {"root": root, "conditions": [0, 100, 880, 230], "conditions_hash": 'b'*64}
    locations = [(100,150),(250,150),(400,260),(745,310),(800,310),(700,340),(700,365)]
    for index, key in enumerate(("start", "end", "query", "count_label", "count", "header", "cell")):
        x,y=locations[index]
        wdms[key] = {"hwnd": 101+index, "pid": 10, "class_name": "SyntheticControl",
                     "point": [x,y], "rect": [x-5,y-5,x+30,y+20]}
    remote_root = {**root, "hwnd": 200, "pid": 20, "class_name": "SyntheticRDP"}
    remote = {"root": remote_root, "anchor": [10, 10, 80, 30], "search": [20, 50, 300, 80],
              "results": [20, 100, 500, 400], "count": [500, 500, 550, 520], "anchor_hash": "a"*64}
    return {"schema": 1, "wdms": wdms, "remote": remote}


def sheet_profile():
    value = profile()
    value["schema"] = 2
    wdms = value["wdms"]
    wdms["column_mode"] = "visible"
    for key, handle, x, cls in (("contract_label", 108, 610, "Static"), ("contract", 109, 720, "WindowsForms10.EDIT.app.synthetic")):
        wdms[key] = {"hwnd": handle, "pid": 10, "class_name": cls, "point": [x, 195], "rect": [x-5, 185, x+60, 215]}
    return value
