"""第 2 段：用截止日前的销售预测未来七天，再用历史预测误差构造需求情景。"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

from inputs import InputError

WEIGHTS = (0.25, 0.5, 0.25)
QUANTILES = (0.2, 0.5, 0.8)
MODEL_VERSION = "direct-category-hgb-v1"


def origin_features(series, horizon):
    """每行对应预测起点。horizon=1 是起点当天，所有销量特征来自起点以前。"""
    frame = pd.DataFrame(index=series.index)
    for lag in (1, 7, 14, 28):
        frame[f"lag_{lag}"] = series.shift(lag)
    for days, minimum in ((7, 4), (28, 7)):
        frame[f"mean_{days}"] = series.shift(1).rolling(days, min_periods=minimum).mean()
    target_day = frame.index + pd.Timedelta(days=horizon - 1)
    frame["weekday"] = target_day.dayofweek
    frame["month"] = target_day.month
    return frame, series.shift(1 - horizon), target_day


def predict(frame, y, target_dates, origins, training_cutoff):
    """标签日期也必须早于训练截止日，堵住多步预测里容易漏掉的未来数据。"""
    train = (target_dates < training_cutoff) & y.notna()
    if train.sum() < 100:
        raise InputError("历史不足。每个品类和预测步长至少需要 100 条可用训练标签。")
    model = HistGradientBoostingRegressor(
        max_iter=150, max_leaf_nodes=15, min_samples_leaf=20,
        learning_rate=0.05, early_stopping=False, random_state=42,
    )
    model.fit(frame.loc[train], y.loc[train])
    return model.predict(frame.loc[origins]).clip(min=0)


def forecast_week(history, cutoff):
    first = cutoff + pd.Timedelta(days=1)
    plan_month = first.to_period("M")
    # 月初方案的校准月往前移三个月，七月案例因此使用四月。
    calibration_month = plan_month - 3
    starts = pd.date_range(calibration_month.start_time, calibration_month.end_time.normalize(), freq="W-SAT")
    starts = starts[starts + pd.Timedelta(days=6) <= calibration_month.end_time.normalize()]
    if starts.empty or history.day.min() >= starts.min():
        raise InputError(f"历史数据无法覆盖 {calibration_month} 的预测误差校准窗口。")
    end = first + pd.Timedelta(days=6)
    calendar = pd.date_range(history.day.min(), end)
    categories = history.groupby(["category_id", "category"], sort=True)
    result = []
    with threadpool_limits(limits=1):
        for (category_id, name), rows in categories:
            series = rows.set_index("day").gross.reindex(calendar).where(lambda x: x > 0)
            for h in range(1, 8):
                frame, y, target_day = origin_features(series, h)
                estimated = predict(frame, y, target_day, [first], plan_month.start_time)[0]
                calibration = predict(frame, y, target_day, starts, calibration_month.start_time)
                residual = (y.loc[starts].to_numpy() - calibration)
                residual = residual[np.isfinite(residual)]
                if len(residual) < 3:
                    raise InputError(f"{name} 第 {h} 天的校准实际销量不足三条。")
                demand = np.maximum(0, estimated + np.quantile(residual, QUANTILES))
                result.append({
                    "day": str(target_day[frame.index.get_loc(first)].date()), "horizon": h,
                    "category_id": str(category_id), "category": str(name),
                    "forecast": float(estimated), "demand": demand.tolist(),
                    "expected_demand": float(np.dot(WEIGHTS, demand)),
                })
    return result, str(calibration_month)
