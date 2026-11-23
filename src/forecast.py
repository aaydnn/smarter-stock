"""
Smarter Stock — AI forecasting (Guide Steps 6 and 7).

Run from the repo root, after etl.py and load.py:
    python src/forecast.py

What it does:
  1. Reads weekly units for in-scope products from fact_weekly_sales.
  2. Builds features that only use information available at the origin week.
  3. Backtest: trains four random forests (1–4 weeks ahead) on weeks before
     CUTOFF, tests them on later weeks, and compares them with the baseline
     (the average of the last four weeks). Results replace fact_backtest.
  4. Live forecast: retrains on all history and predicts the four weeks after
     the last full week. Results are APPENDED to fact_forecast under a new run_id.
  5. Writes docs/model_results.md with error by horizon and win rate.

Needs DB_URL in .env (same as load.py).
"""

import io
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.ensemble import RandomForestRegressor
from sqlalchemy import create_engine, text

CUTOFF = pd.Timestamp("2011-06-27")   # first test origin week; same for every product
HORIZONS = [1, 2, 3, 4]
SEED = 42                             # makes results repeatable
BLEND_WEIGHT = 0.5                    # final forecast = this share forest + the rest baseline
RF_PARAMS = dict(n_estimators=200, min_samples_leaf=5, max_features=0.5,
                 max_samples=0.5, n_jobs=-1, random_state=SEED)

FEATURES = (
    [f"lag_{k}" for k in range(1, 9)]
    + ["roll_mean_4", "roll_mean_8", "roll_mean_12", "roll_std_4",
       "weeks_with_sales_12", "product_code", "product_avg_train",
       "target_week_of_year", "target_month", "target_is_holiday"]
)


# ---------------------------------------------------------------------------
# 6.2  Read history
# ---------------------------------------------------------------------------

def read_history(engine):
    sql = """
        SELECT w.stock_code, w.week_start, w.units
        FROM fact_weekly_sales w
        JOIN dim_product p USING (stock_code)
        WHERE p.in_forecast_scope
        ORDER BY w.stock_code, w.week_start
    """
    df = pd.read_sql(sql, engine, parse_dates=["week_start"])
    df["units"] = df["units"].astype(float)
    return df


# ---------------------------------------------------------------------------
# 6.3 + 6.4  Features and targets
# ---------------------------------------------------------------------------

def add_history_features(df):
    """Features computed at origin week t, using weeks t and earlier only."""
    df = df.sort_values(["stock_code", "week_start"]).copy()
    g = df.groupby("stock_code")["units"]
    for k in range(1, 9):
        df[f"lag_{k}"] = g.shift(k - 1)            # lag_1 = units in week t itself
    for w in (4, 8, 12):
        df[f"roll_mean_{w}"] = g.transform(lambda s, w=w: s.rolling(w).mean())
    df["roll_std_4"] = g.transform(lambda s: s.rolling(4).std())
    df["weeks_with_sales_12"] = g.transform(lambda s: (s > 0).rolling(12).sum())
    df["product_code"] = df["stock_code"].astype("category").cat.codes
    return df


def add_product_avg(df, train_end):
    """Each product's average weekly units, using weeks before train_end only."""
    avg = df[df["week_start"] < train_end].groupby("stock_code")["units"].mean()
    df["product_avg_train"] = df["stock_code"].map(avg).fillna(0.0)
    return df


def horizon_frame(df, h):
    """Rows for horizon h: target = units in week t+h, plus that week's calendar."""
    out = df.copy()
    out["target_week"] = out["week_start"] + pd.Timedelta(weeks=h)
    out["target"] = out.groupby("stock_code")["units"].shift(-h)
    out["target_week_of_year"] = out["target_week"].dt.isocalendar().week.astype(int)
    out["target_month"] = out["target_week"].dt.month
    out["target_is_holiday"] = out["target_month"].isin([10, 11, 12]).astype(int)
    return out.dropna(subset=["roll_mean_12"])     # need 12 weeks of history


def fit(train, h):
    """Train on log(1 + units). A few huge wholesale orders dominate raw units and
    made an untransformed forest lose to the baseline; the log scale fixes that.

    The log scale predicts a "typical" week, which is lower than the average week
    for lumpy demand, so raw predictions run about 35-40% low in total. The
    correction factor rescales them, estimated from the forest's out-of-bag
    predictions on the training rows only, so no test data is used."""
    model = RandomForestRegressor(**RF_PARAMS, oob_score=True)
    model.fit(train[FEATURES], np.log1p(train["target"]))
    oob = np.clip(np.expm1(model.oob_prediction_), 0, None)
    model.correction_ = train["target"].sum() / oob.sum()
    return model


def predict_raw(model, X):
    """Forest alone: back-transformed from the log scale, units can't be negative."""
    return np.clip(np.expm1(model.predict(X)), 0, None)


def predict(model, X, baseline):
    """Final AI forecast: bias-corrected forest blended with the 4-week baseline.
    In the backtest this beat the baseline at every horizon with ~0% total bias;
    the raw forest was more accurate per week but badly under-forecast totals."""
    corrected = predict_raw(model, X) * model.correction_
    return BLEND_WEIGHT * corrected + (1 - BLEND_WEIGHT) * baseline


# ---------------------------------------------------------------------------
# 6.5–6.10  Backtest
# ---------------------------------------------------------------------------

def backtest(history):
    df = add_product_avg(add_history_features(history), CUTOFF)
    results, raw_rows = [], []
    for h in HORIZONS:
        frame = horizon_frame(df, h).dropna(subset=["target"])
        # 6.5 Train: origin weeks whose target is still before the cutoff (no leakage).
        train = frame[frame["target_week"] < CUTOFF]
        test = frame[frame["week_start"] >= CUTOFF]
        t0 = time.time()
        model = fit(train, h)
        base_pred = test["roll_mean_4"].to_numpy()          # 6.7 baseline
        rf_pred = predict(model, test[FEATURES], base_pred)
        raw = predict_raw(model, test[FEATURES])
        act = test["target"].to_numpy()
        raw_rows.append({"horizon": h, "wape": np.abs(act - raw).sum() / act.sum(),
                         "bias": raw.sum() / act.sum() - 1, "correction": model.correction_})
        print(f"  h={h}: trained on {len(train):,} rows, tested on {len(test):,} "
              f"({time.time() - t0:.0f}s)")
        for name, pred in (("random_forest", rf_pred), ("baseline_4wk_avg", base_pred)):
            results.append(pd.DataFrame({
                "stock_code": test["stock_code"].to_numpy(),
                "target_week": test["target_week"].to_numpy(),
                "horizon": h,
                "model_name": name,
                "actual_units": test["target"].to_numpy(),
                "predicted_units": np.round(pred, 2),
            }))
    bt = pd.concat(results, ignore_index=True)
    bt["abs_error"] = (bt["actual_units"] - bt["predicted_units"]).abs().round(2)
    return bt, pd.DataFrame(raw_rows).set_index("horizon")


def summarize(bt):
    """MAE and WAPE by horizon and model, plus the share of products where the forest wins."""
    by_h = (bt.groupby(["horizon", "model_name"])
            .agg(mae=("abs_error", "mean"), err=("abs_error", "sum"),
                 act=("actual_units", "sum"), pred=("predicted_units", "sum"))
            .reset_index())
    by_h["wape"] = by_h["err"] / by_h["act"]
    by_h["bias"] = by_h["pred"] / by_h["act"] - 1      # negative = under-forecasts in total
    table = by_h.pivot(index="horizon", columns="model_name", values=["mae", "wape", "bias"])

    per_prod = (bt.groupby(["stock_code", "horizon", "model_name"])["abs_error"].mean()
                .unstack("model_name"))
    win = ((per_prod["random_forest"] < per_prod["baseline_4wk_avg"])
           .groupby("horizon").mean())
    return table, win


def write_results_doc(table, win, n_products, raw):
    lines = [
        "# Model results (Step 6 backtest)",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `src/forecast.py`.",
        "",
        f"- Products in scope: {n_products:,}",
        f"- Train: origin weeks whose target week is before {CUTOFF:%Y-%m-%d}",
        f"- Test: origin weeks from {CUTOFF:%Y-%m-%d} onward (same cutoff for every product)",
        "- Baseline: average of the last four weeks, repeated for each future week",
        f"- Random forest: {RF_PARAMS['n_estimators']} trees, min_samples_leaf="
        f"{RF_PARAMS['min_samples_leaf']}, trained on log(1 + units), random_state={SEED}",
        f"- AI forecast (\"random_forest\" in the warehouse) = {BLEND_WEIGHT:.0%} bias-corrected forest "
        f"+ {1 - BLEND_WEIGHT:.0%} baseline",
        "",
        "| Weeks ahead | Forest MAE | Baseline MAE | Forest WAPE | Baseline WAPE | Forest bias | Baseline bias | Forest wins (share of products) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for h in HORIZONS:
        lines.append(
            f"| {h} | {table.loc[h, ('mae', 'random_forest')]:.2f} "
            f"| {table.loc[h, ('mae', 'baseline_4wk_avg')]:.2f} "
            f"| {table.loc[h, ('wape', 'random_forest')]:.1%} "
            f"| {table.loc[h, ('wape', 'baseline_4wk_avg')]:.1%} "
            f"| {table.loc[h, ('bias', 'random_forest')]:+.0%} "
            f"| {table.loc[h, ('bias', 'baseline_4wk_avg')]:+.0%} "
            f"| {win.loc[h]:.0%} |")
    lines += [
        "",
        "MAE = average absolute error in units per product-week. "
        "WAPE = total absolute error ÷ total actual units (lower is better). "
        "Bias = total predicted ÷ total actual − 1 (negative means the model under-forecasts "
        "overall, which matters for stock-outs).",
        "Where the baseline wins, the report shows it: the pitch promises visible errors, "
        "not that AI always wins.",
        "",
        "## Why the forecast is a blend",
        "",
        "The forest alone (trained on log units) was the most accurate week to week, but it "
        "under-forecast total demand badly. Because review flags compare the forecast with "
        "recent sales, that bias would flag almost every product as \"demand falling\". "
        "Rescaling the forest fixes the bias but loses its accuracy edge; blending the "
        "rescaled forest with the baseline keeps both an accuracy gain and near-zero bias.",
        "",
        "| Weeks ahead | Forest alone WAPE | Forest alone bias | Correction factor (from out-of-bag) |",
        "| --- | --- | --- | --- |",
    ]
    for h in HORIZONS:
        lines.append(f"| {h} | {raw.loc[h, 'wape']:.1%} | {raw.loc[h, 'bias']:+.0%} "
                     f"| x{raw.loc[h, 'correction']:.2f} |")
    lines.append("")
    Path("docs").mkdir(exist_ok=True)
    Path("docs/model_results.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 6.11 + Step 7  Live forecast
# ---------------------------------------------------------------------------

def live_forecast(history, run_id):
    last_week = history["week_start"].max()
    df = add_product_avg(add_history_features(history), last_week + pd.Timedelta(days=1))
    origin = df[df["week_start"] == last_week]
    rows = []
    for h in HORIZONS:
        frame = horizon_frame(df, h)
        model = fit(frame.dropna(subset=["target"]), h)     # retrain on all history
        live = horizon_frame(origin, h)
        base_pred = live["roll_mean_4"].to_numpy()
        rf_pred = predict(model, live[FEATURES], base_pred)
        for name, pred in (("random_forest", rf_pred), ("baseline_4wk_avg", base_pred)):
            rows.append(pd.DataFrame({
                "run_id": run_id,
                "stock_code": live["stock_code"].to_numpy(),
                "forecast_origin_week": last_week,
                "target_week": live["target_week"].to_numpy(),
                "horizon": h,
                "model_name": name,
                "predicted_units": np.round(pred, 2),
            }))
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# Write to the warehouse
# ---------------------------------------------------------------------------

def copy_frame(conn, table, df):
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, date_format="%Y-%m-%d")
    buf.seek(0)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {table} ({', '.join(df.columns)}) FROM STDIN WITH (FORMAT csv)", buf)


def main():
    load_dotenv()
    engine = create_engine(os.environ["DB_URL"])
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    print("Reading weekly history...")
    history = read_history(engine)
    n_products = history["stock_code"].nunique()
    print(f"  {n_products:,} products x {history['week_start'].nunique()} weeks")

    print("Backtest (train before cutoff, test after):")
    bt, raw = backtest(history)
    table, win = summarize(bt)
    write_results_doc(table, win, n_products, raw)

    print("Live forecast (retrained on all history):")
    fc = live_forecast(history, run_id)

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE fact_backtest"))      # backtest is rebuilt each run
        copy_frame(conn, "fact_backtest", bt)
        copy_frame(conn, "fact_forecast", fc)              # forecasts are appended

    print(f"\nWrote {len(bt):,} backtest rows and {len(fc):,} forecast rows (run {run_id}).")
    print("\nWAPE by weeks ahead (lower is better):")
    for h in HORIZONS:
        print(f"  {h} wk: forest {table.loc[h, ('wape', 'random_forest')]:.1%}  "
              f"baseline {table.loc[h, ('wape', 'baseline_4wk_avg')]:.1%}  "
              f"forest bias {table.loc[h, ('bias', 'random_forest')]:+.0%}  "
              f"forest wins on {win.loc[h]:.0%} of products")
    print("\nFull table: docs/model_results.md")


if __name__ == "__main__":
    main()
