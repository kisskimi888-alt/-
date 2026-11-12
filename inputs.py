"""第 1 段：核查四张 Excel，并把同一个单品在不同表里的记录接起来。"""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

CATEGORY_SHEET = "平均损耗率(%)_小分类编码_不同值"
FILES = tuple(f"附件{i}.xlsx" for i in range(1, 5))
SCHEMAS = {
    "products": (0, "Sheet1", {
        "单品编码": "sku", "单品名称": "name", "分类编码": "category_id", "分类名称": "category",
    }),
    "sales": (1, "Sheet1", {
        "销售日期": "day", "扫码销售时间": "time", "单品编码": "sku", "销量(千克)": "qty",
        "销售单价(元/千克)": "price", "销售类型": "kind", "是否打折销售": "discount",
    }),
    "costs": (2, "Sheet1", {"日期": "day", "单品编码": "sku", "批发价格(元/千克)": "cost"}),
    "losses": (3, "Sheet1", {"单品编码": "sku", "单品名称": "name", "损耗率(%)": "loss"}),
    "category_losses": (3, CATEGORY_SHEET, {
        "小分类编码": "category_id", "小分类名称": "category",
        "平均损耗率(%)_小分类编码_不同值": "loss",
    }),
}


class InputError(ValueError):
    pass


@dataclass
class InputBatch:
    frames: dict
    hashes: tuple
    rows: list
    first_day: str
    last_day: str

    @property
    def batch_id(self):
        return sha256("|".join(self.hashes).encode()).hexdigest()[:16]


def read_demo(folder):
    return tuple((Path(folder) / name).read_bytes() for name in FILES)


def check_inputs(files):
    """返回解析后的数据和核查结果。发现会影响计算的错误时，停止创建新批次。"""
    if len(files) != 4 or not all(files):
        raise InputError("请分别提供四张 Excel。")
    frames, rows, errors = {}, [], []
    books = {}
    try:
        for name, (index, sheet, mapping) in SCHEMAS.items():
            label = f"{FILES[index]} / {sheet}"
            try:
                if index not in books:
                    books[index] = pd.ExcelFile(BytesIO(files[index]), engine="calamine")
                book = books[index]
                if sheet not in book.sheet_names:
                    raise InputError(f"缺少工作表「{sheet}」")
                headers = list(book.parse(sheet, nrows=0).columns)
                missing = set(mapping) - set(headers)
                if missing:
                    raise InputError("缺少字段 " + "、".join(sorted(missing)))
                id_cols = {key: "string" for key, val in mapping.items() if val in ("sku", "category_id")}
                frame = book.parse(sheet, usecols=list(mapping), dtype=id_cols).rename(columns=mapping)
                if frame.empty:
                    raise InputError("工作表没有数据")
                for col in {"sku", "name", "category_id", "category", "kind"} & set(frame):
                    frame[col] = frame[col].astype("string").str.strip()
                    if (frame[col].isna() | frame[col].eq("")).any():
                        raise InputError(f"字段 {col} 有空白")
                if "day" in frame:
                    dates = pd.to_datetime(frame["day"], errors="coerce")
                    if dates.isna().any() or not dates.between("1900-01-01", "2100-01-01").all():
                        raise InputError("存在缺失或无效日期")
                    frame["day"] = dates.dt.normalize()
                for col in {"qty", "price", "cost", "loss"} & set(frame):
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
                    if not np.isfinite(frame[col]).all():
                        raise InputError(f"字段 {col} 有空白或非数字")
                    if col in ("price", "cost") and (frame[col] <= 0).any():
                        raise InputError(f"字段 {col} 必须大于 0")
                    if col == "loss":
                        if not frame[col].between(0, 100, inclusive="left").all():
                            raise InputError("损耗率必须大于等于 0 且小于 100")
                        frame[col] /= 100
                frames[name] = frame
                rows.append({"检查": label, "结果": "通过", "详情": f"{len(frame):,} 行"})
            except Exception as exc:
                errors.append(f"{label}：{exc}")
    finally:
        for book in books.values():
            book.close()
    if errors:
        raise InputError("\n".join(errors))

    products, sales, costs, losses, categories = (frames[k] for k in SCHEMAS)
    for frame, keys, title in (
        (products, ["sku"], "商品"), (costs, ["day", "sku"], "批发价"),
        (losses, ["sku"], "单品损耗"), (categories, ["category_id"], "品类损耗"),
    ):
        if frame.duplicated(keys).any():
            raise InputError(f"{title}出现重复键，请核查 {', '.join(keys)}")
    product_codes = set(products.sku)
    for frame, title in ((sales, "销售流水"), (costs, "批发价"), (losses, "单品损耗")):
        unknown = sorted(set(frame.sku) - product_codes)
        if unknown:
            raise InputError(f"{title}中的编码无法关联商品表，例如 {unknown[0]}")
    if products.groupby("category_id").category.nunique().gt(1).any() or products.groupby("category").category_id.nunique().gt(1).any():
        raise InputError("商品表的品类编码和品类名称需要一一对应")
    if not sales.kind.isin(("销售", "退货")).all() or (
        ((sales.kind == "销售") & (sales.qty <= 0)) | ((sales.kind == "退货") & (sales.qty >= 0))
    ).any():
        raise InputError("销售类型只能是销售或退货，销售量应为正，退货量应为负")
    keys = sales[["day", "sku"]].drop_duplicates().merge(
        costs[["day", "sku"]], how="left", indicator=True,
    )
    missing_cost = keys[keys._merge == "left_only"]
    if not missing_cost.empty:
        row = missing_cost.iloc[0]
        raise InputError(f"{len(missing_cost)} 个日期及单品组合缺少同日批发价，例如 {row.day.date()} / {row.sku}")
    used = products[products.sku.isin(sales.sku)]
    if not set(used.sku).issubset(set(losses.sku)) or not set(used.category_id).issubset(set(categories.category_id)):
        raise InputError("有销售记录的商品缺少单品损耗率或品类损耗率")
    first, last = sales.day.min(), sales.day.max()
    n_missing = len(pd.date_range(first, last)) - sales.day.nunique()
    rows += [
        {"检查": "跨表关联", "结果": "通过", "详情": "商品、同日批发价、损耗率已核查"},
        {"检查": "有交易记录的日期范围", "结果": "通过", "详情": f"{first.date()} 至 {last.date()}"},
        {"检查": "全店无交易日期", "结果": "提示", "详情": f"{n_missing} 天，预测训练保留为缺失"},
    ]
    return InputBatch(frames, tuple(sha256(f).hexdigest() for f in files), rows, str(first.date()), str(last.date()))


def daily_history(batch, cutoff):
    """按日期截断后再汇总。净销量含退货，单价用正向成交量加权。"""
    frames = batch.frames
    sales = frames["sales"]
    cut = sales[sales.day <= cutoff]
    recent = cut[cut.day.between(cutoff - pd.Timedelta(days=6), cutoff)]
    if cut.empty or recent.empty:
        raise InputError("截止日之前的最近七天没有交易，无法形成候选清单")
    joined = cut.merge(frames["products"], on="sku", validate="many_to_one")
    joined = joined.merge(frames["costs"], on=["day", "sku"], validate="many_to_one")
    gross = joined.qty.clip(lower=0)
    joined = joined.assign(gross=gross, revenue=joined.price * gross, spending=joined.cost * gross)
    sums = ["qty", "gross", "revenue", "spending"]
    cat = joined.groupby(["day", "category_id", "category"], as_index=False)[sums].sum()
    cat["retail"] = cat.revenue / cat.gross.replace(0, np.nan)
    cat["wholesale"] = cat.spending / cat.gross.replace(0, np.nan)
    skus = joined[joined.day.between(cutoff - pd.Timedelta(days=6), cutoff)]
    skus = skus.groupby(["sku", "name", "category_id", "category"], as_index=False)[sums].sum()
    skus = skus[(skus.gross > 0) & (skus.qty > 0)].merge(
        frames["losses"][["sku", "loss"]], on="sku", validate="one_to_one",
    )
    skus["retail"] = skus.revenue / skus.gross
    skus["wholesale"] = skus.spending / skus.gross
    return cat, skus
