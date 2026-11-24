"""
Smarter Stock — load clean CSVs into the PostgreSQL warehouse (Guide Step 5).

Run from the repo root, after `python src/etl.py`:
    python src/load.py

Needs a .env file in the repo root with one line:
    DB_URL=postgresql+psycopg2://postgres:YOURPASSWORD@localhost:5432/smarter_stock

The tables must already exist (run sql/create_tables.sql once in pgAdmin).
Reruns empty and reload every table. Because fact_forecast and fact_backtest
reference dim_product, they are emptied too: always run `python src/forecast.py`
after this script.
"""

import io
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

CLEAN = Path("data/clean")

# Dimensions first, then facts, so every foreign key already has its parent row.
LOAD_ORDER = [
    ("dim_product", "products.csv"),
    ("dim_customer", "customers.csv"),
    ("dim_country", "countries.csv"),
    ("dim_week", "weeks.csv"),
    ("dim_month", "months.csv"),
    ("fact_sales_line", "sales_lines.csv"),
    ("fact_cancellation", "cancellations.csv"),
    ("fact_weekly_sales", "weekly_product_sales.csv"),
    ("fact_monthly_retailer_sales", "monthly_retailer_sales.csv"),
    ("fact_market_benchmark", "ons_benchmark.csv"),
    ("fact_industry_inventory", "census_inventory_benchmark.csv"),
]


def copy_csv(conn, table, path):
    """Bulk-load one CSV with PostgreSQL COPY (far faster than row-by-row inserts).
    Columns are matched by the CSV header, so column order doesn't matter."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    cols = ", ".join(df.columns)
    with conn.connection.cursor() as cur:
        cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')", buf)
    return len(df)


def main():
    load_dotenv()
    engine = create_engine(os.environ["DB_URL"])
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    summary = pd.read_csv(CLEAN / "etl_summary.csv")

    with engine.begin() as conn:          # one transaction: all tables load, or none do
        conn.execute(text("TRUNCATE " + ", ".join(t for t, _ in LOAD_ORDER) + " CASCADE"))
        loaded = {}
        for table, file in LOAD_ORDER:
            loaded[table] = copy_csv(conn, table, CLEAN / file)
            print(f"  {table:<30} {loaded[table]:>10,} rows")

        uci_in = int(summary.loc[summary["step"] == "rows in (both sheets)", "rows"].iloc[0])
        sales = summary[summary["step"] == "positive sales lines"].iloc[0]
        log_rows = [
            ("UCI", uci_in, loaded["fact_sales_line"] + loaded["fact_cancellation"],
             int(sales["units"]), float(sales["value"])),
            ("ONS", loaded["fact_market_benchmark"], loaded["fact_market_benchmark"], None, None),
            ("Census", loaded["fact_industry_inventory"], loaded["fact_industry_inventory"], None, None),
        ]
        for dataset, rows_in, rows_loaded, units, value in log_rows:
            conn.execute(text(
                "INSERT INTO etl_run_log (run_id, dataset, rows_in, rows_loaded, total_units, total_value) "
                "VALUES (:r, :d, :i, :l, :u, :v)"),
                {"r": run_id, "d": dataset, "i": rows_in, "l": rows_loaded, "u": units, "v": value})

    print(f"\nLoaded run {run_id}. Forecast tables were cleared: run python src/forecast.py next,")
    print("then sql/checks.sql in pgAdmin to reconcile totals.")


if __name__ == "__main__":
    main()
