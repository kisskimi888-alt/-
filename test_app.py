"""Streamlit 组件级测试。发送网页编辑器所用的单元格编辑消息。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from streamlit.runtime.memory_uploaded_file_manager import MemoryUploadedFileManager
from streamlit.runtime.uploaded_file_manager import UploadedFileRec
from streamlit.testing.v1 import AppTest

from orders import new_draft
from storage import OrderStore
from test_workflow import two_item_plan

ROOT = Path(__file__).resolve().parents[1]


class TestStreamlitFlow(unittest.TestCase):
    def test_editor_preview_confirmation_and_history(self):
        with TemporaryDirectory() as folder, patch.dict("os.environ", {"ORDER_DESK_DB": str(Path(folder) / "app.db")}):
            store = OrderStore(Path(folder) / "app.db")
            plan = two_item_plan()
            plan["initial_messages"] = []
            plan["week"] = [{"day": plan["day"], "category": "测试菜", "horizon": 1, "forecast": 20,
                             "expected_demand": 20, "qty_g": 20000, "retail": 7, "cost": 4}]
            draft = new_draft(store.save_plan(plan))
            store.save_draft(draft)
            at = AppTest.from_file(str(ROOT / "app.py")).run()
            at.session_state["active_draft"] = draft["id"]
            at.run()

            def button(label):
                return next(b for b in at.button if b.label == label)

            def event(values=None, action=None):
                # AppTest 没有公开的 data_editor 编辑接口，按组件实际使用的 string_value 送入状态。
                if action:
                    button(action).click()
                message = at._tree.get_widget_states()
                if values is not None:
                    frame = next(e for e in at.get("arrow_data_frame") if e.proto.id)
                    data = frame.value
                    rows = {sku: str(data.index[data["单品编码"] == sku][0]) for sku in values}
                    message.widgets.add(id=frame.proto.id, string_value=json.dumps({
                        "edited_rows": {rows[sku]: changes for sku, changes in values.items()},
                        "added_rows": [], "deleted_rows": [],
                    }))
                at._run(message)
                self.assertFalse(at.exception, list(at.exception))

            event({"A": {"订货量(kg)": 9}})
            self.assertEqual(store.load_draft(draft["id"])["items"]["A"]["qty_g"], 9000)
            self.assertFalse(button("确认订单").disabled)
            caps = {"A": {"供货上限(kg)": 8}, "B": {"供货上限(kg)": 9}}
            event(caps)
            self.assertTrue(button("确认订单").disabled)
            event(caps, "按新条件重排")
            self.assertIn("重排预览", [h.value for h in at.subheader])
            self.assertEqual(store.load_draft(draft["id"])["items"]["A"]["qty_g"], 10000)

            unavailable = {**caps, "A": {"供货上限(kg)": 0}}
            event(unavailable)
            self.assertNotIn("重排预览", [h.value for h in at.subheader])
            event(unavailable, "按新条件重排")
            self.assertTrue(any("仍差 8.00 kg" in msg.value for msg in at.warning))
            self.assertTrue(button("确认订单").disabled)

            event(caps)
            event(caps, "按新条件重排")
            event(caps, "应用这份重排")
            self.assertEqual(store.load_draft(draft["id"])["items"]["A"]["qty_g"], 8000)
            self.assertEqual(store.load_draft(draft["id"])["items"]["B"]["qty_g"], 9000)
            event({}, "确认订单")
            self.assertTrue(any("已确认为" in msg.value for msg in at.success))
            self.assertIn("下载这版确认订单", [e.proto.label for e in at.get("download_button")])
            self.assertEqual(len(store.orders()), 1)
            event(action="复制为新草稿")
            self.assertNotEqual(at.session_state["active_draft"], draft["id"])
            self.assertEqual(len(store.orders()), 1)


    def test_four_upload_slots_generate_real_plan(self):
        manager = MemoryUploadedFileManager("/mock/upload")
        with TemporaryDirectory() as folder, patch.dict(
            "os.environ", {"ORDER_DESK_DB": str(Path(folder) / "app.db")},
        ), patch("streamlit.testing.v1.local_script_runner.MemoryUploadedFileManager", return_value=manager):
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
            at.radio[0].set_value("上传 Excel").run()
            uploaders = sorted(at.get("file_uploader"), key=lambda widget: widget.proto.label)
            self.assertEqual(len(uploaders), 4)
            records = [
                UploadedFileRec(str(i), f"附件{i}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                (ROOT / "data/demo" / f"附件{i}.xlsx").read_bytes())
                for i in range(1, 5)
            ]
            for record in records:
                manager.add_file("test session id", record)

            def run_upload(button_label, files):
                if button_label:
                    next(b for b in at.button if b.label == button_label).click()
                state = at._tree.get_widget_states()
                for uploader, record in zip(uploaders, files):
                    value = state.widgets.add(id=uploader.proto.id)
                    value.file_uploader_state_value.uploaded_file_info.add(
                        file_id=record.file_id, name=record.name, size=len(record.data),
                    )
                at._run(state)
                self.assertFalse(at.exception, list(at.exception))

            run_upload(None, records)
            run_upload("核查数据", records)
            self.assertTrue(any(b.label == "生成计划" for b in at.button), [(b.label, b.disabled) for b in at.button] + [e.value for e in at.error])
            run_upload("生成计划", records)
            self.assertIn("425.075 kg", [metric.value for metric in at.metric])
            self.assertTrue(any("已生成" in message.value for message in at.success))
