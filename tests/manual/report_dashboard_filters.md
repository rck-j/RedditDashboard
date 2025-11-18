# Report Dashboard Filter Regression Checklist

These manual smoke tests ensure front-end filters keep working even when HTMX swaps or new jobs hydrate the table multiple times.

## Setup
1. Start the FastAPI dashboard: `uvicorn src.ui.report_dashboard:app --reload --port 8000`.
2. In another terminal, launch a sample job (adjust the query/subreddits as needed):
   ```bash
   python red.py --subs smallbusiness Entrepreneur --query "automation" --comments-limit 3 --report-path data/report.json
   ```
3. Open http://localhost:8000 in the browser once the job succeeds.

## Scenarios
1. **Persist subreddit filter across job loads**
   - Pick any subreddit from the “Subreddit” filter.
   - Trigger a new job via the search form.
   - Confirm the selected subreddit chip stays active once the reports refresh and that the table remains filtered accordingly.
2. **Persist complexity filter when switching history entries**
   - Select any automation complexity from the dropdown.
   - Use the “Job history” select to pick another completed job.
   - Validate that the dropdown still shows the chosen complexity and the table continues to honor it.
3. **Search text survives HTMX swaps**
   - Type a keyword in the “Search titles” input and verify the table filters immediately.
   - Use the job history select again (causes an HTMX swap) and ensure the keyword remains in the input and the table still filtered.
4. **Automation chips stay in sync**
   - Click the “Automation only” chip and confirm the table shrinks to automation-ready posts.
   - Start another job and wait for its results; the chip should remain active and the table should stay filtered.
5. **Reset edge case (no matching rows)**
   - Choose a subreddit or complexity that yields no rows and confirm the empty-state banner appears.
   - Switch to another job and ensure the filter choice remains selected even if it still yields zero rows.

Record PASS/FAIL for each scenario before shipping changes.
