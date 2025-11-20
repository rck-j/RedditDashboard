from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from src.jobs import search_runner
from src.ui import report_dashboard

from tests.helpers import configure_test_app

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # type: ignore  # noqa: E402


@pytest.fixture()
def browser_page():
    with playwright_sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - depends on local browsers
            pytest.skip(f"Unable to launch Chromium: {exc}")
        page = browser.new_page()
        yield page
        browser.close()


@pytest.fixture()
def dashboard_server(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    context = configure_test_app(monkeypatch, tmp_path, auto_run_queue=True, analysis_delay=0.1)

    sample_posts = [
        {
            "id": "alpha",
            "subreddit": "agents",
            "title": "Alpha automation idea",
            "url": "https://reddit.com/alpha",
            "permalink": "/r/agents/alpha",
            "score": 12,
            "num_comments": 4,
            "complexity": "high",
            "tools": ["zapier", "slack"],
            "is_automation": True,
        },
        {
            "id": "beta",
            "subreddit": "automation",
            "title": "Beta workflow",
            "url": "https://reddit.com/beta",
            "permalink": "/r/automation/beta",
            "score": 5,
            "num_comments": 2,
            "complexity": "medium",
            "tools": ["notion"],
            "is_automation": True,
        },
        {
            "id": "gamma",
            "subreddit": "agents",
            "title": "Gamma manual process",
            "url": "https://reddit.com/gamma",
            "permalink": "/r/agents/gamma",
            "score": 3,
            "num_comments": 1,
            "complexity": "low",
            "tools": ["sheets"],
            "is_automation": False,
        },
    ]

    def _search_posts(*_args, **_kwargs):
        for post in sample_posts:
            time.sleep(0.05)
            yield post

    monkeypatch.setattr(search_runner, "search_posts", _search_posts)

    port = _reserve_port()
    config = uvicorn.Config(
        report_dashboard.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(port)

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        context.queue.join_all()


def _reserve_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _host, port = sock.getsockname()
    sock.close()
    return int(port)


def _wait_for_server(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Server did not start before timeout.")


@pytest.mark.e2e
def test_dashboard_smoke(dashboard_server: str, browser_page) -> None:
    page = browser_page
    page.goto(dashboard_server, wait_until="networkidle")

    page.wait_for_selector("#time-filter option")
    first_time_filter = page.locator("#time-filter option").first.get_attribute("value")
    if first_time_filter:
        page.select_option("#time-filter", value=first_time_filter)
    page.select_option("#subreddit-select", value="smallbusiness")
    page.fill("#query-input", "automation agents")
    page.click("text=Launch search")

    status_chip = page.locator("#job-status .status-chip")
    expect(status_chip).to_have_text(
        r"queued|running|succeeded", timeout=10_000
    )
    expect(status_chip).to_have_text("succeeded", timeout=15_000)

    rows = page.locator("#report-table tbody tr")
    expect(rows).to_have_count(3, timeout=20_000)

    page.select_option("#subreddit-filter", value="agents")
    expect(rows).to_have_count(2, timeout=5_000)

    page.fill("#search-filter", "Gamma")
    expect(rows).to_have_count(1, timeout=5_000)
    page.fill("#search-filter", "")
    expect(rows).to_have_count(2, timeout=5_000)

    page.click("#automation-chips .chip[data-value='true']")
    expect(rows).to_have_count(1, timeout=5_000)
    page.click("#automation-chips .chip[data-value='any']")
    expect(rows).to_have_count(2, timeout=5_000)
