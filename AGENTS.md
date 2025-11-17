# Repository Guidelines

## Project Structure & Module Organization
RedDash has two moving parts. `red.py` streams Reddit searches through PRAW, calls OpenAI for initial/deep assessments, and writes incremental JSON payloads inside `data/` (default `data/report.json`). The FastAPI dashboard in `src/ui/report_dashboard.py` reads that file, exposes `/api/reports`, and renders `templates/report_dashboard.html` via Jinja2. Prompt text overrides belong in `config/prompts.json`; keep secrets in `.env`. Put exploratory notebooks or large exports under `data/` so `src/` stays import-safe.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate` – provision an isolated interpreter before installing `praw`, `fastapi`, `uvicorn[standard]`, `openai`, `python-dotenv`, and `pydantic`.
- `python red.py --subs smallbusiness Entrepreneur --query "automation" --comments-limit 3 --report-path data/report.json` – fetch posts and regenerate the dashboard dataset; flags mirror the argparse definitions in `red.py`.
- `uvicorn src.ui.report_dashboard:app --reload --port 8000` – run the UI locally; ensure `data/report.json` exists so `get_reports()` can warm its cache.
- `pytest` – place tests under `tests/` (mirroring module names) and run them before opening a PR.

## Coding Style & Naming Conventions
Target Python 3.11+, 4-space indentation, and full type hints (classes and helpers already model this). Keep helper functions small (`_load_json`, `_parse_as_model`) and favor early validation using Pydantic models. Use `snake_case` for functions/vars, `PascalCase` for dataclasses/models, and limit lines to roughly 100 characters. Template logic should stay inside `templates/` while pure data handling remains in Python modules.

## Testing Guidelines
Automated tests are currently absent; new work should introduce focused `pytest` coverage. Mock `praw` and `OpenAI` clients so tests assert behavior around `_parse_as_model`, `AutomationAnalyzer.analyze_post`, and FastAPI route responses without hitting external services. Store reusable samples under `tests/fixtures/` and keep snapshots redacted (no secrets or user handles). Add regression tests whenever updating parsing, caching, or CLI argument handling.

## Commit & Pull Request Guidelines
Write concise, imperative commit titles (e.g., `Add dashboard cache busting`) and avoid bundling unrelated fixes. PR descriptions should cover motivation, functional changes, test evidence (`pytest`, manual dashboard check), screenshots for UI differences, and any env/config updates. Reference related issues and tag reviewers tied to `red.py` or `src/ui/` accordingly.

## Security & Configuration Tips
Set `OPENAI_API_KEY`, `PRAW_CLIENT_ID`, `PRAW_CLIENT_SECRET`, and `PRAW_USER_AGENT` in the environment or `.env`; the CLI raises immediately if any are missing. Never commit populated `data/*.json` files unless scrubbed, because they can contain Reddit usernames or internal commentary. Keep long-running token/secret rotations documented in PRs so future agents know when to reset credentials.
