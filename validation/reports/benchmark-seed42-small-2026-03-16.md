# Lab benchmark

| Metric | Value |
|---|---|
| Anomalies caught (recall) | 0 / 13 (0%) |
| Findings that were real (precision) | n/a |
| False alarms | 0 |
| Related knock-on findings (not penalised) | 0 |
| Relationships discovered (recall) | 0 / 15 (0%) |
| Wrong relationships proposed | 0 |

## By category

| Category | Planted | Caught | False alarms |
|---|---|---|---|
| structural | 3 | 0 | 0 |
| relational | 2 | 0 | 0 |
| column_value | 3 | 0 | 0 |
| business_rule | 2 | 0 | 0 |
| time_series | 3 | 0 | 0 |

## Missed

| Scenario | Where | Expected check | Stage |
|---|---|---|---|
| drop_column | `shop.products.weight_grams` | schema_drift_column_removed | 1.5 |
| widen_column_type | `shop.orders.channel` | type_drift | 1.5 |
| drop_primary_key | `legacy.INV_LINE` | primary_key_removed | 1.5 |
| orphan_orders | `shop.orders.cust_no` | orphan_rows | 1.4 |
| duplicate_customers | `shop.customers.email` | duplicate_entities | 1.5 |
| null_spike | `shop.addresses.postal_code` | null_rate_spike | 1.5 |
| country_variants | `shop.addresses.country` | inconsistent_categories | 1.5 |
| negative_quantity | `shop.order_items.quantity` | out_of_range | 1.5 |
| skipped_load | `reporting.daily_sales` | freshness | 1.5 |
| skipped_load | `reporting.customer_summary` | freshness | 1.5 |
| half_load | `shop.orders` | volume_drop | 1.5 |
| ship_before_order | `shop.shipments.shipped_at` | rule:shipped_at>=order_date | 2 |
| invoice_total_mismatch | `legacy.INV_HDR.TOTAL_AMT` | rule:TOTAL_AMT=sum(INV_LINE.LINE_AMT) | 2 |

## Relationships not discovered

- `shop.categories(parent_id) -> shop.categories(id)`
- `shop.products(category_id) -> shop.categories(id)`
- `shop.addresses(customer_id) -> shop.customers(id)`
- `shop.order_items(order_id) -> shop.orders(order_id)`
- `shop.orders(cust_no) -> shop.customers(id)`
- `shop.orders(ship_addr) -> shop.addresses(id)`
- `shop.order_items(product_id) -> shop.products(id)`
- `shop.payments(order_ref) -> shop.orders(order_id)`
- `shop.shipments(ord_id) -> shop.orders(order_id)`
- `legacy.CUST_MASTER(EXT_REF) -> shop.customers(customer_code)`
- `legacy.INV_HDR(CUSTID) -> legacy.CUST_MASTER(CUSTID)`
- `legacy.INV_LINE(INV_NO) -> legacy.INV_HDR(INV_NO)`
- `legacy.INV_LINE(ITEM_CD) -> shop.products(sku)`
- `legacy.INV_LINE_TAX(INV_NO, LINE_NO) -> legacy.INV_LINE(INV_NO, LINE_NO)`
- `reporting.customer_summary(customer_id) -> shop.customers(id)`
