# 0005 — Scanning safety and credentials

**Status:** Accepted (implemented in Stage 1.3)

## Context
We connect to customers' **production** databases. One runaway query, a lock that blocks their application, or an accidental write would end the relationship and the product's reputation. We also must never hold their credentials in our own database.

## Decision
1. **Read-only at three layers:**
   - the customer's role has only `SELECT` (setup script provided)
   - the role defaults sessions to read-only
   - every transaction we open starts with `SET TRANSACTION READ ONLY`, so even a bug in our code can't write
2. **Time limits on every transaction:** `statement_timeout` (30 s) and `lock_timeout` (2 s) by default, configurable per source.
3. **Isolation per table:** each table is profiled in its own transaction. A failure marks the run `partial`; it doesn't abort the scan.
4. **Cheap by construction:**
   - structure comes from the system catalog
   - measurements are one aggregate query per table
   - large tables are sampled with `TABLESAMPLE SYSTEM`
5. **Identifiable:** `application_name = 'aide'`.
6. **Credentials by reference:** `data_source.connection_ref` names an environment variable / `.env` entry; the URL is resolved only at connect time and never stored or logged.
7. **Values are opt-out:** top values and text min/max are stored by default (self-hosted: the data stays in the customer's network) and can be disabled per source.

## Consequences
- Safety holds even if our code has a bug (database-enforced read-only).
- Sampled measurements are flagged, and detection must treat them as estimates.
- A secrets manager (Vault / AWS Secrets Manager) can later replace `resolve_connection_url` without changing callers.
