-- =====================================================================
-- Smarter Stock — PostgreSQL warehouse schema (Implementation Guide Step 4)
-- Owner: Mariah Jenkins (BI Data Architect)
-- =====================================================================
DROP TABLE IF EXISTS
    etl_run_log,
    fact_backtest,
    fact_forecast,
    fact_industry_inventory,
    fact_market_benchmark,
    fact_monthly_retailer_sales,
    fact_weekly_sales,
    fact_cancellation,
    fact_sales_line,
    dim_month,
    dim_week,
    dim_country,
    dim_customer,
    dim_product
CASCADE;

-- =====================================================================
-- 1. DIMENSIONS (describe things)
-- =====================================================================

-- One row per product (UCI StockCode). Source: data/clean/products.csv
CREATE TABLE dim_product (
    stock_code          VARCHAR(20) PRIMARY KEY,
    description         TEXT,
    in_forecast_scope   BOOLEAN NOT NULL DEFAULT FALSE  -- TRUE if in forecast_products.csv
);

-- One row per customer, plus one 'UNKNOWN' row for missing Customer IDs.
-- Source: data/clean/customers.csv
CREATE TABLE dim_customer (
    customer_id         VARCHAR(20) PRIMARY KEY,
    home_country        TEXT,        -- the customer's most frequent country
    first_purchase      TIMESTAMP,
    last_purchase       TIMESTAMP
);

-- One row per country name. Source: data/clean/countries.csv
CREATE TABLE dim_country (
    country             TEXT PRIMARY KEY,
    is_uk               BOOLEAN NOT NULL
);

-- One row per Monday. Built in Python (Step 5.1). Source: weeks.csv
-- Note for load.py: extend this 4 weeks past the last data week so
-- forecast target weeks also have a matching row for Power BI.
CREATE TABLE dim_week (
    week_start          DATE PRIMARY KEY,   -- always a Monday
    year                INT,
    week_of_year        INT,
    month_start         DATE,               -- the month this week starts in
    quarter             INT,
    is_holiday_season   BOOLEAN             -- e.g. Nov–Dec
);

-- One row per month, Dec 2008 → last month in any dataset.
-- Built in Python (Step 5.1). Source: months.csv
-- Shared link between retailer monthly sales and both benchmarks.
CREATE TABLE dim_month (
    month_start         DATE PRIMARY KEY,   -- first day of the month
    year                INT,
    month_number        INT,
    month_name          TEXT,
    quarter             INT
);

-- =====================================================================
-- 2. FACTS (measurable events)
-- =====================================================================

-- One row per positive UCI invoice line. Source: sales_lines.csv
-- source_row_id (e.g. 'Year 2010-2011-1532') traces back to the workbook.
CREATE TABLE fact_sales_line (
    source_row_id       VARCHAR(40) PRIMARY KEY,
    source_sheet        TEXT NOT NULL,
    invoice             VARCHAR(20),
    stock_code          VARCHAR(20) REFERENCES dim_product(stock_code),
    customer_id         VARCHAR(20) REFERENCES dim_customer(customer_id),
    country             TEXT        REFERENCES dim_country(country),
    invoice_date        TIMESTAMP,
    week_start          DATE        REFERENCES dim_week(week_start),
    month_start         DATE        REFERENCES dim_month(month_start),
    quantity            INT,
    price               NUMERIC(10,3),
    line_value          NUMERIC(14,3)   -- quantity * price
);

-- One row per cancelled UCI line (invoice starts with 'C').
-- Kept separate; never subtracted from sales. Source: cancellations.csv
CREATE TABLE fact_cancellation (
    source_row_id       VARCHAR(40) PRIMARY KEY,
    source_sheet        TEXT NOT NULL,
    invoice             VARCHAR(20),
    stock_code          VARCHAR(20) REFERENCES dim_product(stock_code),
    customer_id         VARCHAR(20) REFERENCES dim_customer(customer_id),
    country             TEXT        REFERENCES dim_country(country),
    invoice_date        TIMESTAMP,
    week_start          DATE        REFERENCES dim_week(week_start),
    month_start         DATE        REFERENCES dim_month(month_start),
    quantity            INT,            -- negative in the source data
    price               NUMERIC(10,3),
    line_value          NUMERIC(14,3)
);

-- One row per product × week, with zero-sales weeks filled in.
-- Source: weekly_product_sales.csv
CREATE TABLE fact_weekly_sales (
    stock_code          VARCHAR(20) REFERENCES dim_product(stock_code),
    week_start          DATE        REFERENCES dim_week(week_start),
    units               INT,
    sales_value         NUMERIC(14,3),
    PRIMARY KEY (stock_code, week_start)
);

-- One row per month of retailer totals. Source: monthly_retailer_sales.csv
CREATE TABLE fact_monthly_retailer_sales (
    month_start         DATE PRIMARY KEY REFERENCES dim_month(month_start),
    units               INT,
    sales_value         NUMERIC(14,3)
);

-- ONS J5BI: UK non-store retail index, one row per month (Dec 2008–Nov 2011).
-- BENCHMARK ONLY — never add to retailer sales. Source: ons_benchmark.csv
CREATE TABLE fact_market_benchmark (
    month_start         DATE REFERENCES dim_month(month_start),
    series_id           VARCHAR(10),    -- 'J5BI'
    value               NUMERIC(10,1),
    unit                TEXT,           -- 'index'
    geography           TEXT,           -- 'Great Britain'
    adjustment          TEXT,           -- 'seasonally adjusted'
    PRIMARY KEY (month_start, series_id)
);

-- US Census inventories and inventory/sales ratios, one row per month × category.
-- BENCHMARK ONLY — USD, never converted or combined. Source: census_inventory_benchmark.csv
CREATE TABLE fact_industry_inventory (
    month_start             DATE REFERENCES dim_month(month_start),
    naics_code              VARCHAR(20),     -- '452' or '44,45 excl. 441'
    kind_of_business        TEXT,
    inventories_usd_m       NUMERIC(14,1),   -- millions of US dollars
    inventory_sales_ratio   NUMERIC(6,2),    -- months of sales held in stock
    period_type             VARCHAR(10) DEFAULT 'project',  -- 'project' or 'current'
    geography               TEXT DEFAULT 'United States',
    currency                TEXT DEFAULT 'USD',
    PRIMARY KEY (month_start, naics_code)
);

-- Forecasts written by src/forecast.py (Step 7). Appended, never overwritten.
-- One row per run × product × horizon × model.
-- target_week has no foreign key: future weeks may fall past the data.
CREATE TABLE fact_forecast (
    run_id                  VARCHAR(30),     -- e.g. '2026-11-21_01'
    stock_code              VARCHAR(20) REFERENCES dim_product(stock_code),
    forecast_origin_week    DATE,            -- last full week of data used
    target_week             DATE,            -- origin + horizon weeks
    horizon                 INT CHECK (horizon BETWEEN 1 AND 4),
    model_name              VARCHAR(30) CHECK (model_name IN ('random_forest', 'baseline_4wk_avg')),
    predicted_units         NUMERIC(12,2),
    PRIMARY KEY (run_id, stock_code, horizon, model_name)
);

-- Backtest results from Step 6: predicted vs. actual on test weeks.
-- One row per product × test week × horizon × model.
CREATE TABLE fact_backtest (
    stock_code              VARCHAR(20) REFERENCES dim_product(stock_code),
    target_week             DATE        REFERENCES dim_week(week_start),
    horizon                 INT CHECK (horizon BETWEEN 1 AND 4),
    model_name              VARCHAR(30) CHECK (model_name IN ('random_forest', 'baseline_4wk_avg')),
    actual_units            NUMERIC(12,2),
    predicted_units         NUMERIC(12,2),
    abs_error               NUMERIC(12,2),   -- |actual − predicted|
    PRIMARY KEY (stock_code, target_week, horizon, model_name)
);

-- =====================================================================
-- 3. AUDIT
-- =====================================================================

-- One row per pipeline run × dataset (UCI, ONS, Census). Written by load.py.
CREATE TABLE etl_run_log (
    run_id              VARCHAR(30),
    dataset             VARCHAR(20),     -- 'UCI', 'ONS' or 'Census'
    run_timestamp       TIMESTAMP DEFAULT NOW(),
    rows_in             INT,
    rows_loaded         INT,
    total_units         BIGINT,
    total_value         NUMERIC(16,2),
    PRIMARY KEY (run_id, dataset)
);

-- =====================================================================
-- 4. INDEXES on the columns Power BI filters by (Step 4.6)
-- (Primary keys are indexed automatically.)
-- =====================================================================
CREATE INDEX idx_sales_week      ON fact_sales_line (week_start);
CREATE INDEX idx_sales_month     ON fact_sales_line (month_start);
CREATE INDEX idx_sales_product   ON fact_sales_line (stock_code);
CREATE INDEX idx_sales_customer  ON fact_sales_line (customer_id);
CREATE INDEX idx_sales_country   ON fact_sales_line (country);

CREATE INDEX idx_cancel_week     ON fact_cancellation (week_start);
CREATE INDEX idx_cancel_month    ON fact_cancellation (month_start);
CREATE INDEX idx_cancel_product  ON fact_cancellation (stock_code);
CREATE INDEX idx_cancel_customer ON fact_cancellation (customer_id);
CREATE INDEX idx_cancel_country  ON fact_cancellation (country);

CREATE INDEX idx_weekly_week     ON fact_weekly_sales (week_start);

CREATE INDEX idx_forecast_product ON fact_forecast (stock_code);
CREATE INDEX idx_forecast_target  ON fact_forecast (target_week);
CREATE INDEX idx_backtest_target  ON fact_backtest (target_week);

