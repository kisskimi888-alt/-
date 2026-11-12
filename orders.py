"""第 3 段：在同一套需求、损耗、成本和业务规则下检查草稿与重排。"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import ceil, isfinite
from uuid import uuid4

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

SCALE = 1000
EPS = 1e-7
DEFAULT_RULES = {"min_count": 27, "max_count": 33, "min_g": 2500, "coverage": 0.85}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_grams(value):
    try:
        grams = Decimal(str(value)) * SCALE
        if not grams.is_finite() or grams < 0 or grams != grams.to_integral_value() or grams > 10**9:
            raise ValueError
        return int(grams)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("请填非负公斤数，最多三位小数，单项最多 1,000,000 kg。") from None


def new_draft(plan, source_order=None):
    return {
        "id": uuid4().hex[:12], "plan_id": plan["id"], "revision": 0,
        "items": {sku: {"qty_g": qty, "cap_g": None, "locked": False}
                  for sku, qty in plan["initial"].items()},
        "history": [], "source_order": source_order,
    }


def update_draft(draft, items, source):
    changed = []
    for sku, line in items.items():
        for field in ("qty_g", "cap_g", "locked"):
            before, after = draft["items"][sku][field], line[field]
            if before != after:
                changed.append({"sku": sku, "field": field, "before": before, "after": after})
    if not changed:
        return draft
    result = deepcopy(draft)
    result["revision"] += 1
    result["items"] = deepcopy(items)
    result["history"].extend(
        {**change, "draft_id": result["id"], "revision": result["revision"], "time": now(), "source": source}
        for change in changed
    )
    return result


def sales_by_scenario(available_g, item):
    sellable = available_g / SCALE * (1 - item["loss"])
    return np.minimum(sellable, item["demand"])


def validate_draft(plan, draft):
    rules = plan["rules"]
    errors, rows = [], []
    by_category = {c: 0.0 for c in plan["reference"]}
    selected = 0
    for sku, item in plan["candidates"].items():
        line = draft["items"][sku]
        qty, cap = line["qty_g"], line["cap_g"]
        selected += qty > 0
        line_errors = []
        if 0 < qty < rules["min_g"]:
            line_errors.append(f"陈列起订量 {rules['min_g']/SCALE:g} kg")
        if cap is not None and qty > cap:
            line_errors.append(f"超出供货上限 {(qty-cap)/SCALE:.3f} kg")
        available = qty if cap is None else min(qty, cap)
        sold = float(np.dot(plan["weights"], sales_by_scenario(available, item)))
        spending = qty / SCALE * item["cost"]
        contribution = sold * item["retail"] - available / SCALE * item["cost"]
        by_category[item["category"]] += sold
        rows.append({
            "sku": sku, "name": item["name"], "category": item["category"],
            "qty_g": qty, "cap_g": cap, "locked": line["locked"],
            "cost": item["cost"], "retail": item["retail"], "loss": item["loss"],
            "spending": spending, "expected_sales": sold, "contribution": contribution,
            "remaining": max(0.0, available / SCALE * (1-item["loss"]) - sold),
            "status": "；".join(line_errors) if line_errors else "通过",
        })
        errors.extend(f"{item['name']}（{sku}）{message}" for message in line_errors)
    if not rules["min_count"] <= selected <= rules["max_count"]:
        errors.append(f"当前选了 {selected} 个单品，要求 {rules['min_count']}–{rules['max_count']} 个。")
    categories = []
    for category, demand in plan["reference"].items():
        sold = by_category[category]
        target = rules["coverage"] * demand
        gap = max(0.0, target - sold)
        if gap > EPS:
            errors.append(f"{category}距覆盖门槛还差 {gap:.3f} kg。")
        categories.append({"category": category, "demand": demand, "sold": sold,
                           "target": target, "gap": gap, "coverage": sold / demand if demand else None})
    return {"ok": not errors, "errors": errors, "selected": selected, "rows": rows,
            "categories": categories, "spending": sum(r["spending"] for r in rows),
            "sales": sum(r["expected_sales"] for r in rows),
            "contribution": sum(r["contribution"] for r in rows),
            "quantity_g": sum(r["qty_g"] for r in rows)}


def explain_limits(plan, draft):
    """先看单项锁定与供货，再计算忽略跨品类选品总数后的乐观上限。"""
    rules = plan["rules"]
    counts = 0
    reasons = []
    upper_sales = {category: 0.0 for category in plan["reference"]}
    for sku, item in plan["candidates"].items():
        line = draft["items"][sku]
        cap = line["cap_g"]
        if line["locked"]:
            qty = line["qty_g"]
            if cap is not None and qty > cap:
                reasons.append(f"{item['name']}锁定 {qty/SCALE:g} kg，最多可供 {cap/SCALE:g} kg。")
            if 0 < qty < rules["min_g"]:
                reasons.append(f"{item['name']}锁定 {qty/SCALE:g} kg，低于最小陈列量。")
            cap = qty if cap is None else min(cap, qty)
        if cap is None or cap >= rules["min_g"]:
            counts += 1
            maximum = item["demand"] if cap is None else sales_by_scenario(cap, item)
            upper_sales[item["category"]] += float(np.dot(plan["weights"], maximum))
    if counts < rules["min_count"]:
        reasons.append(f"最多能选 {counts} 个单品，至少要求 {rules['min_count']} 个。")
    for category, demand in plan["reference"].items():
        target = rules["coverage"] * demand
        upper = upper_sales[category]
        if upper < target - EPS:
            reasons.append(f"{category}至少要覆盖 {target:.2f} kg，按当前供货和单品需求最多覆盖 "
                           f"{upper:.2f} kg，仍差 {target-upper:.2f} kg。")
    return reasons


def solve_order(plan, draft, time_limit=15):
    """整数克订货量、0/1 选品和每个需求情景的销量一起交给 HiGHS。"""
    reasons = explain_limits(plan, draft)
    if reasons:
        return {"status": "infeasible", "messages": reasons}
    sku_list = list(plan["candidates"])
    if not sku_list:
        return {"status": "infeasible", "messages": ["没有可售候选单品。"]}
    rules, weights = plan["rules"], plan["weights"]
    n, k = len(sku_list), len(weights)
    size = n * (2 + k)
    objective = np.zeros(size)
    lb = np.zeros(size)
    ub = np.ones(size)
    matrix_row, matrix_col, coefficients, lower, upper = [], [], [], [], []

    def constraint(pairs, lo=-np.inf, hi=np.inf):
        for col, coefficient in pairs:
            matrix_row.append(len(lower))
            matrix_col.append(col)
            coefficients.append(coefficient)
        lower.append(lo)
        upper.append(hi)

    category_terms = {c: [] for c in plan["reference"]}
    for i, sku in enumerate(sku_list):
        item, line = plan["candidates"][sku], draft["items"][sku]
        q, z = i, n+i
        retain = 1-item["loss"]
        # 超过需求最高情景后再买只会增加成本。锁定量另行保留。
        natural_max = max(rules["min_g"], ceil(max(item["demand"]) / retain * SCALE))
        cap = line["cap_g"]
        if line["locked"]:
            natural_max = max(natural_max, line["qty_g"])
            lb[q] = line["qty_g"]
            cap = line["qty_g"]
        ub[q] = natural_max if cap is None else min(natural_max, cap)
        objective[q] = item["cost"] / SCALE
        constraint([(q, 1), (z, -ub[q])], hi=0)
        constraint([(q, 1), (z, -rules["min_g"])], lo=0)
        for s, weight in enumerate(weights):
            sold = 2*n + i*k + s
            ub[sold] = item["demand"][s]
            objective[sold] = -item["retail"] * weight
            constraint([(sold, 1), (q, -retain / SCALE)], hi=0)
            category_terms[item["category"]].append((sold, weight))

    constraint([(n+i, 1) for i in range(n)], lo=rules["min_count"], hi=rules["max_count"])
    for category, terms in category_terms.items():
        constraint(terms, lo=plan["reference"][category] * rules["coverage"])
    matrix = coo_matrix((coefficients, (matrix_row, matrix_col)), shape=(len(lower), size)).tocsc()
    result = milp(
        objective, integrality=np.r_[np.ones(2*n), np.zeros(n*k)], bounds=Bounds(lb, ub),
        constraints=LinearConstraint(matrix, lower, upper),
        options={"time_limit": time_limit, "mip_rel_gap": 1e-6},
    )
    if result.status == 2:
        return {"status": "infeasible", "messages": [
            "单项上限检查未发现确定缺口，但供货、锁定、品种数、陈列量和覆盖门槛同时生效时无可行解。",
        ]}
    if result.x is not None and np.isfinite(result.x[:n]).all():
        quantities = {sku: int(round(result.x[i])) for i, sku in enumerate(sku_list)}
        proposed = deepcopy(draft)
        for sku, qty in quantities.items():
            proposed["items"][sku]["qty_g"] = qty
        if (result.status in (0, 1) and validate_draft(plan, proposed)["ok"]
                and all(not line["locked"] or quantities[sku] == line["qty_g"]
                        for sku, line in draft["items"].items())):
            status = "optimal" if result.status == 0 else "feasible"
            return {"status": status, "quantities": quantities,
                    "messages": [] if status == "optimal" else ["求解达到限制。当前方案检查通过，最优性未确认。"]}
    if result.status == 1:
        return {"status": "timeout", "messages": ["求解达到时间或迭代限制，暂未找到通过检查的方案。"]}
    return {"status": "error", "messages": ["求解结果未通过独立审单检查，请保留当前草稿。"]}
