#!/usr/bin/env python3
"""
Suite: Accounting audit (read-only). Run it any time; it never trades.

  * pool_total == sum of active deposits, and equals the 'fund' shown in the public masters list
  * no duplicate (trade, investor) allocation rows, no negative wallets
  * per trade: sum(percentage of allocated + dust investors) == 100%, sum(shared_pnl) ~= order_pnl
  * per investor: subscription running_pnl == sum(shared_pnl) of that investor's allocation rows
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.common import Ctx, Evidence, main_wrapper, run_parallel  # noqa: E402


def body(ctx: Ctx, ev: Evidence):
    f = ctx.c.follows(ctx.mgr, ctx.instance)
    pool = [p for p in (f.get("pool") or []) if p.get("status") == "active"]
    total = sum(p["deposit"] for p in pool)
    ev.check(abs(total - float(f.get("pool_total") or 0)) < 0.01, "pool_total equals sum of active deposits",
             f"sum={total} pool_total={f.get('pool_total')}")
    m = next((x for x in (ctx.c.masters(ctx.mgr, ctx.instance).get("masters") or []) if x["client_id"] == ctx.master_id), None)
    if m:
        ev.check(abs(float(m["stats"]["fund"]) - total) < 0.01, "masters list 'fund' equals the pool total", f"fund={m['stats']['fund']} pool={total}")
        ev.check(int(m["stats"]["followers"]) == len(pool), "masters list follower count equals active pool members",
                 f"stats.followers={m['stats']['followers']} active={len(pool)}")
    ev.check(len({p["follower_client_id"] for p in pool}) == len(pool), "no duplicate pool membership", f"{len(pool)} members")

    rows = []
    off = 0
    while off < 5000:
        r = ctx.c.oms(ctx.mgr, "GET", "/pamm/allocations", ctx.instance, params={"role": "master", "limit": 500, "offset": off})
        got = r.get("allocations") or []
        rows += got
        if len(got) < 500:
            break
        off += 500
    ev.info("allocation rows read", f"{len(rows)}")
    pairs = [(r["trade_id"], r["follower"]) for r in rows]
    ev.check(len(pairs) == len(set(pairs)), "no duplicate (trade, investor) allocation rows", f"rows={len(rows)} unique={len(set(pairs))}", sev="HIGH")

    by_trade = defaultdict(list)
    for r in rows:
        by_trade[r["trade_id"]].append(r)
    over, off_sum = [], []
    for t, rs in by_trade.items():
        ps = sum(r["percentage"] for r in rs)
        if ps > 100.05:
            over.append((t, round(ps, 3)))
        pnl = rs[0]["order_pnl"]
        if abs(sum(r["shared_pnl"] for r in rs) - pnl) > 0.005 * len(rs) + 0.01 and abs(pnl) > 0:
            off_sum.append((t, round(sum(r["shared_pnl"] for r in rs), 4), round(pnl, 4)))
    ev.check(not over, "no trade allocates more than 100% of the pool", f"{len(by_trade)} trades checked; over={over[:5]}", sev="HIGH")
    ev.check(not off_sum, "per trade: sum(shared_pnl) equals order_pnl within cent rounding", f"{len(by_trade)} trades; off={off_sum[:5]}", sev="MED")

    sample = ctx.investors

    def audit(email):
        try:
            sub = ctx.active_sub(email) or {}
            als, off2 = [], 0
            while off2 < 5000:
                g = ctx.c.oms(email, "GET", "/pamm/allocations", ctx.instance, params={"limit": 500, "offset": off2}).get("allocations") or []
                als += [a for a in g if a.get("master") == ctx.master_id]
                if len(g) < 500:
                    break
                off2 += 500
            funds = ctx.c.positions(email, ctx.instance).get("funds", {})
            return email, sub, als, funds
        except Exception as e:
            return email, None, [], {"error": str(e)}

    bad_run, neg = [], []
    for email, sub, als, funds in run_parallel(audit, sample):
        if not sub:
            continue
        s = round(sum(a["shared_pnl"] for a in als), 2)
        if abs(s - float(sub.get("running_pnl") or 0)) > 0.011:
            bad_run.append(f"{email}: running_pnl={sub.get('running_pnl')} sum(shared)={s} rows={len(als)}")
        if float(funds.get("balance", 0) or 0) < 0:
            neg.append(email)
    ev.check(not bad_run, f"running_pnl == sum(shared_pnl) for {len(sample)} investors", "; ".join(bad_run[:3]) or "all consistent", sev="HIGH")
    ev.check(not neg, "no investor wallet is negative", f"negative={neg}", sev="HIGH")
    pos = ctx.foreign_positions()
    ev.info("manager open positions", f"{len(pos)} open (these block investors from leaving the pool)")


if __name__ == "__main__":
    main_wrapper("accounting", "PAMM accounting audit (read-only)", body)
