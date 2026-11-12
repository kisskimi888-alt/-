"""第 6 段：网页只负责接收输入、调用业务函数和显示结果。"""
from copy import deepcopy
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from inputs import FILES, InputError, check_inputs, read_demo
from orders import SCALE, new_draft, solve_order, to_grams, update_draft, validate_draft
from planning import generate_plan
from storage import OrderStore, order_xlsx

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="生鲜采购审单台", page_icon="🥬", layout="wide")
store = OrderStore(os.environ.get("ORDER_DESK_DB", ROOT / "data" / "state" / "orders.sqlite3"))
state = st.session_state

st.title("生鲜采购审单台")
st.caption("历史数据模拟 · 六类蔬菜的七天采购参考与首日订货")
tabs = st.tabs(["① 数据与生成", "② 七天品类计划", "③ 首日审单", "④ 已确认订单"])


def activate(draft):
    state.active_draft = draft["id"]
    state.pop("preview", None)
    state.pop("editor_base", None)


def cutoff_date(value):
    return pd.Timestamp(value).date()


def show_metrics(audit):
    cols = st.columns(4)
    cols[0].metric("订货品种", f"{audit['selected']} 个")
    cols[1].metric("填写量", f"{audit['quantity_g']/SCALE:,.3f} kg")
    cols[2].metric("按填写量预计采购金额", f"¥{audit['spending']:,.2f}")
    cols[3].metric("按供货试算的模拟贡献", f"¥{audit['contribution']:,.2f}")


def category_frame(audit, plan):
    week = {r["category"]: r["qty_g"]/SCALE for r in plan["week"] if r["horizon"] == 1}
    quantities = {}
    for row in audit["rows"]:
        category = row["category"]
        quantities[category] = quantities.get(category, 0) + row["qty_g"]/SCALE
    return pd.DataFrame([{
        "品类": r["category"], "参考采购(kg)": week.get(r["category"]),
        "草稿订货(kg)": quantities.get(r["category"], 0),
        "预计销量(kg)": r["sold"], "参考需求(kg)": r["demand"],
        "覆盖率": "—" if r["coverage"] is None else f"{r['coverage']:.2%}",
        "门槛缺口(kg)": r["gap"],
    } for r in audit["categories"]]).round(3)


with tabs[0]:
    st.header("导入并核查四张表")
    source = st.radio("数据来源", ["历史案例", "上传 Excel"], horizontal=True)
    files = []
    if source == "上传 Excel":
        columns = st.columns(2)
        for i, title in enumerate(("商品信息", "销售流水", "批发价格", "损耗率")):
            with columns[i % 2]:
                files.append(st.file_uploader(f"{FILES[i]} · {title}", type=["xlsx"], key=f"upload_{i}"))
        signature = tuple(f.file_id if f else None for f in files)
    else:
        st.write("历史案例使用原始四张附件。核查后默认以 2023-06-30 为数据截止日。")
        signature = ("demo",)
    if st.button("核查数据", disabled=source == "上传 Excel" and not all(files), type="primary"):
        state.pop("checked", None)
        try:
            with st.spinner("正在读取工作簿并核对跨表关联…"):
                content = read_demo(ROOT / "data/demo") if source == "历史案例" else tuple(f.getvalue() for f in files)
                state.checked = (signature, check_inputs(content))
        except (InputError, OSError) as exc:
            st.error(str(exc))
    checked = state.get("checked")
    if checked and checked[0] == signature:
        batch = checked[1]
        st.dataframe(batch.rows, hide_index=True)
        st.caption(f"数据批次 {batch.batch_id}")
        selected_date = st.date_input("最后一个完整营业日", cutoff_date(batch.last_day),
                                      min_value=cutoff_date(batch.first_day),
                                      max_value=cutoff_date(batch.last_day),
                                      key=f"cutoff_{batch.batch_id}")
        if st.button("生成计划", type="primary"):
            try:
                with st.spinner("正在训练七天预测并求解首日订单…"):
                    plan = store.save_plan(generate_plan(batch, selected_date))
                    draft = new_draft(plan)
                    store.save_draft(draft)
                    activate(draft)
                st.success(f"计划 {plan['id']} 已生成。打开「七天品类计划」或「首日审单」。")
            except (InputError, ValueError) as exc:
                st.error(str(exc))

draft = store.load_draft(state.active_draft) if state.get("active_draft") else None
plan = store.load_plan(draft["plan_id"]) if draft else None

with tabs[1]:
    st.header("七天品类参考")
    if plan:
        st.caption(f"计划 {plan['id']} · 截止日 {plan['cutoff']} · 误差校准月 {plan['calibration']} · {plan['price_mode']}")
        weekly = pd.DataFrame([{
            "日期": r["day"], "品类": r["category"], "点预测销量(kg)": r["forecast"],
            "加权情景需求(kg)": r["expected_demand"], "建议采购(kg)": r["qty_g"]/SCALE,
            "参考零售价(元/kg)": r["retail"], "预计进货价(元/kg)": r["cost"],
        } for r in plan["week"]]).sort_values(["日期", "品类"])
        st.dataframe(weekly.round(3), hide_index=True)
        st.caption("首日实际草稿及其与本计划的差额可在审单页查看。")
    else:
        st.info("生成计划后，这里会出现每天每类蔬菜的预测和采购参考。")

with tabs[2]:
    st.header("首日采购审单")
    if not draft:
        st.info("先生成计划，再检查首日订单。")
    else:
        rules = plan["rules"]
        st.caption(f"订货日 {plan['day']} · 数据截止 {plan['cutoff']} · "
                   f"要求 {rules['min_count']}–{rules['max_count']} 个单品、每个至少 {rules['min_g']/SCALE:g} kg、"
                   f"每类覆盖至少 {rules['coverage']:.0%}")
        if plan["initial_messages"]:
            st.info("初始求解：" + "；".join(plan["initial_messages"]))
        confirmed_id = store.order_id_for(draft)
        if confirmed_id:
            st.success(f"草稿 {draft['id']}-v{draft['revision']} 已确认为 {confirmed_id}。")
            if st.button("复制为新草稿"):
                activate(store.clone_order(confirmed_id))
                st.rerun()
        else:
            st.write("提交一个单元格后即时检查。填写量为 0 表示移除，上限留空表示供货未核实，填 0 表示缺货。")
            # 编辑器的基础数据在一次编辑周期内固定，避免 rerun 时将旧的编辑量错用到新行。
            if state.get("editor_base") is None:
                ordered = sorted(plan["candidates"], key=lambda sku: (
                    not bool(plan["initial"][sku]), plan["candidates"][sku]["category"], sku,
                ))
                state.editor_base = pd.DataFrame([{
                    "单品编码": sku, "单品": plan["candidates"][sku]["name"],
                    "品类": plan["candidates"][sku]["category"],
                    "初始量(kg)": plan["initial"][sku]/SCALE,
                    "订货量(kg)": draft["items"][sku]["qty_g"]/SCALE,
                    "供货上限(kg)": None if draft["items"][sku]["cap_g"] is None else draft["items"][sku]["cap_g"]/SCALE,
                    "锁定": draft["items"][sku]["locked"],
                } for sku in ordered]).astype({"供货上限(kg)": "Float64"})
                state.editor_epoch = state.get("editor_epoch", 0) + 1
            edited = st.data_editor(
                state.editor_base, key=f"editor_{state.editor_epoch}", hide_index=True,
                num_rows="fixed", height=435,
                disabled=["单品编码", "单品", "品类", "初始量(kg)"],
                column_config={
                    "订货量(kg)": st.column_config.NumberColumn(min_value=0, format="%.3f", step=0.001),
                    "供货上限(kg)": st.column_config.NumberColumn(min_value=0, format="%.3f", step=0.001),
                    "锁定": st.column_config.CheckboxColumn(),
                },
            )
            edit_error = None
            items = {}
            for row in edited.to_dict("records"):
                try:
                    cap = row["供货上限(kg)"]
                    items[row["单品编码"]] = {
                        "qty_g": to_grams(row["订货量(kg)"]),
                        "cap_g": None if pd.isna(cap) else to_grams(cap), "locked": bool(row["锁定"]),
                    }
                except ValueError as exc:
                    edit_error = f"{row['单品']}：{exc}"
                    break
            if edit_error:
                st.error(edit_error)
            else:
                changed = update_draft(draft, items, "人工审单")
                if changed is not draft:
                    store.save_draft(changed)
                    draft = changed
                    if state.pop("preview", None) is not None:
                        st.info("草稿已更新，原重排预览已失效。")

        audit = validate_draft(plan, draft)
        show_metrics(audit)
        st.caption(f"草稿 {draft['id']}-v{draft['revision']} · "
                   f"{sum(r['qty_g'] > 0 and r['cap_g'] is None for r in audit['rows'])} 个已订单品供货未核实")
        st.dataframe(category_frame(audit, plan), hide_index=True)
        with st.expander("查看每个单品的即时计算结果"):
            st.dataframe(pd.DataFrame([{
                "单品": r["name"], "订货量(kg)": r["qty_g"]/SCALE,
                "预计进货价(元/kg)": r["cost"], "参考零售价(元/kg)": r["retail"],
                "预计销量(kg)": r["expected_sales"], "预计余量(kg)": r["remaining"],
                "按填写量预计采购金额(元)": r["spending"], "模拟贡献(元)": r["contribution"],
                "检查": r["status"],
            } for r in audit["rows"]]).round(3), hide_index=True)
        if audit["ok"]:
            st.success("当前草稿通过订单规则检查。")
        else:
            st.error("\n\n".join(audit["errors"]))

        if not confirmed_id:
            actions = st.columns(2)
            if actions[0].button("按新条件重排", disabled=bool(edit_error)):
                with st.spinner("正在按已录入条件求解…"):
                    state.preview = (draft["id"], draft["revision"], solve_order(plan, draft))
            preview = state.get("preview")
            active_preview = preview and preview[:2] == (draft["id"], draft["revision"])
            has_proposal = active_preview and "quantities" in preview[2]
            if actions[1].button("确认订单", disabled=bool(edit_error or has_proposal or not audit["ok"]), type="primary"):
                try:
                    snapshot = store.confirm(draft["id"], draft["revision"])
                    state.just_confirmed = snapshot["id"]
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            if active_preview:
                result = preview[2]
                for message in result["messages"]:
                    st.info(message) if has_proposal else st.warning(message)
                if has_proposal:
                    items = deepcopy(draft["items"])
                    differences = []
                    for sku, qty in result["quantities"].items():
                        previous = items[sku]["qty_g"]
                        items[sku]["qty_g"] = qty
                        if previous != qty:
                            differences.append({"单品": plan["candidates"][sku]["name"],
                                                "草稿(kg)": previous/SCALE, "重排(kg)": qty/SCALE})
                    st.subheader("重排预览")
                    if differences:
                        st.dataframe(differences, hide_index=True)
                    else:
                        st.caption("重排建议与当前订货量一致。")
                    proposed = validate_draft(plan, {**draft, "items": items})
                    show_metrics(proposed)
                    st.dataframe(category_frame(proposed, plan), hide_index=True)
                    choices = st.columns(2)
                    if choices[0].button("应用这份重排", disabled=bool(edit_error)):
                        updated = update_draft(draft, items, "应用重排")
                        if updated is not draft:
                            store.save_draft(updated)
                        activate(updated)
                        st.rerun()
                    if choices[1].button("舍弃重排预览"):
                        state.pop("preview", None)
                        st.rerun()

with tabs[3]:
    st.header("已确认订单")
    ids = store.orders()
    if ids:
        if state.get("just_confirmed") in ids:
            state.confirmed_selection = state.pop("just_confirmed")
        order_id = st.selectbox("确认单号", ids, key="confirmed_selection")
        snapshot = store.load_order(order_id)
        meta = snapshot["metadata"]
        st.caption(f"{order_id} · 订货日 {meta['day']} · 数据截止 {meta['cutoff']} · 计划 {meta['id']}")
        show_metrics(snapshot["audit"])
        st.dataframe([{"单品": r["name"], "品类": r["category"], "最终订货量(kg)": r["qty_g"]/SCALE}
                      for r in snapshot["audit"]["rows"]], hide_index=True)
        st.download_button("下载这版确认订单", data=order_xlsx(snapshot), file_name=f"{order_id}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           on_click="ignore", type="primary")
        if st.button("从此订单创建新草稿"):
            activate(store.clone_order(order_id))
            st.rerun()
    else:
        st.info("检查并确认首日订单后，这里会列出可下载的版本。")
