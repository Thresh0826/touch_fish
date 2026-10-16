"""Report identity shared by naming, publication and verification."""
from datetime import date
from errors import WDMSError

HEADERS = ("货主", "整件数", "散件数")


def inventory_report_title(task_date: date) -> str:
    if not isinstance(task_date, date):
        raise WDMSError("REPORT_INPUT_INVALID", "报表日期必须是date类型。")
    return f"{task_date.year}年{task_date.month}月{task_date.day}日物流库存"
