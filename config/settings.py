"""
Centralised configuration loader (reads .env; NO hardcoded identities or credentials).

Variable names follow the existing PAMM .env.example so current .env files keep working.
The PAMM master's client id is discovered at runtime by logging in as the manager.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=BASE_DIR / ".env")
except ImportError:  # minimal fallback parser
    _env = BASE_DIR / ".env"
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))


def _int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return int(default)


def _float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return float(default)


# --- Platform ---
BASE_URL = (os.getenv("PLATFORM_BASE_URL") or os.getenv("BASE_URL") or "").rstrip("/")
DEFAULT_INSTANCE = os.getenv("DEFAULT_INSTANCE", "paper")

# --- PAMM manager (the master / pool owner) ---
MANAGER_EMAIL = os.getenv("ADMIN_USERNAME", "")
MANAGER_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
MANAGER_SHARE_PERCENT = _float("MANAGER_SHARE_PERCENT", "20")   # expected performance share (%)
DEFAULT_SYMBOL = os.getenv("DEFAULT_SYMBOL", "BINANCE:BTCUSDT")

# --- Back-office admin (creates members, sets balances) ---
ADMIN_EMAIL = os.getenv("BLACKADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("BLACKADMIN_PASSWORD", "")

# --- Investors ---
INVESTOR_PREFIX = os.getenv("INVESTOR_PREFIX", "pamfollower")
INVESTOR_DOMAIN = os.getenv("INVESTOR_DOMAIN", "mailinator.com")
INVESTOR_PASSWORD = os.getenv("INVESTOR_PASSWORD") or os.getenv("FOLLOWER_PASSWORD", "")
INVESTOR_COUNT = _int("TEST_FOLLOWER_COUNT", "100")
MAX_THREADS = max(1, min(_int("MAX_CONCURRENT_THREADS", "25"), 100))

# --- Dashboard server ---
# Binds to localhost by default. Set DASHBOARD_HOST=0.0.0.0 only together with DASHBOARD_TOKEN.
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _int("DASHBOARD_PORT", os.getenv("PORT") or "5001")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")


def investor_email(i: int) -> str:
    return f"{INVESTOR_PREFIX}{i}@{INVESTOR_DOMAIN}"
