"""
Shared helpers for every runner: evidence/report writer, argument parsing, PAMM discovery and
trade-and-settle helpers.

Output markers (parsed by the dashboard):
    [PASS] [FAIL] [HIGH] [MED] [INFO] [WARN] [SKIP]
Severity model:  HIGH = invariant violated (bug) -> FAIL, MED = suspicious -> WARN, INFO = measurement.
"""

import argparse
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings as S  # noqa: E402
from test_suite.client import PammClient, Resp  # noqa: E402

EVIDENCE_DIR = BASE_DIR / "reports" / "evidence"
TOL = 0.011          # money rounding tolerance (platform rounds shared_pnl to 2 decimals)


# ---------------------------------------------------------------------------------------
# Cross-process lock: the manager account is a shared resource. Two runs trading at the same time
# (CLI + dashboard, or two terminals) would silently corrupt each other's allocation checks.
# ---------------------------------------------------------------------------------------
LOCK_FILE = BASE_DIR / "reports" / "logs" / ".manager.lock"


@contextmanager
def manager_lock(wait: float = 900.0, stale: float = 300.0):
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline, announced = time.time() + wait, False
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_FILE.stat().st_mtime > stale:     # crashed run left it behind
                    LOCK_FILE.unlink()
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                raise TimeoutError("manager account is busy (another run is trading); try again later")
            if not announced:
                print("[INFO] waiting for another run to finish using the manager account...", flush=True)
                announced = True
            time.sleep(1.0)
    try:
        yield
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------
# Evidence / results
# ---------------------------------------------------------------------------------------
class Evidence:
    def __init__(self, suite: str, instance: str = "paper"):
        self.suite = suite
        self.instance = instance
        self.started = datetime.now()
        self.checks: List[dict] = []
        self.notes: List[str] = []
        self.data: Dict[str, object] = {}
        self._lock = threading.Lock()

    def _add(self, status: str, name: str, detail: str = "", **extra):
        with self._lock:
            self.checks.append({"status": status, "name": name, "detail": detail, **extra})
        print(f"[{status}] {name}" + (f" - {detail}" if detail else ""), flush=True)

    def passed(self, name, detail=""):
        self._add("PASS", name, detail)

    def failed(self, name, detail=""):
        self._add("FAIL", name, detail)

    def high(self, name, detail=""):
        self._add("HIGH", name, detail)

    def med(self, name, detail=""):
        self._add("MED", name, detail)

    def info(self, name, detail=""):
        self._add("INFO", name, detail)

    def skip(self, name, detail=""):
        self._add("SKIP", name, detail)

    def check(self, cond: bool, name: str, detail: str = "", sev: str = "FAIL") -> bool:
        """Assert and ALWAYS print what was compared, so a check can never pass silently."""
        if cond:
            self.passed(name, detail)
        elif sev == "MED":
            self.med(name, detail)
        elif sev == "HIGH":
            self.high(name, detail)
        else:
            self.failed(name, detail)
        return cond

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c["status"] in ("FAIL", "HIGH"))

    def finish(self) -> int:
        counts: Dict[str, int] = {}
        for c in self.checks:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        if not any(c["status"] == "PASS" for c in self.checks) and self.n_fail == 0:
            self._add("FAIL", "suite checked nothing", "no PASS markers were produced (vacuous run)")
            counts["FAIL"] = counts.get("FAIL", 0) + 1
        stamp = self.started.strftime("%Y%m%d_%H%M%S")
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"suite": self.suite, "instance": self.instance, "started": self.started.isoformat(timespec="seconds"),
                   "finished": datetime.now().isoformat(timespec="seconds"), "counts": counts,
                   "checks": self.checks, "notes": self.notes, "data": self.data}
        base = EVIDENCE_DIR / f"{self.suite}_evidence_{stamp}"
        base.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md = [f"# {self.suite} - {self.started:%Y-%m-%d %H:%M:%S}", f"Instance: `{self.instance}`  ",
              "Results: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), "",
              "| Status | Check | Detail |", "|---|---|---|"]
        for c in self.checks:
            md.append(f"| {c['status']} | {c['name']} | {str(c['detail']).replace('|', '/')} |")
        for n in self.notes:
            md.append(f"\n> {n}")
        base.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
        verdict = "FAILED" if self.n_fail else "PASSED"
        print(f"\n=== {self.suite}: {verdict}  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
        print(f"Evidence: {base.with_suffix('.md')}", flush=True)
        return 1 if self.n_fail else 0


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------
def make_parser(desc: str, extra: Callable = None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--start", type=int, default=1, help="first investor index")
    p.add_argument("--end", type=int, default=min(S.INVESTOR_COUNT, 10), help="last investor index")
    p.add_argument("--instance", default=S.DEFAULT_INSTANCE, choices=["paper", "real"])
    p.add_argument("--lot", type=float, default=0.05, help="manager order size")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--ramp", default="5,10,25")
    p.add_argument("--i-understand-real-money", action="store_true", dest="real_ok")
    if extra:
        extra(p)
    return p


def guard_instance(args) -> None:
    if args.instance != "paper" and not args.real_ok:
        print("[FAIL] Refusing to run on a non-paper instance without --i-understand-real-money")
        sys.exit(2)


# ---------------------------------------------------------------------------------------
# PAMM discovery + helpers
# ---------------------------------------------------------------------------------------
class Ctx:
    """Everything a runner needs: client, manager, master id, investor list."""

    def __init__(self, args):
        self.args = args
        self.instance = args.instance
        self.c = PammClient(max_workers=max(S.MAX_THREADS, 10))
        self.mgr = S.MANAGER_EMAIL
        self.investors = [S.investor_email(i) for i in range(args.start, args.end + 1)]
        self.master_id: Optional[str] = None
        self.share_percent: Optional[float] = None

    def discover(self, ev: Evidence) -> bool:
        r = self.c.follows(self.mgr, self.instance)
        m = r.get("master") if r.ok else None
        if not m:
            ev.failed("manager is a PAMM master", f"HTTP {r.status} {r.error or 'no master profile on this account'}")
            return False
        self.master_id = m["client_id"]
        self.share_percent = m.get("share_percent")
        ev.info("PAMM master", f"'{m.get('display_name')}' share={self.share_percent}% pool={r.get('pool_total')} "
                               f"followers={len(r.get('pool') or [])}")
        return True

    def pool(self) -> List[dict]:
        r = self.c.follows(self.mgr, self.instance)
        return [p for p in (r.get("pool") or []) if p.get("status") == "active"]

    def pool_by_investor(self) -> Dict[str, dict]:
        return {p["follower_client_id"]: p for p in self.pool()}

    def subs(self, email: str) -> List[dict]:
        r = self.c.follows(email, self.instance)
        return [s for s in (r.get("subscriptions") or []) if s.get("master_client_id") == self.master_id]

    def active_sub(self, email: str) -> Optional[dict]:
        for s in self.subs(email):
            if s.get("status") == "active":
                return s
        return None

    def max_alloc_id(self) -> int:
        rows = self.c.allocations(self.mgr, role="master", limit=1, instance=self.instance).get("allocations") or []
        return max((a["alloc_id"] for a in rows), default=0)

    def alloc_rows_after(self, after_id: int) -> List[dict]:
        """All master-side allocation rows newer than after_id. The endpoint caps limit at 500 (a larger
        value silently resets to 50), so page with offset until we reach rows we have already seen."""
        out: List[dict] = []
        offset = 0
        while offset < 20000:
            r = self.c.oms(self.mgr, "GET", "/pamm/allocations", self.instance,
                           params={"role": "master", "limit": 500, "offset": offset})
            rows = r.get("allocations") or []
            out.extend(a for a in rows if a["alloc_id"] > after_id)
            if len(rows) < 500 or min(a["alloc_id"] for a in rows) <= after_id:
                break
            offset += 500
        return out

    def trade_and_settle(self, ev: Evidence, *a, **kw) -> Optional[dict]:
        with manager_lock():
            return self._trade_and_settle(ev, *a, **kw)

    def _trade_and_settle(self, ev: Evidence, side: str = "BUY", qty: float = None, want_sign: int = 0,
                         min_abs_pnl: float = 0.5, max_wait: int = 45, settle_wait: int = 30,
                         during: Callable = None) -> Optional[dict]:
        """Manager opens a trade, waits (adaptively) for a non-trivial PnL of the wanted sign
        (+1 win / -1 loss / 0 any), closes it, and waits for the allocation rows to settle.
        Returns {order, realized_pnl, trade_id, rows, baseline, price_tape} or None."""
        qty = qty or self.args.lot
        base = self.max_alloc_id()
        o = self.c.place_order(self.mgr, side, qty, instance=self.instance)
        if not o.ok:
            ev.failed("manager order accepted", f"HTTP {o.status} {o.error}")
            return None
        mid = (o.get("order") or {}).get("market_order_id")
        during_result = during() if during else None
        tape = []
        t0 = time.time()
        while time.time() - t0 < max_wait:
            time.sleep(1.0)
            pos = [p for p in (self.c.positions(self.mgr, self.instance).get("positions") or [])
                   if p.get("market_order_id") == mid]
            if not pos:
                break
            pnl = float(pos[0].get("unrealized_pnl") or 0)
            tape.append(round(pnl, 4))
            if abs(pnl) >= min_abs_pnl and (want_sign == 0 or (pnl > 0) == (want_sign > 0)):
                break
        pool_at_close = self.pool_by_investor()          # who should be allocated at close
        sq = self.c.squareoff(self.mgr, mid, self.instance)
        if not sq.ok:
            ev.failed("manager squareoff accepted", f"HTTP {sq.status} {sq.error}")
            return None
        realized = float(sq.get("realized_pnl") or 0)
        must, dust = split_dust(pool_at_close, realized)
        expected = len(must)
        rows: List[dict] = []
        t1 = time.time()
        while time.time() - t1 < settle_wait:
            time.sleep(1.5)
            rows = self.alloc_rows_after(base)
            if abs(realized) > 0 and len({r["follower"] for r in rows}) >= expected:
                break
            if abs(realized) == 0 and time.time() - t1 > 6:
                break                                     # zero-PnL trades may legitimately create no rows
        trade_ids = sorted({r["trade_id"] for r in rows})
        return {"market_order_id": mid, "realized_pnl": realized, "rows": rows, "baseline": base,
                "trade_ids": trade_ids, "expected": expected, "pool": pool_at_close, "must": must, "dust": dust, "during": during_result, "tape": tape[-12:], "settle_lag_s": round(time.time() - t1, 1)}

    def foreign_positions(self) -> List[dict]:
        """Open manager positions that this suite did not open (they block investors from leaving)."""
        return self.c.positions(self.mgr, self.instance).get("positions") or []


DUST = 0.005          # the platform records no allocation row when an investor's share rounds to $0.00


def split_dust(pool: Dict[str, dict], pnl: float, margin: float = 0.0004):
    """Split pool members into (must be allocated, sub-cent dust). Borderline shares are ignored by both."""
    total = sum(p["deposit"] for p in pool.values()) or 1.0
    must, dust = set(), set()
    for fid, p in pool.items():
        share = abs(pnl * p["deposit"] / total)
        if share >= DUST + margin:
            must.add(fid)
        elif share < DUST - margin:
            dust.add(fid)
    return must, dust


def run_parallel(fn: Callable, items: list, workers: int = None) -> list:
    workers = workers or min(S.MAX_THREADS, 25)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def main_wrapper(name: str, desc: str, body: Callable, extra: Callable = None) -> None:
    args = make_parser(desc, extra).parse_args()
    guard_instance(args)
    ev = Evidence(name, args.instance)
    ctx = Ctx(args)
    print(f"=== {name} | {S.BASE_URL} | instance={args.instance} | investors {args.start}..{args.end} ===", flush=True)
    try:
        if ctx.discover(ev):
            body(ctx, ev)
    except KeyboardInterrupt:
        ev.failed("run interrupted", "stopped by user")
    except Exception as e:  # a crashed scenario is a failure, never a silent pass
        import traceback
        ev.failed("runner crashed", f"{type(e).__name__}: {e}")
        traceback.print_exc()
    sys.exit(ev.finish())


def fresh_member(ctx: "Ctx", ev: Evidence, tag: str, balance: float = 10000.0) -> Optional[str]:
    """Create a brand-new investor via the admin API (so destructive/race tests never touch the shared
    investors) and fund it. Returns its email, or None (and records a failure) if it cannot be created."""
    email = f"pammtest{tag}{int(time.time()) % 100000000}@{S.INVESTOR_DOMAIN}"
    try:
        r = ctx.c.admin_create_member(email, S.INVESTOR_PASSWORD, name="PAMM Test")
    except Exception as e:
        ev.failed("create test member", f"admin login/create failed: {e}")
        return None
    mid = ((r.data or {}).get("member") or {}).get("member_id") if r.ok else None
    if not mid:
        ev.failed("create test member", f"HTTP {r.status} {r.error}")
        return None
    b = ctx.c.admin_set_balance(mid, balance, ctx.instance)
    if not b.ok:
        ev.failed("fund test member", f"HTTP {b.status} {b.error}")
        return None
    ev.info("created test investor", f"{email} balance={balance}")
    return email
