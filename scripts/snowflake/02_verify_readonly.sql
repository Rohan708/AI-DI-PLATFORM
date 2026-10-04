-- Stage 0 "done when": the read-only role can read but cannot write.
-- Run in Snowsight after dbt has built jaffle_shop (models in JAFFLE_DB.ANALYTICS).

USE ROLE AIDE_READONLY;
USE WAREHOUSE AIDE_WH;

-- 1. Should WORK: read a jaffle_shop table.
SELECT COUNT(*) FROM JAFFLE_DB.ANALYTICS.ORDERS;

-- 2. Should WORK (may be empty: ACCOUNT_USAGE lags up to ~45 min).
SELECT query_id, start_time, warehouse_name
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
ORDER BY start_time DESC
LIMIT 5;

-- 3. Should FAIL with "Insufficient privileges". If it succeeds, the role is NOT read-only.
CREATE TABLE JAFFLE_DB.PUBLIC.should_fail (a INT);
