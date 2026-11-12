"""运行 python -m unittest discover -s tests -v 检查四条业务链。"""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook

from inputs import InputError, check_inputs, read_demo
from orders import new_draft, solve_order, to_grams, update_draft, validate_draft
from planning import generate_plan
from storage import OrderStore, order_xlsx

ROOT = Path(__file__).resolve().parents[1]
BROCCOLI = "102900005116714"


def two_item_plan():
    return {
        "id": "training-example", "batch_id": "training-example", "file_hashes": [],
        "cutoff": "2023-06-30", "day": "2023-07-01", "model": "test", "engine": "test",
        "calibration": "test", "price_mode": "固定价格", "week": [], "weights": [0.25, 0.5, 0.25],
        "rules": {"min_count": 2, "max_count": 2, "min_g": 2500, "coverage": 0.85},
        "candidates": {sku: {"name": sku, "category": "测试菜", "cost": 4, "retail": 7,
                             "loss": 0, "demand": [10, 10, 10]} for sku in ("A", "B")},
        "reference": {"测试菜": 20}, "initial": {"A": 10000, "B": 8000},
    }


class TestAuditAndExport(unittest.TestCase):
    def test_01_change_remains_usable(self):
        plan = two_item_plan()
        draft = new_draft(plan)
        items = deepcopy(draft["items"])
        items["A"]["qty_g"] = to_grams("9")
        result = update_draft(draft, items, "人工审单")
        audit = validate_draft(plan, result)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["sales"], 17)
        self.assertEqual(audit["spending"], 68)
        self.assertEqual(draft["items"]["A"]["qty_g"], 10000)
        items["A"]["qty_g"] = 2000
        result = update_draft(result, items, "人工审单")
        self.assertTrue(any("陈列起订量" in error for error in validate_draft(plan, result)["errors"]))
        with self.assertRaises(ValueError):
            to_grams("1.0001")

    def test_02_reallocation_and_locked_conflict(self):
        plan = two_item_plan()
        draft = new_draft(plan)
        draft["items"]["A"]["cap_g"] = 8000
        draft["items"]["B"]["cap_g"] = 9000
        audit = validate_draft(plan, draft)
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["sales"], 16)
        self.assertEqual(audit["spending"], 72)
        solved = solve_order(plan, draft)
        self.assertEqual(solved["status"], "optimal")
        self.assertEqual(solved["quantities"], {"A": 8000, "B": 9000})
        self.assertEqual(draft["items"]["A"]["qty_g"], 10000)
        draft["items"]["A"]["locked"] = True
        impossible = solve_order(plan, draft)
        self.assertEqual(impossible["status"], "infeasible")
        self.assertTrue(any("锁定 10 kg" in message for message in impossible["messages"]))

    def test_03_confirm_rejects_invalid_or_stale_and_exports_saved_versions(self):
        with TemporaryDirectory() as folder:
            store = OrderStore(Path(folder) / "test.sqlite")
            plan = store.save_plan(two_item_plan())
            draft = new_draft(plan)
            store.save_draft(draft)
            items = deepcopy(draft["items"])
            items["A"]["qty_g"] = 9500
            updated = update_draft(draft, items, "人工审单")
            store.save_draft(updated)
            with self.assertRaisesRegex(ValueError, "版本"):
                store.confirm(draft["id"], 0)
            order_a = store.confirm(updated["id"], 1)
            self.assertEqual(store.confirm(updated["id"], 1)["id"], order_a["id"])

            clone = store.clone_order(order_a["id"])
            items = deepcopy(clone["items"])
            items["A"]["qty_g"] = 2000
            invalid = update_draft(clone, items, "人工审单")
            store.save_draft(invalid)
            with self.assertRaisesRegex(ValueError, "未通过"):
                store.confirm(clone["id"], 1)
            items["A"]["qty_g"] = 9000
            new = update_draft(invalid, items, "人工审单")
            store.save_draft(new)
            order_b = store.confirm(new["id"], 2)
            self.assertNotEqual(order_a["id"], order_b["id"])

            def read(order):
                book = load_workbook(BytesIO(order_xlsx(order)), read_only=True, data_only=True)
                try:
                    return {sheet.title: list(sheet.values) for sheet in book}
                finally:
                    book.close()

            export_a = read(order_a)
            export_b = read(order_b)
            self.assertEqual(export_a, read(store.load_order(order_a["id"])))
            self.assertEqual(export_a["采购明细"][1][5], 9.5)
            self.assertEqual(export_b["采购明细"][1][5], 9)
            self.assertIn(("数据截止日", "2023-06-30", None, None, None), export_b["订单信息"])
            self.assertEqual(len(export_a["修改记录"]), 2)
            self.assertEqual(len(export_b["修改记录"]), 4)


class TestOriginalAttachments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = read_demo(ROOT / "data/demo")
        cls.batch = check_inputs(cls.files)
        cls.plan = generate_plan(cls.batch, "2023-06-30")

    def test_04_replay_original_and_broccoli_shortage(self):
        plan = self.plan
        self.assertEqual(len(plan["week"]), 42)
        self.assertEqual(len(plan["candidates"]), 49)
        self.assertEqual(plan["calibration"], "2023-04")
        self.assertEqual(sorted({row["day"] for row in plan["week"]})[::6], ["2023-07-01", "2023-07-07"])
        draft = new_draft(plan)
        audit = validate_draft(plan, draft)
        self.assertTrue(audit["ok"], audit["errors"])
        self.assertEqual(audit["selected"], 33)
        self.assertAlmostEqual(audit["quantity_g"]/1000, 425.075, delta=0.01)
        self.assertAlmostEqual(audit["contribution"], 615.27, delta=0.02)
        draft["items"][BROCCOLI]["cap_g"] = 0
        result = solve_order(plan, draft)
        self.assertEqual(result["status"], "infeasible")
        self.assertTrue(any("花菜类" in message and "仍差" in message for message in result["messages"]))

    def test_05_new_cutoff_and_altered_upload(self):
        earlier = generate_plan(self.batch, "2023-06-29")
        self.assertEqual(earlier["day"], "2023-06-30")
        self.assertNotEqual(earlier["id"], self.plan["id"])
        wb = load_workbook(BytesIO(self.files[2]))
        sheet = wb["Sheet1"]
        for row in sheet.iter_rows(min_row=2, min_col=3, max_col=3):
            row[0].value *= 1.01
        modified = BytesIO()
        wb.save(modified)
        wb.close()
        variant = check_inputs((*self.files[:2], modified.getvalue(), self.files[3]))
        plan = generate_plan(variant, self.plan["cutoff"])
        self.assertNotEqual(plan["batch_id"], self.plan["batch_id"])
        self.assertEqual([r["forecast"] for r in plan["week"]], [r["forecast"] for r in self.plan["week"]])
        self.assertAlmostEqual(plan["week"][0]["cost"], self.plan["week"][0]["cost"] * 1.01)
        self.assertNotAlmostEqual(plan["week"][0]["contribution"], self.plan["week"][0]["contribution"])

    def test_06_missing_column_names_the_file(self):
        wb = load_workbook(BytesIO(self.files[0]))
        wb["Sheet1"]["A1"] = "错误表头"
        modified = BytesIO()
        wb.save(modified)
        wb.close()
        with self.assertRaisesRegex(InputError, r"附件1.*缺少字段.*单品编码"):
            check_inputs((modified.getvalue(), *self.files[1:]))


if __name__ == "__main__":
    unittest.main()
