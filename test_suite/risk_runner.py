#!/usr/bin/env python3
"""
Suite: Risk limits & deposit rules. Uses brand-new members, funded with a small wallet, so the shared investors
are never touched. Each member that joins is removed again (when the platform allows it).

  * deposit larger than the wallet balance is rejected and the wallet is unchanged
  * deposit == 0 / negative is rejected
  * wallet never goes negative; after join + leave the wallet is made whole
  * minimum deposit: probes 1 and 10 and reports what the platform accepts (measurement)
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.common import Ctx, Evidence, fresh_member, main_wrapper  # noqa: E402


def wallet(ctx, email):
    return float(ctx.c.positions(email, ctx.instance).get("funds", {}).get("balance", 0))


def active(ctx, email):
    return sum(1 for s in ctx.subs(email) if s.get("status") == "active")


def body(ctx: Ctx, ev: Evidence):
    w = fresh_member(ctx, ev, "rk", balance=200.0)
    if not w:
        return
    b0 = wallet(ctx, w)
    for dep in (1000.0, 200.01, 1e9):
        r = ctx.c.join(w, ctx.master_id, dep, f"rk_{uuid.uuid4().hex[:8]}", ctx.instance)
        ev.check(not r.ok and r.status < 500 and active(ctx, w) == 0, f"deposit {dep} exceeds wallet 200 -> rejected, no membership",
                 f"HTTP {r.status} '{r.error}' active={active(ctx, w)}", sev="HIGH")
    for dep in (0, -50):
        r = ctx.c.join(w, ctx.master_id, dep, f"rk_{uuid.uuid4().hex[:8]}", ctx.instance)
        ev.check(not r.ok and r.status < 500 and active(ctx, w) == 0, f"deposit {dep} rejected", f"HTTP {r.status} '{r.error}'", sev="HIGH")
    ev.check(abs(wallet(ctx, w) - b0) < 0.01, "rejected joins left the wallet untouched", f"{b0} -> {wallet(ctx, w)}", sev="HIGH")

    for dep in (1.0, 10.0, 100.0):                       # minimum-deposit measurement (stops at the first accepted)
        r = ctx.c.join(w, ctx.master_id, dep, f"rk_{uuid.uuid4().hex[:8]}", ctx.instance)
        if r.ok:
            ev.info("minimum deposit", f"smallest accepted among 1/10/100 = {dep}")
            w_after = wallet(ctx, w)
            ev.check(abs((b0 - w_after) - dep) < 0.01, "join debits the wallet by exactly the deposit", f"{b0} -> {w_after} (deposit {dep})", sev="HIGH")
            lv = ctx.c.unfollow(w, ctx.master_id, ctx.instance)
            ev.info("leave after join", f"HTTP {lv.status} {lv.error}")
            if lv.ok:
                ev.check(abs(wallet(ctx, w) - b0) < 0.01, "join + leave with no trades returns the wallet to its start",
                         f"{b0} -> {wallet(ctx, w)}", sev="HIGH")
            break
        ev.info(f"deposit {dep}", f"rejected: HTTP {r.status} '{r.error}'")
    else:
        ev.skip("minimum deposit", "1, 10 and 100 were all rejected")
    ev.check(wallet(ctx, w) >= 0, "wallet never negative", f"balance={wallet(ctx, w)}", sev="HIGH")


if __name__ == "__main__":
    main_wrapper("risk", "PAMM risk limits & deposit rules", body)
