"""第 5 段：保存草稿版本，按订单号确认、读取和导出。"""
from contextlib import contextmanager
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from orders import SCALE, now, validate_draft


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class OrderStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, plan_id TEXT NOT NULL,
                    content TEXT NOT NULL, PRIMARY KEY(id, revision),
                    FOREIGN KEY(plan_id) REFERENCES plans(id)
                );
                CREATE TABLE IF NOT EXISTS confirmed (
                    id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    content TEXT NOT NULL, UNIQUE(draft_id, revision),
                    FOREIGN KEY(draft_id, revision) REFERENCES drafts(id, revision)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def save_plan(self, plan):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO plans VALUES (?, ?)", (plan["id"], encode(plan)))
            return json.loads(db.execute("SELECT content FROM plans WHERE id=?", (plan["id"],)).fetchone()[0])

    def load_plan(self, plan_id):
        with self.connection() as db:
            row = db.execute("SELECT content FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError("计划不存在。")
        return json.loads(row[0])

    def save_draft(self, draft):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT MAX(revision) FROM drafts WHERE id=?", (draft["id"],)).fetchone()[0]
            if draft["revision"] != (0 if current is None else current + 1):
                raise ValueError("草稿版本已变化，请刷新页面后再编辑。")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?)",
                       (draft["id"], draft["revision"], draft["plan_id"], encode(draft)))

    def load_draft(self, draft_id):
        with self.connection() as db:
            row = db.execute("SELECT content FROM drafts WHERE id=? ORDER BY revision DESC LIMIT 1",
                             (draft_id,)).fetchone()
        if row is None:
            raise ValueError("草稿不存在。")
        return json.loads(row[0])

    def confirm(self, draft_id, revision):
        # 整个确认步骤在一个写事务里完成，重复提交返回相同订单。
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            args = (draft_id, revision)
            row = db.execute("SELECT content FROM confirmed WHERE draft_id=? AND revision=?", args).fetchone()
            if row:
                return json.loads(row[0])
            latest = db.execute("SELECT revision, plan_id, content FROM drafts WHERE id=? "
                                "ORDER BY revision DESC LIMIT 1", (draft_id,)).fetchone()
            if latest is None or latest[0] != revision:
                raise ValueError("草稿版本已变化，请先查看最新草稿。")
            _, plan_id, content = latest
            draft = json.loads(content)
            plan = json.loads(db.execute("SELECT content FROM plans WHERE id=?", (plan_id,)).fetchone()[0])
            audit = validate_draft(plan, draft)
            if not audit["ok"]:
                raise ValueError("草稿未通过检查：" + "；".join(audit["errors"]))
            metadata = {key: val for key, val in plan.items()
                        if key not in ("candidates", "week", "initial", "reference")}
            snapshot = {
                "id": "O-" + uuid4().hex[:12].upper(), "confirmed_at": now(),
                "metadata": metadata, "draft": draft,
                "audit": {**audit, "rows": [row for row in audit["rows"] if row["qty_g"] > 0]},
                "initial": plan["initial"],
            }
            db.execute("INSERT INTO confirmed VALUES (?, ?, ?, ?)", (snapshot["id"], *args, encode(snapshot)))
            return snapshot

    def load_order(self, order_id):
        with self.connection() as db:
            row = db.execute("SELECT content FROM confirmed WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("找不到已确认的订单。")
        return json.loads(row[0])

    def orders(self):
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM confirmed ORDER BY rowid DESC")]

    def order_id_for(self, draft):
        with self.connection() as db:
            row = db.execute("SELECT id FROM confirmed WHERE draft_id=? AND revision=?",
                             (draft["id"], draft["revision"])).fetchone()
        return row[0] if row else None

    def clone_order(self, order_id):
        snapshot = self.load_order(order_id)
        draft = deepcopy(snapshot["draft"])
        draft.update(id=uuid4().hex[:12], revision=0, source_order=order_id)
        self.save_draft(draft)
        return draft


def order_xlsx(snapshot):
    """从确认快照生成文件，网页的实时编辑状态无需传给本函数。"""
    wb = Workbook()
    detail = wb.active
    detail.title = "采购明细"
    info, history = wb.create_sheet("订单信息"), wb.create_sheet("修改记录")
    plan, draft, audit = snapshot["metadata"], snapshot["draft"], snapshot["audit"]
    detail.append(["订货日期", "单品编码", "单品名称", "品类", "初始量(kg)", "最终订货量(kg)", "单位",
                   "供货上限(kg)", "锁定", "预计进货价(元/kg)", "参考零售价(元/kg)", "预计采购金额(元)"])
    for row in audit["rows"]:
        sku, cap = row["sku"], row["cap_g"]
        detail.append([
            plan["day"], sku, row["name"], row["category"], snapshot["initial"][sku] / SCALE,
            row["qty_g"] / SCALE, "kg", None if cap is None else cap / SCALE,
            row["locked"], row["cost"], row["retail"], row["spending"],
        ])
        for col in (5, 6, 8):
            detail.cell(detail.max_row, col).number_format = "0.000"
        for col in (10, 11, 12):
            detail.cell(detail.max_row, col).number_format = "0.00"
    detail.auto_filter.ref = detail.dimensions
    info.append(["字段", "值"])
    for key, value in [
        ("确认单号", snapshot["id"]), ("确认时间(UTC)", snapshot["confirmed_at"]),
        ("草稿", f"{draft['id']}-v{draft['revision']}"), ("来源订单", draft["source_order"]),
        ("计划编号", plan["id"]), ("数据批次", plan["batch_id"]), ("数据截止日", plan["cutoff"]),
        ("订货日期", plan["day"]), ("模型版本", plan["model"]), ("计算版本", plan["engine"]),
        ("误差校准月", plan["calibration"]), ("价格模式", plan["price_mode"]), ("业务规则", encode(plan["rules"])),
        ("需求情景权重", encode(plan["weights"])), ("四张附件的指纹", encode(plan["file_hashes"])),
        ("选中单品数", audit["selected"]), ("总订货量(kg)", audit["quantity_g"]/SCALE),
        ("预计采购金额(元)", audit["spending"]), ("模拟贡献(元)", audit["contribution"]),
        ("供货未核实的已订单品", sum(row["cap_g"] is None for row in audit["rows"])),
        ("用途", "历史数据模拟"),
    ]:
        info.append([key, value])
    info.append(["品类", "预计销量(kg)", "参考需求(kg)", "覆盖率", "门槛销量(kg)"])
    for row in audit["categories"]:
        info.append([row["category"], row["sold"], row["demand"], row["coverage"], row["target"]])
        info.cell(info.max_row, 4).number_format = "0.0%"
    history.append(["草稿编号", "版本", "时间(UTC)", "单品编码", "字段", "原值", "新值", "来源"])
    names = {"qty_g": "订货量(kg)", "cap_g": "供货上限(kg)", "locked": "锁定"}
    for change in draft["history"]:
        field = change["field"]
        old, new = change["before"], change["after"]
        if field in ("qty_g", "cap_g"):
            old = None if old is None else old/SCALE
            new = None if new is None else new/SCALE
        history.append([change["draft_id"], change["revision"], change["time"], change["sku"], names[field], old, new, change["source"]])
    for sheet, widths in ((detail, (15, 21, 26, 17, 16, 18, 10, 17, 10, 22, 22, 23)),
                          (info, (26, 45, 23, 16, 20)), (history, (18, 11, 30, 21, 19, 16, 16, 25))):
        sheet.freeze_panes = "A2"
        for cell, width in zip(sheet[1], widths):
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="175B46")
            sheet.column_dimensions[cell.column_letter].width = width
        # Force formula-looking names from uploaded workbooks to remain inert text.
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
