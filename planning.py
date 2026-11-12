"""第 4 段：把七天预测、品类采购量和首日单品方案接成一个计划。"""
from hashlib import sha256
import json
from math import ceil

import numpy as np
import pandas as pd

from forecast import MODEL_VERSION, WEIGHTS, forecast_week
from inputs import InputError, daily_history
from orders import DEFAULT_RULES, SCALE, new_draft, solve_order, validate_draft

ENGINE_VERSION = "replenishment-v1"


def category_order(scenarios, loss, retail, cost):
    """固定售价时，贡献折线的拐点来自各需求情景，逐个试算即可。"""
    grams = {0}
    for demand in scenarios:
        breakpoint = demand / (1 - loss) * SCALE
        grams.update((int(breakpoint), ceil(breakpoint)))

    def value(g):
        sales = np.dot(WEIGHTS, np.minimum(np.asarray(scenarios), g / SCALE * (1-loss)))
        return float(sales * retail - g / SCALE * cost)

    amount = max(grams, key=lambda g: (value(g), -g))
    return amount, value(amount)


def generate_plan(batch, cutoff):
    cutoff = pd.Timestamp(cutoff).normalize()
    if not pd.Timestamp(batch.first_day) <= cutoff <= pd.Timestamp(batch.last_day):
        raise InputError(f"数据截止日需要在 {batch.first_day} 至 {batch.last_day} 内。")
    history, sku_history = daily_history(batch, cutoff)
    forecasts, calibration = forecast_week(history, cutoff)
    recent = history[history.day.between(cutoff - pd.Timedelta(days=6), cutoff)]
    prices = recent.groupby("category_id")[["retail", "wholesale"]].median()
    losses = batch.frames["category_losses"].set_index("category_id").loss
    week, first_day = [], {}
    for row in forecasts:
        code = row["category_id"]
        if code not in prices.index or prices.loc[code].isna().any():
            raise InputError(f"{row['category']}最近七天缺少价格参考。")
        retail, cost = map(float, prices.loc[code])
        loss = float(losses[code])
        grams, contribution = category_order(row["demand"], loss, retail, cost)
        result = {**row, "qty_g": grams, "retail": retail, "cost": cost,
                  "loss": loss, "contribution": contribution}
        week.append(result)
        if row["horizon"] == 1:
            first_day[code] = result

    totals = sku_history.groupby("category_id").gross.sum()
    candidates = {}
    for row in sku_history.itertuples(index=False):
        # 近期真实成交量占比决定分给该单品的需求，缺货后份额也不会自行流向另一单品。
        share = float(row.gross / totals[row.category_id])
        candidates[str(row.sku)] = {
            "name": str(row.name), "category": str(row.category),
            "cost": float(row.wholesale), "retail": float(row.retail), "loss": float(row.loss),
            "share": share, "demand": [d * share for d in first_day[row.category_id]["demand"]],
        }
    config = {
        "batch_id": batch.batch_id, "file_hashes": list(batch.hashes), "cutoff": str(cutoff.date()),
        "model": MODEL_VERSION, "engine": ENGINE_VERSION, "calibration": calibration,
        "price_mode": "固定近期参考零售价", "rules": DEFAULT_RULES.copy(), "weights": list(WEIGHTS),
    }
    plan_id = sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    plan = {
        **config, "id": plan_id, "day": str((cutoff + pd.Timedelta(days=1)).date()),
        "week": week, "candidates": candidates,
        "reference": {r["category"]: r["expected_demand"] for r in first_day.values()},
        "initial": dict.fromkeys(candidates, 0),
    }
    draft = new_draft(plan)
    result = solve_order(plan, draft)
    if result["status"] in ("optimal", "feasible"):
        plan["initial"] = result["quantities"]
    plan["initial_status"] = result["status"]
    plan["initial_messages"] = result["messages"]
    return plan


def main():
    import argparse
    from inputs import check_inputs, read_demo

    parser = argparse.ArgumentParser(description="从四张 Excel 复跑七天品类计划和首日订单")
    parser.add_argument("--input-dir", default="data/demo")
    parser.add_argument("--as-of", default="2023-06-30")
    args = parser.parse_args()
    batch = check_inputs(read_demo(args.input_dir))
    plan = generate_plan(batch, args.as_of)
    audit = validate_draft(plan, new_draft(plan))
    print(json.dumps({
        "plan_id": plan["id"], "cutoff": plan["cutoff"], "day": plan["day"],
        "week_rows": len(plan["week"]), "candidates": len(plan["candidates"]),
        "solver_status": plan["initial_status"], "solver_messages": plan["initial_messages"],
        "selected": audit["selected"], "kg": audit["quantity_g"] / SCALE,
        "simulated_contribution": round(audit["contribution"], 2), "categories": audit["categories"],
    }, ensure_ascii=False, indent=2))
    if not audit["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
