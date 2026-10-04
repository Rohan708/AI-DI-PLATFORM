# Snowflake test environment

> Snowflake is one adapter among several (Stage 3), not the product. jaffle_shop is already loaded (2026-10-04).

Goal: jaffle_shop is loaded, there is a read-only role and a key-pair service user, and `CREATE TABLE` fails for that role.

## 1. Account
- Trial account: AWS, Asia Pacific (Thailand). Write down the **account identifier**: Snowsight → account menu → "Connect a tool" (format `ORGNAME-ACCOUNTNAME`).
- The trial expires after about 30 days. Starting a new trial is fine.

## 2. Load jaffle_shop with dbt
dbt runs in its **own** conda env (`dbt`: Python 3.12, dbt-core 1.12, dbt-snowflake 1.12), so its dependencies never mix with the app's.

**Version pin:** `dbt-labs/jaffle-shop` `main` now requires dbt 2.0 (Fusion), which pip can't install. The clone at `validation/jaffle_shop` is checked out on local branch `aide-dbt1` at commit `7d0d8de`, which requires dbt >= 1.12. That version also produces the standard dbt 1.x `manifest.json` our parser targets.

**Profile:** `~/.dbt/profiles.yml` (profile `default`) contains no secrets.
- It reads `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_USER` from environment variables.
- It authenticates with a key pair (`~/.snowflake/rohan_dbt_key.p8`) registered on the admin user. Browser SSO (`externalbrowser`) does **not** work on a trial without a SAML provider (error 390190), and password login conflicts with mandatory MFA.
- To register the key, run `ALTER USER <login> SET RSA_PUBLIC_KEY='<key body>';` as ACCOUNTADMIN.
- Seeds go to `JAFFLE_DB.RAW`; models go to `JAFFLE_DB.ANALYTICS`.

**Already done:** creating the env, installing dbt, cloning and pinning the project, writing the profile, and `dbt deps`.

**Steps:**
1. In Snowsight (as ACCOUNTADMIN), run:
   ```sql
   CREATE DATABASE IF NOT EXISTS JAFFLE_DB;
   ```
2. In PowerShell:
   ```powershell
   conda activate dbt
   cd D:\Projects\AI-DE\validation\jaffle_shop
   $env:SNOWFLAKE_ACCOUNT = "ORGNAME-ACCOUNTNAME"   # your account identifier
   $env:SNOWFLAKE_USER    = "YOUR_LOGIN_NAME"
   dbt debug                                          # expect "All checks passed!"
   dbt seed --vars '{"load_source_data": true}'       # ~16 MB of CSVs; several minutes
   dbt run                                            # builds staging views + mart tables
   ```
3. Check: `JAFFLE_DB.RAW` has 6 `RAW_*` tables, and `JAFFLE_DB.ANALYTICS` has the `STG_*` views plus mart tables (`CUSTOMERS`, `ORDERS`, …).
4. Keep `target/manifest.json`. Stage 1.3 reads it.

When loading is finished and you don't need dbt for a while, you can remove the admin key with `ALTER USER <login> UNSET RSA_PUBLIC_KEY;` (re-add it later with `SET`).

## 3. Key pair
Generate the key **outside the repo**, for example in `%USERPROFILE%\.snowflake\`:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out aide_rsa_key.p8 -nocrypt
openssl rsa -in aide_rsa_key.p8 -pubout -out aide_rsa_key.pub
```

Git for Windows includes `openssl`, so run these in Git Bash. Use a passphrase (drop `-nocrypt`) for anything beyond a throwaway trial.

## 4. Role, warehouse, and user
Run [`scripts/snowflake/01_create_readonly_role.sql`](../../scripts/snowflake/01_create_readonly_role.sql) in Snowsight. Paste in the public key body.

## 5. Verify
Run [`scripts/snowflake/02_verify_readonly.sql`](../../scripts/snowflake/02_verify_readonly.sql). Statements 1–2 should succeed, and statement 3 **must fail**.

## 6. Connect the platform (Stage 3)
The Snowflake adapter and its smoke test come in Stage 3, when Snowflake is added as a data source. Steps 3–5 can wait until then.
