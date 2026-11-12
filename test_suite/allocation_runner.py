#!/usr/bin/env python3
"""
Suite: Allocation sizing (pro-rata by investor deposit share) - model-checked.

For a winning and a losing manager trade, every active investor must receive exactly one allocation row
whose percentage == deposit / pool_total and whose shared_pnl == order_pnl * percentage (rounded to cents).
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.common import TOL, Ctx, Evidence, main_wrapper  # noqa: E402


def verify_trade(ctx: Ctx, ev: Evidence, label: str, res: dict, pool: dict):
    rows = res["rows"]
    if not ev.check(bool(rows), f"{label}: allocation rows were produced",
                    f"realized_pnl={res['realized_pnl']} rows={len(rows)} settle_lag={res['settle_lag_s']}s tape={res['tape']}"):
        return
    tids = res["trade_ids"]
    ev.check(len(tids) == 1, f"{label}: all rows belong to one trade", f"trade_ids={tids}")
    by_inv = Counter(r["follower"] for r in rows)
    dup = {k: v for k, v in by_inv.items() if v > 1}
    ev.check(not dup, f"{label}: no investor allocated twice for the same trade", f"duplicates={dup}" if dup else f"{len(by_inv)} investors, 1 row each")
    must, dust = res["must"], res["dust"]
    missing = must - set(by_inv)
    extra = set(by_inv) - set(pool)
    ev.check(not missing and not extra, f"{label}: every investor whose share >= $0.005 was allocated, nobody outside the pool",
             f"allocated={len(by_inv)} pool={len(pool)} required={len(must)} missing={len(missing)} extra={len(extra)}")
    if dust:
        ev.info(f"{label}: sub-cent shares are not recorded", f"{len(dust & (set(pool) - set(by_inv)))} of {len(dust)} investors whose share rounds to $0.00 got no row "
                f"(platform drops dust: small investors never accumulate PnL on small trades)")
    pool_total = sum(p["deposit"] for p in pool.values())
    absent_pct = sum(100.0 * pool[f]["deposit"] / pool_total for f in pool if f not in by_inv)
    pct_sum = sum(r["percentage"] for r in rows) + absent_pct
    ev.check(abs(pct_sum - 100.0) < 0.05, f"{label}: percentages (incl. dust investors) sum to 100%", f"sum={pct_sum:.6f}")
    bad_pct = bad_pnl = 0
    worst = 0.0
    for r in rows:
        dep = pool.get(r["follower"], {}).get("deposit", r["deposit"])
        exp_pct = 100.0 * dep / pool_total
        if abs(r["percentage"] - exp_pct) > 0.001:
            bad_pct += 1
        exp_pnl = r["order_pnl"] * r["percentage"] / 100.0
        worst = max(worst, abs(r["shared_pnl"] - exp_pnl))
        if abs(r["shared_pnl"] - exp_pnl) > TOL:
            bad_pnl += 1
    ev.check(bad_pct == 0, f"{label}: percentage == deposit / pool_total for every investor", f"violations={bad_pct}/{len(rows)}")
    ev.check(bad_pnl == 0, f"{label}: shared_pnl == order_pnl x percentage (tol ${TOL})",
             f"violations={bad_pnl}/{len(rows)} worst_error=${worst:.4f}")
    total_shared = sum(r["shared_pnl"] for r in rows)
    order_pnl = rows[0]["order_pnl"]
    ev.check(abs(total_shared - order_pnl) < TOL * max(5, len(rows) ** 0.5 * 2), f"{label}: investor shares add up to the trade PnL",
             f"sum(shared_pnl)=${total_shared:.4f} order_pnl=${order_pnl:.4f}")
    ev.info(f"{label}: manager performance share", f"configured={ctx.share_percent}% (expected investor net on profit = "
            f"{100 - (ctx.share_percent or 0):.0f}%). sum(shared)/order_pnl={total_shared / order_pnl:.4f}" if order_pnl else "order_pnl=0")


def body(ctx: Ctx, ev: Evidence):
    pool = ctx.pool_by_investor()
    ev.check(len(pool) > 0, "pool has active investors", f"{len(pool)} active")
    for label, sign in (("WIN", +1), ("LOSS", -1)):
        res = ctx.trade_and_settle(ev, side="BUY", want_sign=sign, min_abs_pnl=1.0)
        if not res:
            continue
        if res["realized_pnl"] == 0:
            ev.skip(f"{label} trade", f"market did not move enough (tape={res['tape']}); nothing to verify")
            continue
        if (res["realized_pnl"] > 0) != (sign > 0):
            ev.info(f"{label} trade", f"realized {res['realized_pnl']:+.2f} (wrong sign for target; still verified)")
        verify_trade(ctx, ev, f"{label} {res['realized_pnl']:+.2f}", res, pool)


if __name__ == "__main__":
    main_wrapper("allocation", "PAMM allocation sizing", body)
