"""
Centralized Configuration Loader for PAMM Test Suite
"""

import os
from pathlib import Path


class Config:
    ROOT_DIR = Path(__file__).resolve().parent.parent

    # Load .env manually to avoid extra dependencies
    _env_path = ROOT_DIR / ".env"
    _env_vars = {}
    if _env_path.exists():
        with open(_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    _env_vars[key.strip()] = val.strip().strip("\"'")

    BASE_URL = _env_vars.get("PLATFORM_BASE_URL", "")

    # Master Account
    MASTER_EMAIL = _env_vars.get("ADMIN_USERNAME", "")
    MASTER_PASSWORD = _env_vars.get("ADMIN_PASSWORD", "")
    MASTER_CLIENT_ID = _env_vars.get("MASTER_CLIENT_ID", "")
    MASTER_POOL_NAME = _env_vars.get("MASTER_POOL_NAME", "Alpha Pool")
    MANAGER_SHARE_PERCENT = float(_env_vars.get("MANAGER_SHARE_PERCENT", "20.0"))

    # Back-Office Admin
    BLACKADMIN_EMAIL = _env_vars.get("BLACKADMIN_EMAIL", "")
    BLACKADMIN_PASSWORD = _env_vars.get("BLACKADMIN_PASSWORD", "")

    # Follower Pool Settings
    FOLLOWER_PREFIX = "pamfollower"
    FOLLOWER_DOMAIN = "mailinator.com"
    FOLLOWER_PASSWORD = _env_vars.get("INVESTOR_PASSWORD") or _env_vars.get("FOLLOWER_PASSWORD", "")
    FOLLOWER_COUNT = int(_env_vars.get("TEST_FOLLOWER_COUNT", "100"))

    # Trading Defaults
    DEFAULT_SYMBOL = _env_vars.get("DEFAULT_SYMBOL", "BINANCE:BTCUSDT")
    DEFAULT_INSTANCE = _env_vars.get("DEFAULT_INSTANCE", "paper")

    # User Agent
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # Paths
    REPORTS_DIR = ROOT_DIR / "reports"
    CSV_REPORT_PATH = REPORTS_DIR / "pamm_100_audit.csv"
    HTML_REPORT_PATH = REPORTS_DIR / "pamm_audit_dashboard.html"
    STATE_FILE_PATH = REPORTS_DIR / "followers_state.json"

