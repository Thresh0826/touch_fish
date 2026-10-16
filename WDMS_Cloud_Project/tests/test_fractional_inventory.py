from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from adapters.base import InventoryResult, Owner, QueryTicket
from adapters.real_wdms import RealWDMSAdapter
from core.inventory_service import InventoryService
from core.numbers import parse_nonnegative_quantity, quantity_number_format
from errors import WDMSError
from services.excel_reporter import ExcelReporter


class TestFractionalInventory(unittest.TestCase):
    def test_site_readings_and_grouped_decimals_are_preserved(self):
        for raw, expected in (("1312325.54", "1312325.54"), ("1,312,325.54", "1312325.54"), ("32772080.54", "32772080.54"), ("0", "0"), ("0.54", "0.54")):
            self.assertEqual(parse_nonnegative_quantity(raw, "散件数"), Decimal(expected))
        self.assertEqual(quantity_number_format(Decimal("1312325.5400")), "#,##0.00")

    def test_invalid_or_precision_losing_values_are_rejected(self):
        for value in (True, "-1.2", "3,12.54", "1.2.3", "NaN", float("inf"), "", "12 件"):
            with self.subTest(value=value):
                with self.assertRaises(WDMSError):
                    parse_nonnegative_quantity(value, "散件数")
        for value in ("999999999999999.54", "0.1234567890123456", "1000000000000000"):
            with self.subTest(value=value):
                with self.assertRaises(WDMSError) as caught:
                    parse_nonnegative_quantity(value, "散件数")
                self.assertEqual(caught.exception.code, "COUNT_PRECISION_RISK")

    def test_real_result_adapter_and_inventory_validation_accept_fractional_loose(self):
        adapter = object.__new__(RealWDMSAdapter)
        owner = Owner("site", "货主测试")
        adapter._request = lambda *args: {"request_id": "req", "owner_id": "site", "owner_name": owner.name, "whole_count": "262,350", "loose_count": "1312325.54", "evidence": {"test": True}}
        result = adapter.read_result(QueryTicket("req", owner.owner_id, owner.name, 0, {}), 15)
        InventoryService._validate_result(owner, "req", result)
        self.assertEqual(result.whole_count, 262350)
        self.assertEqual(result.loose_count, Decimal("1312325.54"))

    def test_saved_excel_contains_numeric_fraction_and_correct_display(self):
        record = InventoryResult("req", Owner("site", "货主测试"), 262350, Decimal("1312325.54"), {})
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = ExcelReporter().publish(Path(temporary), date(2026, 10, 10), "mock", "test", "run", 1, 24, [record])
            book = load_workbook(path)
            try:
                sheet = book["库存分析"]
                self.assertEqual(sheet["B2"].value, 262350)
                self.assertEqual(Decimal(str(sheet["C2"].value)), Decimal("1312325.54"))
                self.assertEqual(sheet["C2"].data_type, "n")
                self.assertEqual(sheet["C2"].number_format, "#,##0.00")
            finally:
                book.close()


if __name__ == "__main__":
    unittest.main()
