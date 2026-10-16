"""Strict parsers for inventory counts."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from errors import WDMSError


_PLAIN_INTEGER = re.compile(r"^[0-9]+$")
_GROUPED_INTEGER = re.compile(r"^[1-9][0-9]{0,2}(?:,[0-9]{3})+$")
_PLAIN_QUANTITY = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_GROUPED_QUANTITY = re.compile(r"^[1-9][0-9]{0,2}(?:,[0-9]{3})+(?:\.[0-9]+)?$")
MAX_EXCEL_INTEGER = 999_999_999_999_999


def parse_nonnegative_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise WDMSError("INVALID_COUNT", f"{field_name} 不是有效的非负整数。")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not (_PLAIN_INTEGER.fullmatch(raw) or _GROUPED_INTEGER.fullmatch(raw)):
            raise WDMSError("INVALID_COUNT", f"{field_name} 不是有效的非负整数。")
        parsed = int(raw.replace(",", ""))
    else:
        raise WDMSError("INVALID_COUNT", f"{field_name} 不是有效的非负整数。")
    if parsed < 0:
        raise WDMSError("INVALID_COUNT", f"{field_name} 不是有效的非负整数。")
    if parsed > MAX_EXCEL_INTEGER:
        raise WDMSError("COUNT_PRECISION_RISK", f"{field_name} 超出 Excel 可精确表示的整数范围。")
    return parsed


def parse_nonnegative_quantity(value: Any, field_name: str) -> Decimal:
    """Parse fractional inventory without truncation or silent Excel rounding."""
    invalid = WDMSError("INVALID_COUNT", f"{field_name} 不是有效的非负数。")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise invalid
    if isinstance(value, str):
        raw = value.strip()
        if not (_PLAIN_QUANTITY.fullmatch(raw) or _GROUPED_QUANTITY.fullmatch(raw)):
            raise invalid
        value = raw.replace(",", "")
    try:
        parsed = Decimal(str(value))
    except (ValueError, InvalidOperation) as error:
        raise invalid from error
    if not parsed.is_finite() or parsed < 0:
        raise invalid
    if parsed == 0:
        return Decimal(0)
    digits = list(parsed.as_tuple().digits)
    exponent = parsed.as_tuple().exponent
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    if parsed >= Decimal(10) ** 15 or len(digits) > 15 or exponent < -15:
        raise WDMSError("COUNT_PRECISION_RISK", f"{field_name} 超出本项目 Excel 数值精度范围，不进行截断或四舍五入。")
    return parsed


def quantity_number_format(value: Any) -> str:
    parsed = parse_nonnegative_quantity(value, "散件数")
    if parsed == 0:
        return "#,##0"
    digits = list(parsed.as_tuple().digits)
    exponent = parsed.as_tuple().exponent
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return "#,##0" + ("." + "0" * -exponent if exponent < 0 else "")
