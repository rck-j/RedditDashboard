# RedDash Dashboard for visualizing search results using PRAW

A web application for reviewing query results from the PRAW. Enter the subreddit and a search term and a dashboard of relevant statistics will be generated to give the user insight into a given subreddit/topic.

## Setup

1. Create a virtual environment and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` into `.env` and fill in the secrets described below. Both the CLI (`red.py`) and the FastAPI server call `_require_env` at startup, so missing values halt the process with a descriptive error.
3. Provision a Postgres database (local or hosted) and set `DATABASE_URL` accordingly before starting the API or RQ workers.
4. Optional: override the default prompts by editing `config/prompts.json`.

### Required environment variables

| Name | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Used by `red.py` and the RQ worker to call the OpenAI Responses API. |
| `PRAW_CLIENT_ID` / `PRAW_CLIENT_SECRET` | OAuth credentials for accessing Reddit's API through PRAW. |
| `PRAW_USER_AGENT` | Custom user-agent string so Reddit can identify your application. |
| `DATABASE_URL` | SQLAlchemy connection string for your Postgres instance (for example, `postgresql+psycopg://reddash:reddash@localhost:5432/reddash`). |

### Optional but recommended variables

| Name | Purpose |
| --- | --- |
| `OPENAI_MODEL` | Defaults to `gpt-4o-mini`, but can be overridden if your account has access to a different model. |
| `PRAW_USERNAME` / `PRAW_PASSWORD` | Only needed for flows that require authenticated Reddit actions. Included for completeness in `.env.example`. |
| `REDIS_URL` | Points the API and workers at your Redis instance (defaults to `redis://localhost:6379/0`). |
| `SEARCH_CACHE_TTL_SECONDS` | How long (in seconds) to reuse cached search jobs. |
| `SEARCH_JOB_TTL_DAYS` | TTL (in days) for persisted search jobs in the relational database. |

If `_require_env` raises `Missing <NAME>; define it in .env or your shell.`, double-check spelling, confirm the variable is exported in your shell, or ensure the `.env` file sits next to the repository root.

## Running the FastAPI dashboard

1. Start the development server:
   ```bash
   uvicorn src.ui.report_dashboard:app --reload
   ```
2. Visit `http://localhost:8000` to load the HTMX-powered dashboard. The legacy table still calls `/api/reports`, which now streams the latest completed SQLModel `SearchJob` back in the same JSON structure that `data/report.json` used to provide.

## Background search jobs with Redis + RQ

1. Launch Redis locally (e.g., `redis-server`).
2. Start an RQ worker that listens for Reddit searches:
   ```bash
   rq worker reddit-searches
   ```
3. Use the FastAPI endpoint to enqueue a search job:
   ```bash
   curl -X POST http://localhost:8000/api/searches \
     -H 'Content-Type: application/json' \
     -d '{
       "subreddits": ["smallbusiness", "Entrepreneur"],
       "query": "(agent OR \"ai agent\")",
       "time_filter": "month",
       "limit": 25,
       "comments_limit": 5
     }'
   ```
4. Each POST creates a `SearchJob` database row, enqueues `src.jobs.search_runner.run` via RQ, and immediately returns the job metadata (including timestamps, counts, and status). RQ workers stream their progress back into Postgres, creating `PersistedPostReport` rows linked to each job.

### Observability and metrics

- Every job lifecycle event emits a structured JSON log (`job_submitted`, `job_enqueued`, `job_started`, `job_progress`, `job_completed`, and `job_failed`). These logs include queue/wall-clock timings plus per-job metadata so you can trace requests end-to-end across the API server, Redis queue, and worker.
- External API calls also produce structured logs. Reddit searches and submission fetches note the subreddit/query plus the current Reddit quota headers, while OpenAI requests log a masked prompt label, latency, and the reported token usage.
- Prometheus-compatible metrics are exported from the FastAPI app at `GET /metrics`. The following series are available out of the box:
  - `reddash_job_duration_seconds` histogram (job runtime)
  - `reddash_posts_per_minute` gauge (throughput of the latest job)
  - `reddash_openai_tokens_per_job` histogram (tokens consumed per job)
  - `reddash_queue_depth` gauge (current Redis queue depth)

  To scrape locally, run the dashboard (`uvicorn src.ui.report_dashboard:app --reload`) and open `http://localhost:8000/metrics` or point Prometheus at the same endpoint.

### Legacy `/api/reports` compatibility

- The `/api/reports` endpoint now queries the database for the most recently finished job with `status="succeeded"` and serializes its `PersistedPostReport` rows back into the original `PostReport` schema expected by the dashboard table. The response body remains a JSON list, but the handler also sets an `X-RedDash-Report-Stats` header (job id, completion timestamp, and report count) so the UI team can detect when a job changes.
- If no job has finished yet the API responds with an empty list plus the header `X-RedDash-Report-Message: No completed search jobs yet. Launch one via POST /api/searches.` to make the situation explicit.
- Once the dashboard migrates to the richer `/api/searches/{id}` endpoint, the header metadata can guide the UI in choosing which job id to hydrate.

## Caching and retention

- The `/api/searches` endpoint consults a Redis cache before enqueuing new work. Search parameters are normalized (subreddits are lower-cased and sorted, whitespace is collapsed, etc.) and hashed into a cache key. If a fresh job exists for the same inputs, the API immediately returns that job instead of duplicating the work. Control the freshness window via `SEARCH_CACHE_TTL_SECONDS` (defaults to one hour).
- Each worker run calls `src.infra.cleanup.purge_expired_jobs()` before processing new posts. This enforces a rolling TTL for `SearchJob` rows (and their related `PersistedPostReport` entries) so the Postgres database stays lean. Override the default 30-day limit with the `SEARCH_JOB_TTL_DAYS` environment variable.

