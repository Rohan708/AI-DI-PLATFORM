# Lab answer key — seed 42, size `small`

Simulated through **2026-03-16**. Written by the lab *before* detection runs; the scorer compares findings with it.

## Planted anomalies (13)

| # | Scenario | Category | Where | Expected check | Stage | Effective | What |
|---|---|---|---|---|---|---|---|
| 1 | drop_column | structural | `shop.products.weight_grams` | schema_drift_column_removed | 1.5 | 2026-03-16 | Column weight_grams was dropped from shop.products |
| 2 | widen_column_type | structural | `shop.orders.channel` | type_drift | 1.5 | 2026-03-16 | shop.orders.channel changed from varchar(20) to varchar(50) |
| 3 | drop_primary_key | structural | `legacy.INV_LINE` | primary_key_removed | 1.5 | 2026-03-16 | legacy.INV_LINE lost its primary key ("INV_NO", "LINE_NO") |
| 4 | orphan_orders | relational | `shop.orders.cust_no` | orphan_rows | 1.4 | 2026-03-16 | 56 orders reference customer ids that don't exist in shop.customers |
| 5 | duplicate_customers | relational | `shop.customers.email` | duplicate_entities | 1.5 | 2026-03-16 | 32 customers were duplicated (same name, email differs only by case) |
| 6 | null_spike | column_value | `shop.addresses.postal_code` | null_rate_spike | 1.5 | 2026-03-16 | postal_code set to NULL on 102 addresses (normally 0% NULL) |
| 7 | country_variants | column_value | `shop.addresses.country` | inconsistent_categories | 1.5 | 2026-03-16 | 154 'US' values rewritten as 'USA' / 'United States' |
| 8 | negative_quantity | column_value | `shop.order_items.quantity` | out_of_range | 1.5 | 2026-03-16 | 35 order lines got a negative quantity (minimum was 1) |
| 9 | skipped_load | time_series | `reporting.daily_sales` | freshness | 1.5 | 2026-03-16 | The nightly ETL skipped its run; the table was not refreshed |
| 10 | skipped_load | time_series | `reporting.customer_summary` | freshness | 1.5 | 2026-03-16 | The nightly ETL skipped its run; the table was not refreshed |
| 11 | half_load | time_series | `shop.orders` | volume_drop | 1.5 | 2026-03-16 | Only 40% of the usual daily orders arrived |
| 12 | ship_before_order | business_rule | `shop.shipments.shipped_at` | rule:shipped_at>=order_date | 2 | 2026-03-16 | 27 shipments have shipped_at two days before their order date |
| 13 | invoice_total_mismatch | business_rule | `legacy.INV_HDR.TOTAL_AMT` | rule:TOTAL_AMT=sum(INV_LINE.LINE_AMT) | 2 | 2026-03-16 | 24 invoice totals inflated by 10% versus the sum of their lines |

## True relationships (15; 11 hidden)

| From | To | Declared FK? | Note |
|---|---|---|---|
| `shop.categories(parent_id)` | `shop.categories(id)` | yes |  |
| `shop.products(category_id)` | `shop.categories(id)` | yes |  |
| `shop.addresses(customer_id)` | `shop.customers(id)` | yes |  |
| `shop.order_items(order_id)` | `shop.orders(order_id)` | yes |  |
| `shop.orders(cust_no)` | `shop.customers(id)` | **no** | misnamed |
| `shop.orders(ship_addr)` | `shop.addresses(id)` | **no** | misnamed |
| `shop.order_items(product_id)` | `shop.products(id)` | **no** |  |
| `shop.payments(order_ref)` | `shop.orders(order_id)` | **no** |  |
| `shop.shipments(ord_id)` | `shop.orders(order_id)` | **no** |  |
| `legacy.CUST_MASTER(EXT_REF)` | `shop.customers(customer_code)` | **no** | cross-schema, links legacy to the app by business code; NULL for legacy-only |
| `legacy.INV_HDR(CUSTID)` | `legacy.CUST_MASTER(CUSTID)` | **no** |  |
| `legacy.INV_LINE(INV_NO)` | `legacy.INV_HDR(INV_NO)` | **no** |  |
| `legacy.INV_LINE(ITEM_CD)` | `shop.products(sku)` | **no** | cross-schema, by business code |
| `legacy.INV_LINE_TAX(INV_NO, LINE_NO)` | `legacy.INV_LINE(INV_NO, LINE_NO)` | **no** | composite key |
| `reporting.customer_summary(customer_id)` | `shop.customers(id)` | **no** |  |

## Normal patterns (must NOT be flagged)

- shop.customers.middle_name is ~60% NULL (most people have none)
- shop.customers.phone is ~40% NULL (optional field)
- shop.products.weight_grams is NULL for every Digital product
- shop.shipments.delivered_at is NULL while a parcel is in transit
- shop.orders.ship_addr is NULL for customers without an address
- legacy.CUST_MASTER.EXT_REF is NULL for legacy-only customers (~5%)
- legacy CHAR columns are space-padded (CUST_NM, CUSTID, STAT_CD)
- order volume is ~40% lower on weekends (seasonality)
- shop.orders.status mix shifts daily as orders move placed -> shipped -> delivered
