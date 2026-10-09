# Lab benchmark

| Metric | Value |
|---|---|
| Anomalies caught (recall) | 12 / 14 (86%) |
| Findings that were real (precision) | 100% |
| False alarms | 0 |
| Related knock-on findings (not penalised) | 7 |
| Relationships discovered (recall) | 15 / 15 (100%) |
| Wrong relationships proposed | 0 |
| Known baseline issues found (real, not planted) | 13 / 13 |

## By category

| Category | Planted | Caught | False alarms |
|---|---|---|---|
| structural | 3 | 3 | 0 |
| relational | 2 | 2 | 0 |
| column_value | 3 | 3 | 0 |
| business_rule | 2 | 0 | 0 |
| time_series | 3 | 3 | 0 |
| row_outlier | 1 | 1 | 0 |

## Missed

| Scenario | Where | Expected check | Stage |
|---|---|---|---|
| ship_before_order | `shop.shipments.shipped_at` | rule:shipped_at>=order_date | 2 |
| invoice_total_mismatch | `legacy.INV_HDR.TOTAL_AMT` | rule:TOTAL_AMT=sum(INV_LINE.LINE_AMT) | 2 |
