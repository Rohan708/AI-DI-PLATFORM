# Lab benchmark

| Metric | Value |
|---|---|
| Anomalies caught (recall) | 1 / 13 (8%) |
| Findings that were real (precision) | 100% |
| False alarms | 0 |
| Related knock-on findings (not penalised) | 0 |
| Relationships discovered (recall) | 15 / 15 (100%) |
| Wrong relationships proposed | 0 |

## By category

| Category | Planted | Caught | False alarms |
|---|---|---|---|
| structural | 3 | 0 | 0 |
| relational | 2 | 1 | 0 |
| column_value | 3 | 0 | 0 |
| business_rule | 2 | 0 | 0 |
| time_series | 3 | 0 | 0 |

## Missed

| Scenario | Where | Expected check | Stage |
|---|---|---|---|
| drop_column | `shop.products.weight_grams` | schema_drift_column_removed | 1.5 |
| widen_column_type | `shop.orders.channel` | type_drift | 1.5 |
| drop_primary_key | `legacy.INV_LINE` | primary_key_removed | 1.5 |
| duplicate_customers | `shop.customers.email` | duplicate_entities | 1.5 |
| null_spike | `shop.addresses.postal_code` | null_rate_spike | 1.5 |
| country_variants | `shop.addresses.country` | inconsistent_categories | 1.5 |
| negative_quantity | `shop.order_items.quantity` | out_of_range | 1.5 |
| skipped_load | `reporting.daily_sales` | freshness | 1.5 |
| skipped_load | `reporting.customer_summary` | freshness | 1.5 |
| half_load | `shop.orders` | volume_drop | 1.5 |
| ship_before_order | `shop.shipments.shipped_at` | rule:shipped_at>=order_date | 2 |
| invoice_total_mismatch | `legacy.INV_HDR.TOTAL_AMT` | rule:TOTAL_AMT=sum(INV_LINE.LINE_AMT) | 2 |
