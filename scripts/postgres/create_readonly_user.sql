-- Creates the read-only login the AI Data Engineer connects as.
-- Run as a superuser (or the database owner) in the database to be monitored.
-- Replace :db, the schema list, and the password before running. Example (psql):
--   psql -v db=shopco -v pw="'a-strong-password'" -f create_readonly_user.sql
--
-- What it grants: CONNECT, USAGE on the listed schemas, SELECT on their tables (now and
-- future), and pg_read_all_stats (query statistics for relationship discovery).
-- What it does NOT grant: any INSERT/UPDATE/DELETE/DDL. On top of that, the tool runs
-- every query inside a READ ONLY transaction with statement and lock timeouts.

CREATE ROLE aide_readonly LOGIN PASSWORD :pw
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- Belt and braces: even if a write slipped through, the session defaults to read-only.
ALTER ROLE aide_readonly SET default_transaction_read_only = on;
ALTER ROLE aide_readonly SET statement_timeout = '30s';
ALTER ROLE aide_readonly SET lock_timeout = '2s';

GRANT CONNECT ON DATABASE :db TO aide_readonly;

-- Repeat for every schema to monitor (the lab uses shop, legacy, reporting).
GRANT USAGE ON SCHEMA shop TO aide_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA shop TO aide_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA shop GRANT SELECT ON TABLES TO aide_readonly;

GRANT USAGE ON SCHEMA legacy TO aide_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA legacy TO aide_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA legacy GRANT SELECT ON TABLES TO aide_readonly;

GRANT USAGE ON SCHEMA reporting TO aide_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO aide_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA reporting GRANT SELECT ON TABLES TO aide_readonly;

-- Query statistics (pg_stat_statements, pg_stat_user_tables) without superuser.
GRANT pg_read_all_stats TO aide_readonly;

-- Verify (as aide_readonly): this must FAIL with "cannot execute ... in a read-only transaction"
--   CREATE TABLE shop.should_fail (id int);
