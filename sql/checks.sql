-- Smarter Stock — reconciliation checks (Guide Step 5.5).
-- Run in pgAdmin's Query Tool after `python src/load.py`.
-- Compare each result with data/clean/etl_summary.csv.

-- 1. UCI sales lines: rows, units, value  (= "positive sales lines" in etl_summary)
SELECT COUNT(*) AS sales_rows, SUM(quantity) AS units, ROUND(SUM(line_value), 2) AS value
FROM fact_sales_line;

-- 2. UCI cancellation lines  (= "cancellation lines")
SELECT COUNT(*) AS cancel_rows, SUM(quantity) AS units FROM fact_cancellation;

-- 3. Weekly totals (= "weekly product rows"; lower than check 1 because the
--    partial first and last weeks are dropped)
SELECT COUNT(*) AS rows, SUM(units) AS units, ROUND(SUM(sales_value), 2) AS value,
       COUNT(DISTINCT week_start) AS weeks
FROM fact_weekly_sales;

-- 4. Every product has the same number of weeks (expect one row)
SELECT weeks, COUNT(*) AS products
FROM (SELECT stock_code, COUNT(*) AS weeks FROM fact_weekly_sales GROUP BY stock_code) t
GROUP BY weeks;

-- 5. Benchmarks: 36 months each (Dec 2008 – Nov 2011)
SELECT 'ONS J5BI' AS source, MIN(month_start), MAX(month_start), COUNT(*) FROM fact_market_benchmark
UNION ALL
SELECT 'Census ' || naics_code, MIN(month_start), MAX(month_start), COUNT(*)
FROM fact_industry_inventory WHERE period_type = 'project' GROUP BY naics_code;

-- 6. Spot-check values against the raw files (expect 21.0, 28.8 and 1.39)
SELECT month_start, value FROM fact_market_benchmark
WHERE month_start IN ('2008-12-01', '2011-11-01') ORDER BY month_start;
SELECT inventory_sales_ratio FROM fact_industry_inventory
WHERE naics_code = '452' AND month_start = '2010-01-01';

-- 7. Latest load log
SELECT * FROM etl_run_log ORDER BY run_timestamp DESC, dataset LIMIT 3;

-- 8. Trace three random rows back to the workbook (open that sheet, go to that row)
SELECT source_row_id, invoice, stock_code, quantity, price
FROM fact_sales_line ORDER BY random() LIMIT 3;
