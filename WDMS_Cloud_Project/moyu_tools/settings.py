"""Personal settings and explicit local window-binding profile."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from moyu_tools.model import Policy, digest, fail
from services.status_manager import atomic_json_write


def state_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home()/".local"/"share"))/"MoyuTools"/"sheet_rows_simple"


def task_lock_path():
    # Share the v1 lock, while keeping its settings, binding and progress intact.
    return state_root().parent/"task.lock"


def inventory_lock_path():
    return state_root().parent.parent/"WDMSInventoryDemo"/"workflow.lock"


def load_json(path):
    try:
        if Path(path).stat().st_size > 32 * 1024 * 1024 or Path(path).is_symlink():
            fail("MOYU_CONFIG_INVALID", "本地配置无效或过大。")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dictionary")
        return data
    except (OSError, ValueError) as error:
        fail("MOYU_CONFIG_INVALID", "配置无法读取，请保留原文件并重新设置。")


def defaults():
    return {"plan": None, "policy": asdict(Policy()), "output": "", "task": ""}


def validate_profile(profile):
    def require(condition):
        if not condition:
            raise ValueError("invalid binding")
    try:
        require(type(profile["schema"]) is int and profile["schema"] in (1, 2, 3))
        for name in ("wdms", "remote"):
            section = profile[name]
            root = section["root"]
            require(type(root["hwnd"]) is int and root["hwnd"] > 0 and type(root["pid"]) is int and root["pid"] > 0)
            require(isinstance(root["class_name"], str) and bool(root["class_name"]))
            require(type(root["dpi"]) is int and root["dpi"] > 0)
            rect = root["rect"]
            require(len(rect) == 4 and all(type(x) is int for x in rect))
            require(rect[0] < rect[2] and rect[1] < rect[3])
        require(profile["wdms"]["root"]["hwnd"] != profile["remote"]["root"]["hwnd"])
        keys = ("start", "end", "query", "count_label", "count", "header", "cell")
        if profile["schema"] >= 2:
            keys += ("contract_label", "contract")
            require(profile["wdms"]["column_mode"] == "visible")
        for key in keys:
            target = profile["wdms"][key]
            root = profile["wdms"]["root"]
            require(target["pid"] == root["pid"] and type(target["hwnd"]) is int)
            x, y = target["point"]
            require(root["rect"][0] <= x < root["rect"][2] and root["rect"][1] <= y < root["rect"][3])
        require(profile["wdms"]["start"]["hwnd"] != profile["wdms"]["end"]["hwnd"])
        points = {key: profile["wdms"][key]["point"] for key in ("start", "end", "count_label", "count", "header", "cell")}
        tolerance = 20*profile["wdms"]["root"]["dpi"]/96
        require(points["start"][0] < points["end"][0] and abs(points["start"][1]-points["end"][1]) <= tolerance)
        require(points["count_label"][0] < points["count"][0]
                and abs(points["count_label"][1]-points["count"][1]) <= tolerance)
        require(points["count"][1] < points["header"][1] < points["cell"][1])
        conditions = profile["wdms"]["conditions"]
        bounds = profile["wdms"]["root"]["rect"]
        require(len(conditions) == 4 and all(type(x) is int for x in conditions))
        require(bounds[0] <= conditions[0] < conditions[2] <= bounds[2]
                and bounds[1] <= conditions[1] < conditions[3] <= bounds[3])
        inputs = ("start", "end", "contract") if profile["schema"] >= 2 else ("start", "end")
        require(len({profile["wdms"][name]["hwnd"] for name in inputs}) == len(inputs))
        for name in inputs:
            rect = profile["wdms"][name]["rect"]
            require(conditions[0] <= rect[0] < rect[2] <= conditions[2]
                    and conditions[1] <= rect[1] < rect[3] <= conditions[3])
        if profile["schema"] >= 2:
            label, field = profile["wdms"]["contract_label"], profile["wdms"]["contract"]
            require(label["point"][0] < field["point"][0]
                    and abs(label["point"][1]-field["point"][1]) <= tolerance)
            require(re.fullmatch(r"(?:.*\.)?EDIT(?:\..*)?", field["class_name"], re.IGNORECASE) is not None)
        require(isinstance(profile["wdms"]["conditions_hash"], str)
                and re.fullmatch(r"[a-f0-9]{64}", profile["wdms"]["conditions_hash"]) is not None)
        if profile["schema"] == 3:
            require(profile["remote"]["mode"] == "single_zip")
        for key in (("anchor", "search", "results") if profile["schema"] == 3 else ("anchor", "search", "results", "count")):
            rect, root = profile["remote"][key], profile["remote"]["root"]["rect"]
            require(len(rect) == 4 and all(type(x) is int for x in rect))
            require(root[0] <= rect[0] < rect[2] <= root[2] and root[1] <= rect[1] < rect[3] <= root[3])
            require((rect[2]-rect[0]) * (rect[3]-rect[1]) <= 8000000)
        require(isinstance(profile["remote"]["anchor_hash"], str)
                and re.fullmatch(r"[a-f0-9]{64}", profile["remote"]["anchor_hash"]) is not None)
        require(type(profile["remote"].get("submit_enter", False)) is bool)
    except (KeyError, TypeError, ValueError):
        fail("MOYU_BINDING_INVALID", "定位配置不完整或位置越界，请重新完成首次定位。")
    return profile


def save_profile(profile):
    validate_profile(profile)
    path = state_root()/"binding.json"
    if path.exists() and load_json(path).get("schema") != profile["schema"]:
        shutil.copyfile(path, path.with_name("binding_previous_"+uuid.uuid4().hex+".json"))
    atomic_json_write(path, profile)


def profile_digest(profile):
    return digest(validate_profile(profile))
