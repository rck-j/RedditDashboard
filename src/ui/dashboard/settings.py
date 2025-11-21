"""Shared configuration for the dashboard UI."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi.templating import Jinja2Templates

from src.env import REQUIRED_SECRETS, _require_env, ensure_required_secrets

ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

RATE_LIMIT_REQUESTS_PER_MINUTE = 30
JOB_CLEANUP_INTERVAL_SECONDS = int(os.getenv("JOB_CLEANUP_INTERVAL_SECONDS", "3600"))
ENABLE_JOB_CLEANUP = os.getenv("ENABLE_JOB_CLEANUP", "true").lower() != "false"

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "reddash_auth")
AUTH_COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN")
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "86400"))
AUTH_ALGORITHM = "HS256"
PASSWORD_HASH_NAME = os.getenv("PASSWORD_HASH_NAME", "sha256")
PASSWORD_ITERATIONS = int(os.getenv("PASSWORD_ITERATIONS", "130000"))
PASSWORD_SALT_BYTES = int(os.getenv("PASSWORD_SALT_BYTES", "16"))

API_REQUIRED_SECRETS = (
    *REQUIRED_SECRETS,
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "AUTH_SECRET_KEY",
)
ensure_required_secrets(API_REQUIRED_SECRETS)

GOOGLE_CLIENT_ID = _require_env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _require_env("GOOGLE_CLIENT_SECRET")
AUTH_SECRET_KEY = _require_env("AUTH_SECRET_KEY")
