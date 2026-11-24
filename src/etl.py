"""
Smarter Stock — ETL pipeline (Implementation Guide Step 3).

Reads the three raw files in data/raw/ and writes clean CSVs to data/clean/
that match sql/create_tables.sql (Mariah's warehouse schema).

Run from the repo root:
    python src/etl.py

Raw files expected in data/raw/:
    online_retail_II.xlsx          Dataset 1 — UCI Online Retail II
    drsi.csv                       Dataset 2 — ONS Retail Sales Index (series J5BI)
    census_mrts_inventories.xlsx   Dataset 3 — US Census inventories & I/S ratios
"""

from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path("data/raw")
CLEAN = Path("data/clean")

# Benchmark window: project period (Dec 2009 – Nov 2011) plus 12 earlier months for YoY.
BENCH_START, BENCH_END = "2008-12-01", "2011-11-01"

# Stock codes that are fees, postage or adjustments — not products (rule 3).
NON_PRODUCT_CODES = {
    "POST", "DOT", "M", "D", "C2", "C3", "S", "B", "CRUK",
    "BANK CHARGES", "AMAZONFEE", "ADJUST", "ADJUST2", "TEST001", "TEST002",
}

MIN_WEEKS_SOLD = 40          # products sold in at least this many weeks get forecast (Step 3.9)
FORECAST_WEEKS_AHEAD = 4     # dim_week is extended this far past the data for forecasts

CENSUS_KEEP = {
    "44,45 excl. 441": "Total excl. motor vehicles",
    "452": "General merchandise stores",
}

summary = []   # rows for etl_summary.csv


def log(dataset, step, df=None, rows=None, units=None, value=None):
    """Record a row count (and totals, when the frame has them) for etl_summary.csv."""
    if df is not None:
        rows = len(df)
        if "quantity" in df:
            units = int(df["quantity"].sum())
        if "line_value" in df:
            value = round(float(df["line_value"].sum()), 2)
    summary.append({"dataset": dataset, "step": step, "rows": rows,
                    "units": units, "value": value})
    print(f"  {dataset:<7} {step:<34} {rows:>10,}")


# ---------------------------------------------------------------------------
# Dataset 1 — UCI Online Retail II
# ---------------------------------------------------------------------------

def extract_uci():
    """3.2  Read both year sheets, keep a source row ID, rename to snake_case.

    The first run reads the Excel file (1–3 minutes) and caches it as
    data/raw/retail_with_ids.parquet; later runs read the cache.
    """
    cache = RAW / "retail_with_ids.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
    else:
        print("  Reading online_retail_II.xlsx (first run only, 1-3 min)...")
        sheets = pd.read_excel(RAW / "online_retail_II.xlsx", sheet_name=None,
                               dtype={"Invoice": str, "StockCode": str})
        frames = []
        for name, s in sheets.items():
            s = s.copy()
            s["source_sheet"] = name
            s["source_row_id"] = name + "-" + (s.index + 2).astype(str)  # Excel row number
            frames.append(s)
        df = pd.concat(frames, ignore_index=True)
        # A few descriptions are stored as numbers in Excel; make the column all text.
        df["Description"] = df["Description"].map(lambda v: v if pd.isna(v) else str(v))
        df.to_parquet(cache, index=False)

    df = df.rename(columns={
        "Invoice": "invoice", "StockCode": "stock_code", "Description": "description",
        "Quantity": "quantity", "InvoiceDate": "invoice_date", "Price": "price",
        "Customer ID": "customer_id", "Country": "country",
    })
    df["invoice"] = df["invoice"].astype(str).str.strip()
    # Uppercase: the same product sometimes appears as e.g. 15056BL and 15056bl.
    # PostgreSQL treats those as different codes but Power BI does not, which
    # breaks the dim_product relationships, so merge them here.
    df["stock_code"] = df["stock_code"].astype(str).str.strip().str.upper()
    df["customer_id"] = (df["customer_id"].astype("Float64").astype("Int64")
                         .astype(str).replace("<NA>", "UNKNOWN"))       # rule 7
    df["country"] = df["country"].astype(str).str.strip()
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    assert df["source_row_id"].is_unique, "source_row_id must be unique"
    return df


def split_uci(df):
    """3.3  Apply the cleaning rules: every source row ends up in exactly one of
    sales, cancellations or rejected."""
    df = df.copy()
    biz_cols = ["invoice", "stock_code", "description", "quantity",
                "invoice_date", "price", "customer_id", "country"]
    df["reason"] = pd.Series(pd.NA, index=df.index, dtype="object")

    df.loc[df.duplicated(subset=biz_cols), "reason"] = "duplicate"           # rule 2
    code = df["stock_code"].str.upper()
    is_fee = code.isin(NON_PRODUCT_CODES) | code.str.startswith("GIFT")
    df.loc[df["reason"].isna() & is_fee, "reason"] = "fee_code"              # rule 3

    ok = df["reason"].isna()
    is_cancel = df["invoice"].str.upper().str.startswith("C")                # rule 4
    sales = df[ok & ~is_cancel & (df["quantity"] > 0) & (df["price"] > 0)].copy()  # rule 5
    cancels = df[ok & is_cancel].copy()

    rejected = df[~df.index.isin(sales.index) & ~df.index.isin(cancels.index)].copy()
    rest = rejected["reason"].isna()                                          # rule 6
    rejected.loc[rest & (rejected["price"] <= 0), "reason"] = "zero_or_negative_price"
    rejected.loc[rejected["reason"].isna(), "reason"] = "negative_qty_not_cancellation"

    for frame in (sales, cancels):
        frame["line_value"] = (frame["quantity"] * frame["price"]).round(3)   # 3.4
        frame["week_start"] = frame["invoice_date"].dt.to_period("W-SUN").dt.start_time
        frame["month_start"] = frame["invoice_date"].dt.to_period("M").dt.start_time
    return sales, cancels, rejected


def build_dims(df_all, sales, cancels):
    """3.5  Dimensions built from sales AND cancellations together, so every
    fact row has a matching dimension row (Mariah's foreign keys)."""
    both = pd.concat([sales, cancels], ignore_index=True)

    # dim_product: one description per stock code (the most frequent, rule 7)
    desc = (df_all.dropna(subset=["description"])
            .groupby("stock_code")["description"]
            .agg(lambda s: s.value_counts().index[0]))
    products = pd.DataFrame({"stock_code": sorted(both["stock_code"].unique())})
    products["description"] = products["stock_code"].map(desc)

    # dim_customer: home country = most frequent country; plus UNKNOWN
    cust = both.groupby("customer_id").agg(
        first_purchase=("invoice_date", "min"), last_purchase=("invoice_date", "max"))
    cust["home_country"] = (both.groupby("customer_id")["country"]
                            .agg(lambda s: s.value_counts().index[0]))
    customers = cust.reset_index()[["customer_id", "home_country",
                                    "first_purchase", "last_purchase"]]
    if "UNKNOWN" not in set(customers["customer_id"]):
        customers.loc[len(customers)] = ["UNKNOWN", None, pd.NaT, pd.NaT]
    customers.loc[customers["customer_id"] == "UNKNOWN", "home_country"] = None

    # dim_country
    countries = pd.DataFrame({"country": sorted(both["country"].unique())})
    countries["is_uk"] = countries["country"].eq("United Kingdom")
    return products, customers, countries


def full_period_bounds(sales):
    """First and last dates with sales. Partial weeks/months at either end are dropped."""
    return sales["invoice_date"].min().normalize(), sales["invoice_date"].max().normalize()


def weekly_totals(sales):
    """3.6 + 3.8  Product × week grid with zero-sales weeks filled in; partial
    first and last weeks dropped (a partial week looks like a sales crash)."""
    first_day, last_day = full_period_bounds(sales)
    weekly = (sales.groupby(["stock_code", "week_start"])
              .agg(units=("quantity", "sum"), sales_value=("line_value", "sum"))
              .reset_index())
    weeks = pd.date_range(weekly["week_start"].min(), weekly["week_start"].max(), freq="W-MON")
    weeks = weeks[(weeks >= first_day) & (weeks + pd.Timedelta(days=6) <= last_day)]
    idx = pd.MultiIndex.from_product([sorted(sales["stock_code"].unique()), weeks],
                                     names=["stock_code", "week_start"])
    weekly = (weekly.set_index(["stock_code", "week_start"])
              .reindex(idx, fill_value=0).reset_index())
    weekly["units"] = weekly["units"].astype(int)
    weekly["sales_value"] = weekly["sales_value"].round(3)
    return weekly


def monthly_totals(sales):
    """3.7 + 3.8  Retailer totals per month, partial months dropped."""
    first_day, last_day = full_period_bounds(sales)
    m = (sales.groupby("month_start")
         .agg(units=("quantity", "sum"), sales_value=("line_value", "sum"))
         .reset_index())
    month_end = m["month_start"] + pd.offsets.MonthEnd(0)
    m = m[(m["month_start"] >= first_day) & (month_end <= last_day)].copy()
    m["sales_value"] = m["sales_value"].round(3)
    return m


def forecast_scope(weekly):
    """3.9  Products sold in at least MIN_WEEKS_SOLD weeks."""
    weeks_sold = weekly[weekly["units"] > 0].groupby("stock_code").size()
    scope = weeks_sold[weeks_sold >= MIN_WEEKS_SOLD].index
    return pd.DataFrame({"stock_code": sorted(scope)})


def clean_uci():
    print("Dataset 1: UCI Online Retail II")
    df = extract_uci()
    log("UCI", "rows in (both sheets)", rows=len(df))
    sales, cancels, rejected = split_uci(df)
    log("UCI", "positive sales lines", sales)
    log("UCI", "cancellation lines", cancels)
    for reason, n in rejected["reason"].value_counts().items():
        log("UCI", f"rejected: {reason}", rows=int(n))
    assert len(sales) + len(cancels) + len(rejected) == len(df), "rows do not reconcile"

    products, customers, countries = build_dims(df, sales, cancels)
    weekly = weekly_totals(sales)
    monthly = monthly_totals(sales)
    scope = forecast_scope(weekly)
    products["in_forecast_scope"] = products["stock_code"].isin(scope["stock_code"])
    log("UCI", "products", rows=len(products))
    log("UCI", "products in forecast scope", rows=len(scope))
    log("UCI", "customers (incl. UNKNOWN)", rows=len(customers))
    log("UCI", "countries", rows=len(countries))
    log("UCI", "weekly product rows", rows=len(weekly),
        units=int(weekly["units"].sum()), value=round(float(weekly["sales_value"].sum()), 2))
    log("UCI", "full weeks", rows=weekly["week_start"].nunique())
    log("UCI", "full months", rows=len(monthly))

    fact_cols = ["source_row_id", "source_sheet", "invoice", "stock_code", "customer_id",
                 "country", "invoice_date", "week_start", "month_start",
                 "quantity", "price", "line_value"]
    return {
        "sales_lines": sales[fact_cols],
        "cancellations": cancels[fact_cols],
        "rejected_rows": rejected,
        "products": products,
        "customers": customers,
        "countries": countries,
        "weekly_product_sales": weekly,
        "monthly_retailer_sales": monthly,
        "forecast_products": scope,
    }


# ---------------------------------------------------------------------------
# Dataset 2 — ONS J5BI
# ---------------------------------------------------------------------------

def clean_ons():
    """3.10–3.12  Pull the J5BI column (found by its series ID in row 2), monthly rows only."""
    print("Dataset 2: ONS J5BI")
    raw = pd.read_csv(RAW / "drsi.csv", header=None, dtype=str, low_memory=False)
    col = raw.columns[raw.iloc[1] == "J5BI"][0]
    ons = raw[[0, col]].rename(columns={0: "period", col: "value"})
    ons = ons[ons["period"].str.match(r"^\d{4} [A-Z]{3}$", na=False)].copy()
    ons["month_start"] = pd.to_datetime(ons["period"].str.title(), format="%Y %b")
    ons["value"] = pd.to_numeric(ons["value"])
    ons = ons[ons["month_start"].between(BENCH_START, BENCH_END)]
    ons = ons.assign(series_id="J5BI", unit="index", geography="Great Britain",
                     adjustment="seasonally adjusted")
    ons = ons[["month_start", "series_id", "value", "unit", "geography", "adjustment"]]
    log("ONS", "J5BI months kept", rows=len(ons))
    return {"ons_benchmark": ons}


# ---------------------------------------------------------------------------
# Dataset 3 — Census inventories and inventory/sales ratios
# ---------------------------------------------------------------------------

def clean_census():
    """3.13–3.17  Adjusted blocks only, two chosen categories, one row per month × category."""
    print("Dataset 3: Census inventories")
    from openpyxl import load_workbook
    wb = load_workbook(RAW / "census_mrts_inventories.xlsx", read_only=True, data_only=True)
    first_year, last_year = int(BENCH_START[:4]), int(BENCH_END[:4])
    rows = []
    for year in range(first_year, last_year + 1):
        section = None
        for r in wb[str(year)].iter_rows(values_only=True):
            label = str(r[1] or "").strip()
            if label.isupper():                        # block headers, e.g. "ADJUSTED(1)"
                section = label
                continue
            code = str(r[0]).strip() if r[0] is not None else ""
            if section and "NOT" not in section and "ADJUSTED" in section and code in CENSUS_KEEP:
                measure = "inventory_sales_ratio" if "RATIOS" in section else "inventories_usd_m"
                for month, v in enumerate(r[2:14], start=1):   # columns C–N = Jan–Dec
                    if v not in (None, ""):
                        rows.append({"month_start": pd.Timestamp(year, month, 1),
                                     "naics_code": code, "kind_of_business": CENSUS_KEEP[code],
                                     "measure": measure, "value": float(v)})
    wb.close()
    cen = (pd.DataFrame(rows)
           .pivot_table(index=["month_start", "naics_code", "kind_of_business"],
                        columns="measure", values="value")
           .reset_index())
    cen.columns.name = None
    cen = cen[cen["month_start"].between(BENCH_START, BENCH_END)].copy()
    cen = cen.assign(period_type="project", geography="United States", currency="USD")
    cen = cen[["month_start", "naics_code", "kind_of_business", "inventories_usd_m",
               "inventory_sales_ratio", "period_type", "geography", "currency"]]
    log("Census", "month x category rows", rows=len(cen))
    return {"census_inventory_benchmark": cen}


# ---------------------------------------------------------------------------
# Calendar dimensions (needed by the foreign keys)
# ---------------------------------------------------------------------------

def build_calendar(uci, ons, cen):
    """dim_week: every Monday from the first sale's week to FORECAST_WEEKS_AHEAD weeks
    past the last sale (partial weeks included, because sales lines in them still
    need a week). dim_month: Dec 2008 through the latest month in any dataset."""
    lines = pd.concat([uci["sales_lines"], uci["cancellations"]])
    first_week, last_week = lines["week_start"].min(), lines["week_start"].max()
    weeks = pd.DataFrame({"week_start": pd.date_range(
        first_week, last_week + pd.Timedelta(weeks=FORECAST_WEEKS_AHEAD), freq="W-MON")})
    iso = weeks["week_start"].dt.isocalendar()
    weeks["year"] = iso["year"].astype(int)
    weeks["week_of_year"] = iso["week"].astype(int)
    weeks["month_start"] = weeks["week_start"].dt.to_period("M").dt.start_time
    weeks["quarter"] = weeks["week_start"].dt.quarter
    weeks["is_holiday_season"] = weeks["week_start"].dt.month.isin([10, 11, 12])

    last_month = max(lines["month_start"].max(),
                     weeks["month_start"].max(),
                     ons["ons_benchmark"]["month_start"].max(),
                     cen["census_inventory_benchmark"]["month_start"].max())
    months = pd.DataFrame({"month_start": pd.date_range(BENCH_START, last_month, freq="MS")})
    months["year"] = months["month_start"].dt.year
    months["month_number"] = months["month_start"].dt.month
    months["month_name"] = months["month_start"].dt.strftime("%B")
    months["quarter"] = months["month_start"].dt.quarter
    log("Calendar", "weeks", rows=len(weeks))
    log("Calendar", "months", rows=len(months))
    return {"weeks": weeks, "months": months}


# ---------------------------------------------------------------------------

def main():
    CLEAN.mkdir(parents=True, exist_ok=True)
    summary.clear()
    uci = clean_uci()
    ons = clean_ons()
    cen = clean_census()
    cal = build_calendar(uci, ons, cen)

    outputs = {**uci, **ons, **cen, **cal}
    for name, frame in outputs.items():
        frame.to_csv(CLEAN / f"{name}.csv", index=False, date_format="%Y-%m-%d %H:%M:%S")
    pd.DataFrame(summary).to_csv(CLEAN / "etl_summary.csv", index=False)
    print(f"\nWrote {len(outputs) + 1} files to {CLEAN}/")


if __name__ == "__main__":
    main()
