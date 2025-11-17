# RedDash Dashboard for visualizing search results using PRAW

A web application for reviewing query results from the PRAW. Enter the subreddit and a search term and a dashboard of relevant statistics will be generated to give the user insight into a given subreddit/topic.

## Setup

1. Create a virtual environment and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` (if available) or create a `.env` file with your Reddit and OpenAI credentials (`PRAW_CLIENT_ID`, `PRAW_CLIENT_SECRET`, `PRAW_USER_AGENT`, `OPENAI_API_KEY`, etc.).
3. Optional: override the default prompts by editing `config/prompts.json`.

## Running the FastAPI dashboard

1. Start the development server:
   ```bash
   uvicorn src.ui.report_dashboard:app --reload
   ```
2. Visit `http://localhost:8000` to load the HTMX-powered dashboard which renders the latest report stored in `data/report.json`.

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
4. Each POST creates a `SearchJob` database row, enqueues `src.jobs.search_runner.run` via RQ, and immediately returns the job metadata (including timestamps, counts, and status). RQ workers stream their progress back into SQLite, creating `PersistedPostReport` rows linked to each job.

## Caching and retention

- The `/api/searches` endpoint consults a Redis cache before enqueuing new work. Search parameters are normalized (subreddits are lower-cased and sorted, whitespace is collapsed, etc.) and hashed into a cache key. If a fresh job exists for the same inputs, the API immediately returns that job instead of duplicating the work. Control the freshness window via `SEARCH_CACHE_TTL_SECONDS` (defaults to one hour).
- Each worker run calls `src.infra.cleanup.purge_expired_jobs()` before processing new posts. This enforces a rolling TTL for `SearchJob` rows (and their related `PersistedPostReport` entries) so the SQLite file stays small. Override the default 30-day limit with the `SEARCH_JOB_TTL_DAYS` environment variable.

