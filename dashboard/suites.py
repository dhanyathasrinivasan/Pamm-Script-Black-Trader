"""
Suite registry - the single source of truth for what the dashboard can launch.

To add a suite: write a runner in test_suite/ (use test_suite/common.py), then add ONE entry to SUITES.
The launcher UI, the run options and the command line are all generated from it.

Fields
------
key        unique id used by the API
name, desc, category, icon, tone (ok|cyan|warn|danger|violet), tag   -- presentation
script     path relative to the repo root (None for inline suites)
flags      CLI flags the runner understands: start, end, instance, lot, ramp, concurrency, ops, seed
extra      fixed extra argv appended to the command (e.g. an opt-in flag)
risk       safe        - paper trading only
           real        - touches the REAL instance (typed confirmation: REAL)
           destructive - closes positions / changes shared state (typed confirmation: TEARDOWN)
fixed_instance  force an instance regardless of the selector
minutes    rough duration shown to the user
"""

import sys
from typing import Any, Dict, List, Optional

PY = sys.executable or "python"

SUITES: List[Dict[str, Any]] = [
    # ---- Core correctness (model-checked) ---------------------------------------------
    dict(key="allocation", name="Allocation Sizing", desc="Win and loss trade: every investor gets exactly deposit / pool_total of the PnL",
         category="Core correctness", icon="chart-pie", tone="ok", tag="Pro-rata", script="test_suite/allocation_runner.py",
         flags=["instance", "lot"], risk="safe", minutes=2),
    dict(key="fees", name="Fees & Money Conservation", desc="Performance share on wins, full loss on losses, running_pnl and wallet reconciliation",
         category="Core correctness", icon="scale-balanced", tone="warn", tag="Fees", script="test_suite/fees_runner.py",
         flags=["start", "end", "instance", "lot"], risk="safe", minutes=4),
    dict(key="random_walk", name="Seeded Random Walk", desc="Random manager trades, each model-checked; reproducible with --seed",
         category="Core correctness", icon="shuffle", tone="violet", tag="Seeded", script="test_suite/random_walk_runner.py",
         flags=["instance", "ops", "seed"], risk="safe", minutes=4),
    dict(key="accounting", name="Accounting Audit", desc="Read-only: pool totals, duplicate rows, running_pnl vs allocation history",
         category="Core correctness", icon="calculator", tone="cyan", tag="Read-only", script="test_suite/accounting_runner.py",
         flags=["start", "end", "instance"], risk="safe", minutes=1),

    # ---- Membership & risk -------------------------------------------------------------
    dict(key="membership", name="Join / Leave Rules", desc="Leave blocked by open trades, join idempotency, no double membership",
         category="Membership & risk", icon="user-plus", tone="ok", tag="Rules", script="test_suite/membership_runner.py",
         flags=["start", "end", "instance", "lot"], risk="safe", minutes=1),
    dict(key="membership_full", name="Join / Leave Lifecycle", desc="Full mid-trade join and leave. CLOSES all open manager positions first",
         category="Membership & risk", icon="right-left", tone="danger", tag="Closes positions", script="test_suite/membership_runner.py",
         flags=["start", "end", "instance", "lot"], extra=["--close-manager-positions"], risk="destructive", minutes=3),
    dict(key="risk", name="Risk Limits & Deposits", desc="Over-wallet, zero and negative deposits; wallet debit and refund; minimum deposit",
         category="Membership & risk", icon="shield-halved", tone="cyan", tag="Limits", script="test_suite/risk_runner.py",
         flags=["instance"], risk="safe", minutes=1),

    # ---- Breakpoints --------------------------------------------------------------------
    dict(key="breakpoint", name="Breakpoint & Race Hunter", desc="Fuzz, concurrent joins/unfollows, order burst ramp, final audit",
         category="Breakpoints", icon="burst", tone="danger", tag="Tries to break it", script="test_suite/breakpoint_runner.py",
         flags=["instance", "lot", "ramp", "concurrency"], risk="safe", minutes=5),

    # ---- Maintenance ---------------------------------------------------------------------
    dict(key="teardown", name="Emergency Teardown", desc="Close every open position of the PAMM manager",
         category="Maintenance", icon="trash-can", tone="danger", tag="Clean up", script=None,
         flags=["instance"], risk="destructive", fixed_instance="paper", minutes=1),
]

SUITE_INDEX: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SUITES}


def public_registry() -> List[Dict[str, Any]]:
    return [dict(s) for s in SUITES]


def _int(v, default: int, lo: int = 1, hi: int = 100000) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _num(v, default: float, hi: float = 100.0) -> float:
    try:
        f = float(v)
        return min(f, hi) if f > 0 else default
    except (TypeError, ValueError):
        return default


def build_command(suite: Dict[str, Any], params: Dict[str, Any], defaults: Dict[str, Any]) -> List[str]:
    """Turn user-chosen params into a validated argv list. Never uses a shell."""
    start = _int(params.get("start"), 1)
    end = _int(params.get("end"), min(defaults["investor_count"], 10))
    if end < start:
        start, end = end, start
    instance = suite.get("fixed_instance") or (params.get("instance") if params.get("instance") in ("paper", "real") else "paper")

    if suite["key"] == "teardown":
        code = (
            "from test_suite.client import PammClient; from config import settings as S; "
            "c = PammClient(); n = c.squareoff_all(S.MANAGER_EMAIL, 'paper'); "
            "print('[PASS] manager positions closed:', n)"
        )
        return [PY, "-u", "-c", code]

    cmd = [PY, "-u", suite["script"]]
    flags = suite["flags"]
    if "start" in flags:
        cmd += ["--start", str(start)]
    if "end" in flags:
        cmd += ["--end", str(end)]
    if "instance" in flags:
        cmd += ["--instance", instance]
        if instance == "real":
            cmd += ["--i-understand-real-money"]
    if "lot" in flags:
        cmd += ["--lot", str(_num(params.get("lot"), 0.5))]
    if "ramp" in flags:
        raw = str(params.get("ramp") or "5,10,25")
        levels = [str(_int(x, 5, 1, 200)) for x in raw.split(",") if x.strip()][:8]
        cmd += ["--ramp", ",".join(levels) or "5,10,25"]
    if "concurrency" in flags:
        cmd += ["--concurrency", str(_int(params.get("concurrency"), 10, 2, 100))]
    if "ops" in flags:
        cmd += ["--ops", str(_int(params.get("ops"), 4, 1, 50))]
    if "seed" in flags and params.get("seed") not in (None, ""):
        cmd += ["--seed", str(_int(params.get("seed"), 1, 0, 10**9))]
    cmd += list(suite.get("extra") or [])
    return cmd


def effective_instance(suite: Dict[str, Any], params: Dict[str, Any]) -> Optional[str]:
    return suite.get("fixed_instance") or params.get("instance") or "paper"


def needs_confirmation(suite: Dict[str, Any], params: Dict[str, Any]) -> bool:
    return suite["risk"] in ("real", "destructive") or effective_instance(suite, params) in ("real", "both")
