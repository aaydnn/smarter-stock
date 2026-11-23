# Model results (Step 6 backtest)

Generated 2026-11-22 19:57 by `src/forecast.py`.

- Products in scope: 2,107
- Train: origin weeks whose target week is before 2011-06-27
- Test: origin weeks from 2011-06-27 onward (same cutoff for every product)
- Baseline: average of the last four weeks, repeated for each future week
- Random forest: 200 trees, min_samples_leaf=5, trained on log(1 + units), random_state=42
- AI forecast ("random_forest" in the warehouse) = 50% bias-corrected forest + 50% baseline

| Weeks ahead | Forest MAE | Baseline MAE | Forest WAPE | Baseline WAPE | Forest bias | Baseline bias | Forest wins (share of products) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 29.30 | 30.03 | 68.1% | 69.8% | -0% | -6% | 55% |
| 2 | 30.21 | 30.64 | 69.7% | 70.7% | -1% | -8% | 50% |
| 3 | 31.27 | 31.51 | 71.0% | 71.5% | -2% | -11% | 48% |
| 4 | 32.20 | 32.26 | 72.6% | 72.8% | -2% | -13% | 47% |

MAE = average absolute error in units per product-week. WAPE = total absolute error ÷ total actual units (lower is better). Bias = total predicted ÷ total actual − 1 (negative means the model under-forecasts overall, which matters for stock-outs).
Where the baseline wins, the report shows it: the pitch promises visible errors, not that AI always wins.

## Why the forecast is a blend

The forest alone (trained on log units) was the most accurate week to week, but it under-forecast total demand badly. Because review flags compare the forecast with recent sales, that bias would flag almost every product as "demand falling". Rescaling the forest fixes the bias but loses its accuracy edge; blending the rescaled forest with the baseline keeps both an accuracy gain and near-zero bias.

| Weeks ahead | Forest alone WAPE | Forest alone bias | Correction factor (from out-of-bag) |
| --- | --- | --- | --- |
| 1 | 62.1% | -36% | x1.65 |
| 2 | 64.0% | -38% | x1.70 |
| 3 | 65.7% | -39% | x1.75 |
| 4 | 67.3% | -39% | x1.79 |
