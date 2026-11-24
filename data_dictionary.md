# Data Dictionary

This file explains every table and column in the Smarter Stock warehouse (`sql/create_tables.sql`).
---

## Dimension tables

### dim_product — one row per product

| Column | Type | Meaning |
|---|---|---|
| stock_code (PK) | VARCHAR(20) | Product ID |
| description | TEXT | Product name |
| in_forecast_scope | BOOLEAN | TRUE if the product is forecast |

### dim_customer — one row per customer

| Column | Type | Meaning |
|---|---|---|
| customer_id (PK) | VARCHAR(20) | Customer ID (`UNKNOWN` if missing) |
| home_country | TEXT | Country the customer orders from most |
| first_purchase | TIMESTAMP | Date of first order |
| last_purchase | TIMESTAMP | Date of last order |

### dim_country — one row per country

| Column | Type | Meaning |
|---|---|---|
| country (PK) | TEXT | Country name |
| is_uk | BOOLEAN | TRUE for United Kingdom |

### dim_week — one row per week

| Column | Type | Meaning |
|---|---|---|
| week_start (PK) | DATE | Monday the week starts |
| year | INT | Year |
| week_of_year | INT | Week number (1–53) |
| month_start | DATE | Month the week is in |
| quarter | INT | Quarter (1–4) |
| is_holiday_season | BOOLEAN | TRUE for October–December |

### dim_month — one row per month

| Column | Type | Meaning |
|---|---|---|
| month_start (PK) | DATE | First day of the month |
| year | INT | Year |
| month_number | INT | Month (1–12) |
| month_name | TEXT | Month name |
| quarter | INT | Quarter (1–4) |

---

## Fact tables

### fact_sales_line — one row per sale (invoice line)

| Column | Type | Meaning |
|---|---|---|
| source_row_id (PK) | VARCHAR(40) | Sheet + row number in the original Excel file |
| source_sheet | TEXT | Excel sheet the row came from |
| invoice | VARCHAR(20) | Order number |
| stock_code (FK) | VARCHAR(20) | Product sold |
| customer_id (FK) | VARCHAR(20) | Customer |
| country (FK) | TEXT | Customer's country |
| invoice_date | TIMESTAMP | Date and time of the order |
| week_start (FK) | DATE | Week of the order |
| month_start (FK) | DATE | Month of the order |
| quantity | INT | Units sold |
| price | NUMERIC | Price per unit (£) |
| line_value | NUMERIC | quantity × price (£) |

### fact_cancellation — one row per cancelled line

Same columns as `fact_sales_line`. Quantity and value are negative. Kept separate and never subtracted from sales.

### fact_weekly_sales — one row per product per week

| Column | Type | Meaning |
|---|---|---|
| stock_code (PK, FK) | VARCHAR(20) | Product |
| week_start (PK, FK) | DATE | Week |
| units | INT | Units sold that week (0 if none) |
| sales_value | NUMERIC | Sales that week (£) |

### fact_monthly_retailer_sales — one row per month

| Column | Type | Meaning |
|---|---|---|
| month_start (PK, FK) | DATE | Month |
| units | INT | Units sold that month |
| sales_value | NUMERIC | Sales that month (£) |

### fact_market_benchmark — ONS J5BI, one row per month

| Column | Type | Meaning |
|---|---|---|
| month_start (PK, FK) | DATE | Month |
| series_id (PK) | VARCHAR(10) | Always `J5BI` |
| value | NUMERIC | Index value |
| unit | TEXT | Always `index` |
| geography | TEXT | Always `Great Britain` |
| adjustment | TEXT | Always `seasonally adjusted` |

### fact_industry_inventory — US Census, one row per month per category

| Column | Type | Meaning |
|---|---|---|
| month_start (PK, FK) | DATE | Month |
| naics_code (PK) | VARCHAR(20) | `452` (General merchandise) or `44,45 excl. 441` (Retail excl. cars) |
| kind_of_business | TEXT | Category name |
| inventories_usd_m | NUMERIC | Inventory held, millions of USD |
| inventory_sales_ratio | NUMERIC | Months of sales held in stock |
| period_type | VARCHAR(10) | `project` or `current` |
| geography | TEXT | Always `United States` |
| currency | TEXT | Always `USD` |

### fact_forecast — one row per forecast run, product, week ahead and model

| Column | Type | Meaning |
|---|---|---|
| run_id (PK) | VARCHAR(30) | Which forecast run |
| stock_code (PK, FK) | VARCHAR(20) | Product |
| forecast_origin_week | DATE | Last week of data used |
| target_week | DATE | Week being predicted |
| horizon (PK) | INT | Weeks ahead (1–4) |
| model_name (PK) | VARCHAR(30) | `random_forest` or `baseline_4wk_avg` |
| predicted_units | NUMERIC | Predicted units sold |

### fact_backtest — forecast test results

| Column | Type | Meaning |
|---|---|---|
| stock_code (PK, FK) | VARCHAR(20) | Product |
| target_week (PK, FK) | DATE | Test week |
| horizon (PK) | INT | Weeks ahead (1–4) |
| model_name (PK) | VARCHAR(30) | `random_forest` or `baseline_4wk_avg` |
| actual_units | NUMERIC | Units actually sold |
| predicted_units | NUMERIC | Units predicted |
| abs_error | NUMERIC | Difference between actual and predicted |

---

## Audit table

### etl_run_log — one row per dataset each time the pipeline runs

| Column | Type | Meaning |
|---|---|---|
| run_id (PK) | VARCHAR(30) | Which pipeline run |
| dataset (PK) | VARCHAR(20) | `UCI`, `ONS` or `Census` |
| run_timestamp | TIMESTAMP | When it ran |
| rows_in | INT | Rows read |
| rows_loaded | INT | Rows loaded |
| total_units | BIGINT | Total units loaded |
| total_value | NUMERIC | Total £ loaded |
