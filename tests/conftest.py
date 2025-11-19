"""Pytest configuration shared across the suite."""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("PRAW_CLIENT_ID", "client")
os.environ.setdefault("PRAW_CLIENT_SECRET", "secret")
os.environ.setdefault("PRAW_USER_AGENT", "pytest-agent")
