# Postgres setup for RedDash

Use this checklist to move the dashboard from SQLite to Postgres and support multi-user data isolation.

## 1) Install Postgres locally (or pick a hosted option)
- **Local install:** `sudo apt install postgresql` (Debian/Ubuntu) or `brew install postgresql@16` (macOS). Start the service with `sudo service postgresql start` or `brew services start postgresql@16`.
- **Hosted:** provision a managed instance (e.g., Render, Railway, Supabase, RDS). Ensure you can reach it from both the FastAPI app and the RQ workers.

## 2) Create a database user + database
Run these commands in `psql` (adjust names/passwords to your environment):

```sql
CREATE ROLE reddash WITH LOGIN PASSWORD 'reddash' CREATEDB;
CREATE DATABASE reddash OWNER reddash;
\c reddash;
ALTER SCHEMA public OWNER TO reddash;
```

If your org requires TLS-only connections or stronger passwords, configure them here and mirror the settings in `DATABASE_URL`.

## 3) Set `DATABASE_URL` for all services
Define a single Postgres URL in `.env` so the FastAPI app, background workers, and tests share the same connection string. Example:

```
DATABASE_URL=postgresql+psycopg://reddash:reddash@localhost:5432/reddash
```

Keep the prefix `postgresql+psycopg` so SQLModel uses the binary psycopg driver. Export this in your shell or `.env` before starting `uvicorn` or `rq worker`.

## 4) Install the driver and dependencies
Ensure `psycopg[binary]` is installed (it is listed in `requirements.txt`). If you created a new virtualenv, re-run:

```bash
pip install -r requirements.txt
```

## 5) Create/upgrade tables
The FastAPI app already calls `run_migrations(engine)` on startup, which will:
- Create the `users`, `search_jobs`, and `post_reports` tables if they do not exist.
- Add `user_id` columns and backfill existing rows with the system user when upgrading from older SQLite/JSON deployments.

To run migrations without starting the server, open a Python shell with your `.env` loaded and execute:

```bash
python - <<'PY'
from src.db.migrations import run_migrations
from src.db.session import engine

run_migrations(engine)
print("Migrations applied to", engine.url)
PY
```

## 6) Point RQ workers and the API at the same database
Start Redis and an RQ worker after exporting `DATABASE_URL` and `REDIS_URL` so jobs persist their data in Postgres:

```bash
export DATABASE_URL=postgresql+psycopg://reddash:reddash@localhost:5432/reddash
export REDIS_URL=redis://localhost:6379/0
rq worker reddit-searches
```

Then start the API (`uvicorn src.ui.report_dashboard:app --reload`). Both processes will read/write the same Postgres tables.

## 7) Backups and retention
- Schedule regular `pg_dump` backups for production instances.
- Adjust `SEARCH_JOB_TTL_DAYS` to control how long completed jobs remain in the database; the cleanup routine will purge expired rows on worker startup.

Following these steps ensures Postgres is fully wired for the multi-user model and that all services share the same database state.
