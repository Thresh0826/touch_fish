"""Snapshot geometry and conservative native candidates, independent of Tk/Win32."""
from __future__ import annotations

import copy
import re

from moyu_tools.model import fail

FIELDS = (
    ("start", "开始日期", "point"), ("end", "截止日期", "point"),
    ("contract", "合同编号输入框", "point"), ("query", "查询按钮", "point"),
    ("count", "上方送货单总行数", "point"), ("header", "送货单号表头", "point"),
    ("contract_label", "合同编号文字（必要时补选）", "point"),
    ("count_label", "上方总行数文字（必要时补选）", "point"),
    ("cell", "第一行单号位置（必要时补选）", "point"),
    ("conditions", "整个查询条件区（必要时补框）", "rect"),
)
REMOTE_FIELDS = (("search", "搜索输入框", "rect"), ("results", "ZIP结果列表", "rect"),
                 ("anchor", "固定菜单区域（自动生成，可重框）", "rect"))


def screen_point(snapshot, x, y):
    width, height = snapshot["width"], snapshot["height"]
    if not (0 <= x < width and 0 <= y < height):
        fail("MOYU_POINT_INVALID", "请在截图内点选。")
    left, top, right, bottom = snapshot["source_rect"]
    return [left+int(x*(right-left)/width), top+int(y*(bottom-top)/height)]


def screen_rect(snapshot, first, last):
    a, b = screen_point(snapshot, *first), screen_point(snapshot, *last)
    rect = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
    if rect[2]-rect[0] < 4 or rect[3]-rect[1] < 4:
        fail("MOYU_POINT_INVALID", "请拖出完整的区域。")
    return rect


def canvas_rect(snapshot, rect):
    left, top, right, bottom = snapshot["source_rect"]
    return [(rect[0]-left)*snapshot["width"]/(right-left), (rect[1]-top)*snapshot["height"]/(bottom-top),
            (rect[2]-left)*snapshot["width"]/(right-left), (rect[3]-top)*snapshot["height"]/(bottom-top)]


def target_at(snapshot, point):
    candidates = [item for item in snapshot["controls"] if item["rect"][0] <= point[0] < item["rect"][2]
                  and item["rect"][1] <= point[1] < item["rect"][3]]
    target = min(candidates, key=lambda item: (item["rect"][2]-item["rect"][0])*(item["rect"][3]-item["rect"][1])) if candidates else snapshot["root"]
    return {**copy.deepcopy(target), "point": list(point)}


def refresh_marks(snapshot, found):
    return {key: target_at(snapshot, value["point"]) if isinstance(value, dict) else copy.deepcopy(value)
            for key, value in found.items()}


def normalize(text):
    return re.sub(r"\s", "", text or "").replace("&", "").rstrip(":：")


def automatic(snapshot):
    controls, found = snapshot["controls"], {}
    def unique(key, values):
        if len(values) == 1:
            found[key] = copy.deepcopy(values[0])
    dates = sorted([item for item in controls if "datetimepick" in item["class_name"].casefold()], key=lambda item: item["point"][0])
    if len(dates) == 2 and abs(dates[0]["point"][1]-dates[1]["point"][1]) < 30:
        found.update(start=copy.deepcopy(dates[0]), end=copy.deepcopy(dates[1]))
    unique("contract_label", [item for item in controls if normalize(item.get("text")) == "合同编号"])
    if "contract_label" in found:
        x, y = found["contract_label"]["point"]
        edits = [item for item in controls if re.fullmatch(r"(?:.*\.)?EDIT(?:\..*)?", item["class_name"], re.IGNORECASE)
                 and item["rect"][0] > x and abs(item["point"][1]-y) < 20]
        if edits:
            distance = min(item["rect"][0] for item in edits)
            unique("contract", [item for item in edits if item["rect"][0] == distance])
    unique("query", [item for item in controls if re.fullmatch(r"查询(?:[（(][A-Za-z][）)])?", normalize(item.get("text")))])
    totals = sorted([item for item in controls if re.fullmatch(r"总行数[:：]?(?:[0-9]+行?)?", normalize(item.get("text")))], key=lambda item: item["point"][1])
    if totals:
        found["count_label"] = copy.deepcopy(totals[0])
        label = totals[0]
        digits = [item for item in controls if re.fullmatch(r"[0-9]+(?:行)?", normalize(item.get("text")))
                  and item["rect"][0] > label["point"][0] and abs(item["point"][1]-label["point"][1]) < 20]
        if digits:
            distance = min(item["rect"][0] for item in digits)
            unique("count", [item for item in digits if item["rect"][0] == distance])
    groups = [item for item in controls if normalize(item.get("text")) == "查询条件"]
    if len(groups) == 1:
        found["conditions"] = list(groups[0]["rect"])
    for key in ("header", "cell"):
        unique(key, [item for item in controls if item.get("hint") == key])
    return found


def fill_derived(snapshot, found):
    if "header" in found and "cell" not in found:
        rect = found["header"].get("accessible_rect")
        # The actual MSAA cell and its column bounds are checked after querying.
        y = (rect[3]+max(8, (rect[3]-rect[1])//2)) if rect else found["header"]["point"][1]+max(16, int(22*snapshot["root"]["dpi"]/96))
        point = [found["header"]["point"][0], y]
        found["cell"] = target_at(snapshot, point)
    if "search" in found and "anchor" not in found:
        x1, y1, x2, _ = found["search"]
        area = snapshot["source_rect"]
        top = max(area[1], y1-28)
        if y1-top >= 8:
            found["anchor"] = [x1, top, min(x2, x1+180), y1-2]
    return found
