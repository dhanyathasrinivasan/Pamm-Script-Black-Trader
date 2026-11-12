"""
Setup health checks ("doctor"). Used by `python scripts/doctor.py` and the dashboard's Getting Started tab.
Every check returns {id, label, status: ok|warn|fail|skip, detail, fix}. Secrets are never included.
"""

import importlib
import re
import sys
from typing import Dict, List

from config import settings as S

PLACEHOLDER = re.compile(r"^(your_|changeme|example|<|xxx)", re.I)


def _r(id_, label, status, detail="", fix=""):
    return {"id": id_, "label": label, "status": status, "detail": detail, "fix": fix}


def check_python():
    v = sys.version_info
    ok = v >= (3, 9)
    return _r("python", "Python 3.9+", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro}",
              "" if ok else "Install Python 3.9 or newer from python.org")


def check_deps():
    out = []
    for mod, pip, required in (("requests", "requests", True), ("dotenv", "python-dotenv", True),
                               ("flask", "flask", True), ("playwright", "playwright", False)):
        try:
            importlib.import_module(mod)
            out.append(_r(f"dep_{mod}", f"Package `{pip}`", "ok"))
        except ImportError:
            out.append(_r(f"dep_{mod}", f"Package `{pip}`", "fail" if required else "warn",
                          "not installed" + ("" if required else " (only needed for browser verification)"),
                          "pip install -r requirements.txt" + ("" if required else " && playwright install chromium")))
    return out


def check_env_file():
    p = S.BASE_DIR / ".env"
    return _r("env_file", ".env file present", "ok" if p.exists() else "fail", "found" if p.exists() else "missing",
              "" if p.exists() else "Copy .env.example to .env and fill in your credentials")


def check_credentials():
    out = []
    items = (("PLATFORM_BASE_URL", S.BASE_URL), ("ADMIN_USERNAME", S.MANAGER_EMAIL), ("ADMIN_PASSWORD", S.MANAGER_PASSWORD),
             ("BLACKADMIN_EMAIL", S.ADMIN_EMAIL), ("BLACKADMIN_PASSWORD", S.ADMIN_PASSWORD), ("INVESTOR_PASSWORD", S.INVESTOR_PASSWORD))
    for name, val in items:
        bad = (not val) or bool(PLACEHOLDER.match(val))
        out.append(_r(f"var_{name}", f"{name} configured", "fail" if bad else "ok",
                      "empty or still a placeholder" if bad else "set", f"Set {name} in .env" if bad else ""))
    return out


def check_reachable():
    import requests
    try:
        r = requests.get(S.BASE_URL, timeout=8)
        return _r("reach", f"Platform reachable ({S.BASE_URL})", "ok", f"HTTP {r.status_code}")
    except Exception as e:
        return _r("reach", f"Platform reachable ({S.BASE_URL})", "fail", type(e).__name__,
                  "Check PLATFORM_BASE_URL in .env and your network / VPN")


def _client():
    from test_suite.client import PammClient
    return PammClient(max_workers=4, timeout=10)


def check_admin(client):
    try:
        client.admin_token()
        return _r("admin", "Back-office admin login", "ok")
    except Exception as e:
        return _r("admin", "Back-office admin login", "fail", str(e)[:120], "Verify BLACKADMIN_EMAIL / BLACKADMIN_PASSWORD in .env")


def check_manager(client):
    label = f"PAMM manager login ({S.MANAGER_EMAIL})"
    try:
        r = client.follows(S.MANAGER_EMAIL, S.DEFAULT_INSTANCE)
    except Exception as e:
        return _r("manager", label, "fail", str(e)[:120], "Verify ADMIN_USERNAME / ADMIN_PASSWORD in .env (the manager account)")
    m = r.get("master") if r.ok else None
    if not m:
        return _r("manager", label, "fail", "logged in, but this account has no PAMM master profile",
                  "Create the master profile for this account in the trader terminal (PAMM tab)")
    return _r("manager", label, "ok", f"master '{m.get('display_name')}', share {m.get('share_percent')}%, pool {r.get('pool_total')}")


def check_investors(client):
    pat = re.compile(rf"^{re.escape(S.INVESTOR_PREFIX)}(\d+)@{re.escape(S.INVESTOR_DOMAIN)}$", re.I)
    try:
        found = sum(1 for m in client.admin_members() if pat.match(m.get("email", "")))
    except Exception as e:
        return _r("investors", "Investor accounts exist", "fail", str(e)[:120])
    label = f"Investor accounts exist ({found}/{S.INVESTOR_COUNT})"
    if found >= S.INVESTOR_COUNT:
        return _r("investors", label, "ok")
    return _r("investors", label, "warn" if found else "fail", "some investor accounts are missing",
              "Create them in the back-office, or lower TEST_FOLLOWER_COUNT in .env")


def check_pool(client):
    try:
        r = client.follows(S.MANAGER_EMAIL, S.DEFAULT_INSTANCE)
        n = len([p for p in (r.get("pool") or []) if p.get("status") == "active"])
    except Exception:
        n = 0
    ok = n > 0
    return _r("pool", f"Investors in the PAMM pool ({n})", "ok" if ok else "warn",
              "" if ok else "the pool is empty, so allocation suites have nothing to verify",
              "" if ok else "Have investors join the manager's pool (PAMM tab in the terminal)")


def check_writable():
    d = S.BASE_DIR / "reports"
    try:
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".write_test"
        t.write_text("x")
        t.unlink()
        return _r("writable", "reports/ is writable", "ok")
    except Exception as e:
        return _r("writable", "reports/ is writable", "fail", str(e)[:120])


def run_checks(offline: bool = False) -> List[Dict]:
    results: List[Dict] = [check_python()]
    results.extend(check_deps())
    results.append(check_env_file())
    results.extend(check_credentials())
    results.append(check_writable())
    creds_ok = all(r["status"] == "ok" for r in results if r["id"].startswith("var_"))
    if offline:
        return results
    reach = check_reachable()
    results.append(reach)
    if reach["status"] != "ok" or not creds_ok:
        for id_, label in (("admin", "Back-office admin login"), ("manager", "PAMM manager login"), ("investors", "Investor accounts exist")):
            results.append(_r(id_, label, "skip", "blocked by an earlier failure"))
        return results
    client = _client()
    results.append(check_admin(client))
    results.append(check_manager(client))
    results.append(check_investors(client))
    results.append(check_pool(client))
    return results


def summarize(results: List[Dict]) -> str:
    if any(r["status"] == "fail" for r in results):
        return "fail"
    return "warn" if any(r["status"] == "warn" for r in results) else "ok"
