#!/usr/bin/env python3
"""
PAMM Test Hub - dashboard server
================================
- Launch test suites from a registry and stream their logs live
- Evidence browser, run history, setup doctor
- Live pool / investor / allocation views and a manager dispatch-and-verify tool
"""

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, g, jsonify, make_response, render_template, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.doctor import run_checks, summarize as summarize_checks
from config.settings import (BASE_URL, DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_TOKEN, INVESTOR_COUNT,
                             INVESTOR_DOMAIN, INVESTOR_PREFIX, MANAGER_EMAIL, MAX_THREADS, DEFAULT_INSTANCE)
from dashboard.suites import SUITE_INDEX, build_command, effective_instance, needs_confirmation, public_registry
from test_suite.client import PammClient

app = Flask(__name__, template_folder="templates", static_folder="static")

dashboard_client = PammClient(max_workers=10)

EVIDENCE_DIR = BASE_DIR / "reports" / "evidence"
LOGS_DIR = BASE_DIR / "reports" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

LATEST_LOG_FILE = LOGS_DIR / "live_execution.log"

# Global process tracker
RUNNING_PROCESS = None
CURRENT_TASK = {
    "suite": None,
    "name": None,
    "status": "IDLE",  # IDLE, RUNNING, COMPLETED, FAILED, TERMINATED
    "started_at": None,
    "pid": None,
    "exit_code": None,
    "ended_at": None,
    "run_id": None,
    "params": {},
    "risk": None,
}
PROCESS_LOCK = threading.Lock()

RUNS_DIR = LOGS_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = LOGS_DIR / "run_history.json"
HISTORY_LOCK = threading.Lock()
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


# -------------------------------------------------------------------------------------
# Access control: localhost-only unless DASHBOARD_TOKEN is set
# -------------------------------------------------------------------------------------
@app.before_request
def _access_control():
    g.set_token_cookie = False
    if DASHBOARD_TOKEN:
        supplied = request.cookies.get("bt_dash") or request.headers.get("X-Dashboard-Token") or request.args.get("token") or ""
        if hmac.compare_digest(supplied, DASHBOARD_TOKEN):
            g.set_token_cookie = request.args.get("token") is not None
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized - open the dashboard once with /?token=<DASHBOARD_TOKEN>"}), 401
        return Response("Dashboard is locked. Open /?token=<DASHBOARD_TOKEN>", 401)
    # No token: only answer requests addressed to localhost (blocks DNS-rebinding / LAN access)
    host = (request.host or "").rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    if host not in LOOPBACK_HOSTS:
        return Response("Forbidden: set DASHBOARD_TOKEN in .env to allow non-localhost access.", 403)
    return None


@app.after_request
def _set_cookie(resp):
    if getattr(g, "set_token_cookie", False):
        resp.set_cookie("bt_dash", DASHBOARD_TOKEN, httponly=True, samesite="Strict")
    return resp


# -------------------------------------------------------------------------------------
# Run history (persisted so results survive restarts and are shareable)
# -------------------------------------------------------------------------------------
def _load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(items: list) -> None:
    HISTORY_FILE.write_text(json.dumps(items[-100:], indent=1), encoding="utf-8")


def history_add(entry: dict) -> None:
    with HISTORY_LOCK:
        items = _load_history()
        items.append(entry)
        _save_history(items)


def history_update(run_id: str, **fields) -> None:
    with HISTORY_LOCK:
        items = _load_history()
        for it in items:
            if it.get("id") == run_id:
                it.update(fields)
        _save_history(items)


_COUNT_CACHE = {}


def count_log_results(path: Path) -> tuple:
    """Count [PASS] / [FAIL] markers in a log (cached by mtime+size)."""
    try:
        st = path.stat()
    except OSError:
        return 0, 0
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _COUNT_CACHE:
        return _COUNT_CACHE[key]
    text = path.read_text(encoding="utf-8", errors="replace")
    res = (text.count("[PASS]"), text.count("[FAIL]") + text.count("[HIGH]"))
    _COUNT_CACHE.clear()
    _COUNT_CACHE[key] = res
    return res


def monitor_process(proc, run_id, log_handle):
    global RUNNING_PROCESS
    proc.wait()
    try:
        log_handle.close()
    except Exception:
        pass
    passed, failed = count_log_results(LATEST_LOG_FILE)
    with PROCESS_LOCK:
        if CURRENT_TASK["status"] != "TERMINATED":
            CURRENT_TASK["status"] = "COMPLETED" if proc.returncode == 0 else "FAILED"
        CURRENT_TASK["exit_code"] = proc.returncode
        CURRENT_TASK["ended_at"] = time.time()
        status = CURRENT_TASK["status"]
        started = CURRENT_TASK["started_at"] or time.time()
        RUNNING_PROCESS = None
    try:
        shutil.copyfile(LATEST_LOG_FILE, RUNS_DIR / f"{run_id}.log")
    except Exception:
        pass
    history_update(run_id, status=status, exit_code=proc.returncode, ended_at=datetime.now().isoformat(timespec="seconds"),
                   duration_sec=round(time.time() - started, 1), passed=passed, failed=failed)


@app.route("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/config")
def get_config():
    """Non-secret configuration, so the UI can show what it is pointed at."""
    return jsonify({
        "base_url": BASE_URL,
        "manager_email": MANAGER_EMAIL,
        "investor_prefix": INVESTOR_PREFIX,
        "investor_domain": INVESTOR_DOMAIN,
        "investor_count": INVESTOR_COUNT,
        "default_instance": DEFAULT_INSTANCE,
        "token_protected": bool(DASHBOARD_TOKEN),
        "env_file_present": (BASE_DIR / ".env").exists(),
    })


@app.route("/api/suites")
def get_suites():
    return jsonify({
        "suites": public_registry(),
        "defaults": {"investor_count": INVESTOR_COUNT, "lot": 0.5, "ramp": "5,10,25", "concurrency": 10, "ops": 4},
    })


_HEALTH_CACHE = {"t": 0.0, "data": None}


@app.route("/api/health")
def get_health():
    """Run the setup doctor. Network checks are cached for 20s; ?refresh=1 forces a re-run."""
    offline = request.args.get("offline") == "1"
    fresh = request.args.get("refresh") == "1"
    if not offline and not fresh and _HEALTH_CACHE["data"] and time.time() - _HEALTH_CACHE["t"] < 20:
        return jsonify(_HEALTH_CACHE["data"])
    results = run_checks(offline=offline)
    data = {"overall": summarize_checks(results), "checks": results, "checked_at": datetime.now().isoformat(timespec="seconds")}
    if not offline:
        _HEALTH_CACHE.update(t=time.time(), data=data)
    return jsonify(data)


@app.route("/api/status")
def get_status():
    with PROCESS_LOCK:
        task = dict(CURRENT_TASK)
    elapsed = 0
    if task["started_at"]:
        end = time.time() if task["status"] == "RUNNING" else (task.get("ended_at") or task["started_at"])
        elapsed = round(end - task["started_at"], 1)
    passed, failed = count_log_results(LATEST_LOG_FILE) if task["suite"] else (0, 0)
    return jsonify({**task, "elapsed_seconds": elapsed, "is_running": task["status"] == "RUNNING",
                    "pass_count": passed, "fail_count": failed})


@app.route("/api/logs")
def get_logs():
    if not LATEST_LOG_FILE.exists():
        return jsonify({"logs": ""})
    try:
        with open(LATEST_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"logs": "".join(lines[-300:]), "total_lines": len(lines)})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {str(e)}"})


@app.route("/api/logs/download")
def download_log():
    if not LATEST_LOG_FILE.exists():
        return jsonify({"error": "no log yet"}), 404
    return send_from_directory(LOGS_DIR, LATEST_LOG_FILE.name, as_attachment=True)


@app.route("/api/history")
def get_history():
    with HISTORY_LOCK:
        items = _load_history()
    return jsonify({"runs": list(reversed(items))})


@app.route("/api/history/<run_id>/log")
def get_history_log(run_id):
    if not run_id.replace("_", "").replace("-", "").isalnum():
        return jsonify({"error": "bad id"}), 400
    p = RUNS_DIR / f"{run_id}.log"
    if not p.exists():
        return jsonify({"error": "log not archived"}), 404
    return jsonify({"logs": p.read_text(encoding="utf-8", errors="replace")[-200000:]})


@app.route("/api/run", methods=["POST"])
def run_suite():
    global RUNNING_PROCESS
    data = request.get_json(silent=True) or {}
    suite_key = data.get("suite")
    suite = SUITE_INDEX.get(suite_key)
    if not suite:
        return jsonify({"error": f"Unknown suite '{suite_key}'"}), 400
    params = data.get("params") or {}

    if needs_confirmation(suite, params) and data.get("confirm") is not True:
        return jsonify({"error": "This action needs explicit confirmation.", "needs_confirmation": True,
                        "reason": ("Runs against the REAL instance." if effective_instance(suite, params) in ("real", "both")
                                   else "Closes positions and cancels orders.")}), 412

    cmd = build_command(suite, params, {"investor_count": INVESTOR_COUNT})
    title = suite["name"]
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + suite_key

    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with PROCESS_LOCK:
        if RUNNING_PROCESS and RUNNING_PROCESS.poll() is None:
            return jsonify({"error": "A test suite is already running.", "current": CURRENT_TASK["name"]}), 409

        with open(LATEST_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"=== LAUNCHING: {title} ===\n")
            shown = "(inline teardown script)" if "-c" in cmd[:3] else " ".join(["python"] + cmd[1:])
            f.write(f"Command  : {shown}\n")
            f.write(f"Instance : {effective_instance(suite, params)}\n")
            f.write(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        log_out = open(LATEST_LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=log_out, stderr=subprocess.STDOUT, cwd=str(BASE_DIR), text=True, env=env)
        RUNNING_PROCESS = proc
        CURRENT_TASK.update(suite=suite_key, name=title, status="RUNNING", started_at=time.time(), ended_at=None,
                            pid=proc.pid, exit_code=None, run_id=run_id,
                            params={**params, "instance": effective_instance(suite, params)}, risk=suite["risk"])
        history_add({"id": run_id, "suite": suite_key, "name": title, "status": "RUNNING",
                     "started_at": datetime.now().isoformat(timespec="seconds"),
                     "params": CURRENT_TASK["params"]})
        threading.Thread(target=monitor_process, args=(proc, run_id, log_out), daemon=True).start()

    return jsonify({"message": f"Launched {title}", "pid": proc.pid, "suite": suite_key, "run_id": run_id})


@app.route("/api/stop", methods=["POST"])
def stop_suite():
    global RUNNING_PROCESS
    with PROCESS_LOCK:
        if RUNNING_PROCESS and RUNNING_PROCESS.poll() is None:
            CURRENT_TASK["status"] = "TERMINATED"
            RUNNING_PROCESS.terminate()
            return jsonify({"message": "Run stopped. Positions may still be open - use Emergency Teardown to clean up."})
    return jsonify({"message": "Nothing is running."})


# -------------------------------------------------------------------------------------
# Evidence summary (real numbers for the header cards)
# -------------------------------------------------------------------------------------
_FAMILY_RE = re.compile(r"^(?P<fam>.+?)_(?P<ts>\d{8}_\d{6})$")
_SUMMARY_CACHE = {}


def _count_statuses(obj, acc):
    if isinstance(obj, dict):
        st = obj.get("status")
        if isinstance(st, str):
            s = st.upper()
            if s.startswith("PASS"):
                acc["passed"] += 1
            elif s.startswith("FAIL") or s in ("ERROR", "HIGH"):
                acc["failed"] += 1
            elif s.startswith("WARN") or s == "MED":
                acc["warned"] += 1
        for v in obj.values():
            _count_statuses(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _count_statuses(v, acc)


@app.route("/api/summary")
def get_summary():
    families = {}
    for p in EVIDENCE_DIR.glob("*.json"):
        if p.stem in ("baseline_snapshot",) or "jmeter" in p.stem:
            continue
        m = _FAMILY_RE.match(p.stem)
        fam = m.group("fam") if m else p.stem
        mt = p.stat().st_mtime
        if fam not in families or mt > families[fam][1]:
            families[fam] = (p, mt)
    rows = []
    for fam, (p, mt) in families.items():
        key = (p.name, mt)
        if key not in _SUMMARY_CACHE:
            acc = {"passed": 0, "failed": 0, "warned": 0}
            try:
                _count_statuses(json.loads(p.read_text(encoding="utf-8")), acc)
            except Exception:
                pass
            _SUMMARY_CACHE[key] = acc
        acc = _SUMMARY_CACHE[key]
        if acc["passed"] + acc["failed"] + acc["warned"] == 0:
            continue
        rows.append({"family": fam, "label": fam.replace("_", " ").replace(" evidence", "").title(), "file": p.name,
                     "modified": datetime.fromtimestamp(mt).isoformat(timespec="seconds"), **acc,
                     "status": "FAIL" if acc["failed"] else ("WARN" if acc["warned"] else "PASS")})
    rows.sort(key=lambda r: r["modified"], reverse=True)
    with HISTORY_LOCK:
        hist = _load_history()
    last = hist[-1] if hist else None
    return jsonify({
        "families": rows,
        "totals": {"suites": len(rows), "suites_green": sum(1 for r in rows if r["status"] == "PASS"),
                   "passed": sum(r["passed"] for r in rows), "failed": sum(r["failed"] for r in rows)},
        "last_run": last,
        "runs_recorded": len(hist),
    })


@app.route("/api/reports")
def get_reports():
    reports = []
    seen_files = set()
    search_dirs = [BASE_DIR / "reports", BASE_DIR / "reports" / "evidence"]

    for d in search_dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), key=os.path.getmtime, reverse=True):
            if f.is_file() and f.suffix in [".json", ".md", ".csv", ".jtl"] and not f.name.startswith("baseline") and f.name not in seen_files:
                seen_files.add(f.name)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                m = _FAMILY_RE.match(f.stem)
                category = (m.group("fam") if m else f.stem).replace("_evidence", "").replace("_", " ").title()

                reports.append({
                    "filename": f.name,
                    "category": category,
                    "type": f.suffix.replace(".", "").upper(),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "modified_at": mtime
                })
    return jsonify({"reports": reports})


@app.route("/api/reports/<filename>")
def get_report_content(filename):
    search_paths = [
        BASE_DIR / "reports" / "evidence" / filename,
        BASE_DIR / "reports" / filename
    ]
    file_path = next((p for p in search_paths if p.exists() and p.is_file()), None)
    if not file_path:
        return jsonify({"error": "Report file not found"}), 404
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            return jsonify({
                "filename": filename,
                "is_json": filename.endswith(".json"),
                "content": content
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


SCREENSHOT_DIR = BASE_DIR / "reports" / "evidence" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


@app.route("/api/screenshots")
def list_screenshots():
    if not SCREENSHOT_DIR.exists():
        return jsonify({"screenshots": []})
    files = [f.name for f in SCREENSHOT_DIR.iterdir() if f.suffix.lower() in [".png", ".jpg", ".jpeg"]]
    return jsonify({"screenshots": sorted(files)})


@app.route("/api/screenshots/<filename>")
def get_screenshot(filename):
    return send_from_directory(SCREENSHOT_DIR, filename)





# -------------------------------------------------------------------------------------
# Live PAMM data (read-only except /api/dispatch)
# -------------------------------------------------------------------------------------
_LIVE = {}
_LIVE_LOCK = threading.Lock()


def _cached(key, ttl, fn):
    with _LIVE_LOCK:
        hit = _LIVE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _LIVE_LOCK:
        _LIVE[key] = (time.time(), val)
    return val


def _instance_arg() -> str:
    v = request.args.get("instance", "paper")
    return v if v in ("paper", "real") else "paper"


def _pool_snapshot(instance: str) -> dict:
    f = dashboard_client.follows(MANAGER_EMAIL, instance)
    if not f.ok:
        raise RuntimeError(f"manager follows failed: HTTP {f.status} {f.error}")
    return f.data


@app.route("/api/pool")
def api_pool():
    """Master profile, pool totals and public master stats."""
    inst = _instance_arg()
    try:
        f = _cached(("pool", inst), 4, lambda: _pool_snapshot(inst))
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    pool = [p for p in (f.get("pool") or []) if p.get("status") == "active"]
    master = f.get("master") or {}
    stats = {}
    m = dashboard_client.masters(MANAGER_EMAIL, inst)
    for x in (m.get("masters") or []):
        if x.get("client_id") == master.get("client_id"):
            stats = x.get("stats") or {}
    positions = dashboard_client.positions(MANAGER_EMAIL, inst)
    return jsonify({"master": master, "pool_total": f.get("pool_total"), "members": len(pool), "stats": stats,
                    "share_percent": master.get("share_percent"),
                    "manager_funds": positions.get("funds") or {},
                    "manager_open_positions": len(positions.get("positions") or []),
                    "deposit_min": min((p["deposit"] for p in pool), default=0),
                    "deposit_max": max((p["deposit"] for p in pool), default=0)})


@app.route("/api/investors")
def api_investors():
    """Investor grid: one admin call + one pool call (no per-account logins)."""
    inst = _instance_arg()
    try:
        f = _cached(("pool", inst), 4, lambda: _pool_snapshot(inst))
        members = _cached(("members",), 30, dashboard_client.admin_members)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    by_id = {p["follower_client_id"]: p for p in (f.get("pool") or [])}
    pat = re.compile(rf"^{re.escape(INVESTOR_PREFIX)}(\d+)@{re.escape(INVESTOR_DOMAIN)}$", re.I)
    total = sum(p["deposit"] for p in by_id.values() if p.get("status") == "active") or 1
    rows = []
    for m in members:
        mm = pat.match(m.get("email", ""))
        if not mm:
            continue
        p = by_id.get(m["member_id"])
        rows.append({"index": int(mm.group(1)), "email": m["email"], "member_id": m["member_id"],
                     "wallet": (m.get("balances") or {}).get(inst), "in_pool": bool(p and p.get("status") == "active"),
                     "deposit": p["deposit"] if p else None, "share_pct": round(100 * p["deposit"] / total, 4) if p else None,
                     "running_pnl": p.get("running_pnl") if p else None, "joined_at": p.get("joined_at") if p else None})
    rows.sort(key=lambda r: r["index"])
    return jsonify({"investors": rows, "count": len(rows), "pool_total": f.get("pool_total")})


@app.route("/api/account/<email>")
def api_account(email):
    inst = _instance_arg()
    allowed = email == MANAGER_EMAIL or re.match(rf"^{re.escape(INVESTOR_PREFIX)}\d+@{re.escape(INVESTOR_DOMAIN)}$", email, re.I)
    if not allowed:
        return jsonify({"error": "unknown account"}), 404
    try:
        fol = dashboard_client.follows(email, inst)
        pos = dashboard_client.positions(email, inst)
        al = dashboard_client.allocations(email, limit=25, instance=inst)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"email": email, "is_master": bool(fol.get("is_master")), "subscriptions": fol.get("subscriptions") or [],
                    "funds": pos.get("funds") or {}, "positions": pos.get("positions") or [],
                    "allocations": al.get("allocations") or []})


@app.route("/api/allocations")
def api_allocations():
    """Order-flow tape: latest settled trades with their per-investor fan-out."""
    inst = _instance_arg()
    r = dashboard_client.oms(MANAGER_EMAIL, "GET", "/pamm/allocations", inst, params={"role": "master", "limit": 500})
    rows = r.get("allocations") or []
    trades = {}
    for a in rows:
        t = trades.setdefault(a["trade_id"], {"trade_id": a["trade_id"], "at": a["at"], "order_pnl": a["order_pnl"],
                                              "pool_total": a["pool_total"], "investors": 0, "sum_shared": 0.0, "sum_pct": 0.0})
        t["investors"] += 1
        t["sum_shared"] = round(t["sum_shared"] + a["shared_pnl"], 4)
        t["sum_pct"] = round(t["sum_pct"] + a["percentage"], 4)
    out = sorted(trades.values(), key=lambda t: t["trade_id"], reverse=True)[:40]
    return jsonify({"trades": out, "rows_read": len(rows)})


@app.route("/api/dispatch", methods=["POST"])
def api_dispatch():
    """Manager places ONE paper market order, holds briefly, closes it, then every investor's allocation is
    compared with the expected pro-rata value. Paper only. Refused while a suite is running."""
    data = request.get_json(silent=True) or {}
    if data.get("instance", "paper") != "paper":
        return jsonify({"error": "Dispatch is paper-only."}), 400
    if data.get("confirm") is not True:
        return jsonify({"error": "needs confirmation", "needs_confirmation": True,
                        "reason": "Places a paper manager order and closes it, which settles PnL into the pool."}), 412
    side = str(data.get("side", "BUY")).upper()
    if side not in ("BUY", "SELL"):
        return jsonify({"error": "side must be BUY or SELL"}), 400
    try:
        qty = float(data.get("qty", 0.5))
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be a number"}), 400
    if not 0 < qty <= 5:
        return jsonify({"error": "qty must be between 0 and 5"}), 400
    with PROCESS_LOCK:
        if RUNNING_PROCESS and RUNNING_PROCESS.poll() is None:
            return jsonify({"error": "A test suite is running; wait for it to finish."}), 409
    import argparse
    from test_suite.common import Ctx, Evidence
    ctx = Ctx(argparse.Namespace(instance="paper", start=1, end=1, lot=qty))
    ev = Evidence("dispatch", "paper")
    if not ctx.discover(ev):
        return jsonify({"error": "manager is not a PAMM master"}), 502
    pool = ctx.pool_by_investor()
    res = ctx.trade_and_settle(ev, side=side, qty=qty, want_sign=0, min_abs_pnl=0.5, max_wait=25, settle_wait=25)
    if not res:
        return jsonify({"error": "dispatch failed", "log": ev.checks}), 502
    total = sum(p["deposit"] for p in pool.values()) or 1
    by = {r["follower"]: r for r in res["rows"]}
    pnl = res["rows"][0]["order_pnl"] if res["rows"] else res["realized_pnl"]
    table = []
    for fid, p in pool.items():
        exp = pnl * p["deposit"] / total
        got = by.get(fid)
        if got:
            status = "OK" if abs(got["shared_pnl"] - exp) <= 0.011 else "MISMATCH"
        else:
            status = "DUST" if abs(exp) < 0.005 else "MISSING"
        table.append({"follower": fid, "name": p.get("follower_name"), "deposit": p["deposit"], "expected": round(exp, 4),
                      "actual": got["shared_pnl"] if got else None, "status": status})
    table.sort(key=lambda r: r["deposit"], reverse=True)
    return jsonify({"realized_pnl": res["realized_pnl"], "order_pnl": pnl, "trade_ids": res["trade_ids"],
                    "settle_lag_s": res["settle_lag_s"], "tape": res["tape"], "table": table,
                    "summary": {s: sum(1 for r in table if r["status"] == s) for s in ("OK", "DUST", "MISSING", "MISMATCH")}})


def main():
    if DASHBOARD_HOST not in LOOPBACK_HOSTS and not DASHBOARD_TOKEN:
        print("Refusing to bind to a non-loopback host without DASHBOARD_TOKEN. Set DASHBOARD_TOKEN in .env.")
        sys.exit(2)
    print(f"PAMM Test Hub on http://{DASHBOARD_HOST}:{DASHBOARD_PORT}  (token protected: {bool(DASHBOARD_TOKEN)})")
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
