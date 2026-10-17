# Design: accounts, roles, audit log, tenant isolation (Stage 4.3)

**Status:** built 2026-10-18. Lint and types are clean; the test run is pending (unit + integration).
**Code:**
- `accounts/` (`passwords.py`, `store.py`, `cli.py`)
- `api/auth.py` (who's calling, role checks) and `api/account_routes.py`
- `graph/models/accounts.py`, migration `0006`

## 1. Goal
Every person logs in as themselves, may do only what their role allows, and everything they change is recorded under their name. A second company's data in the same metadata store is invisible to them.

## 2. Decisions
| | Decision |
|---|---|
| **Passwords** | at least 12 characters, hashed with **scrypt** from Python's standard library (n=2^15, r=8, p=1, 16-byte salt). There's no extra dependency, and the stored format carries its own cost settings so they can be raised later. |
| **Logging in** | `POST /api/v1/auth/login` with email + password returns a random token (`lmd_…`). Only its SHA-256 hash is stored, so a copy of the database can't be used to log in. Sessions last `LUMIDA_SESSION_HOURS` (12). |
| **API keys** | each person can make personal keys for scripts (`/auth/keys`). A key acts as its owner, with their role, until revoked, and is shown only once. This replaces 4.2's shared `LUMIDA_API_TOKEN`. |
| **Roles** | **viewer** reads everything. **reviewer** can also review findings, rules and relationships, add rules and answer questions. **admin** can also manage people and read the audit log. A role too small gets **403** with the role it needs. |
| **First admin** | `lumida user add you@company.com --role admin` in the container. The password goes in at a hidden prompt (or `--password-stdin`), never as an argument. |
| **Wrong passwords** | the same answer whether the email exists or not, and an unknown email takes as long to check as a wrong password. After `LUMIDA_LOGIN_MAX_FAILURES` (5) in a row, the account is locked for `LUMIDA_LOGIN_LOCKOUT_MINUTES` (15). An admin password reset unlocks it. |
| **Ending access** | disabling someone, or an admin resetting their password, ends all their sessions and API keys at once. Changing your own password ends your sessions but keeps your keys. |
| **Never locked out** | the last admin can't be disabled or demoted, and admins can't disable or demote themselves. |
| **Who did it** | reviews carry the login's email, both in the object's own history (as on the CLI) and in the audit log. Request bodies no longer have a `by` field. |
| **Audit log** | `audit_event`: logins, failed logins (with the reason), logouts, keys made and revoked, people created, role changes, disabling, password changes, reviews, rules added, questions answered. Never passwords, tokens or row values. Admins read it at `GET /audit` (newest first, filter by prefix, e.g. `auth`). |
| **Tenant isolation** | every person belongs to one tenant, and every query is limited to it. A row of another tenant answers **404**, as if it didn't exist. A self-hosted install has one tenant. Postgres row-level security comes with the hosted version. |
| **CLI** | `lumida user add / list / role / disable / enable / password`. The audit log records these as `cli:<os user>`. |

## 3. New endpoints (all under `/api/v1`)
| | |
|---|---|
| `POST /auth/login` | email + password → token, expiry, who you are (the only endpoint without a token) |
| `POST /auth/logout` · `GET /auth/me` · `POST /auth/password` | end this session · who am I · change my password |
| `GET/POST /auth/keys` · `DELETE /auth/keys/{id}` | my API keys |
| `GET/POST /users` · `PATCH /users/{id}` · `POST /users/{id}/password` | admins: people, roles, disabling, password reset |
| `GET /audit?action=&limit=&offset=` | admins: the audit log |

## 4. Tests
- **Unit:**
  - every endpoint except login answers 401 without a valid token
  - the endpoint list is fixed
  - password hashing (salted, verified, rejected when wrong or short)
  - role order and email normalisation
- **Integration:**
  - logging in, `me`, logging out
  - lockout after 5 wrong passwords (with the same answer for unknown emails) and unlocking by reset
  - viewer, reviewer and admin permissions
  - API keys: made, used, listed without the key, revoked
  - changing your own password
  - admins creating, promoting and disabling people; a disabled person is signed out at once; the last admin is protected
  - a second tenant sees nothing of the first
  - the audit log's content (no passwords in it)
  - sessions expiring
  - `lumida user` from the command line

## 5. Not yet
- **Single sign-on** (Google / Microsoft / Okta, SAML/OIDC) and two-factor login: for bigger customers, later.
- **Inviting people by email** (needs an email setup); admins set a first password instead.
- **Per-source permissions** (e.g. "only the billing database"): a role covers the whole tenant.
- **Postgres row-level security** as a second wall between tenants: with the hosted version.
- **Rate limiting** beyond the per-account lockout (e.g. per IP address): behind a reverse proxy for now.
