#!/usr/bin/env python3
"""
Suite: Fees & profit sharing + money conservation.

Documented rule (README): on a winning trade the manager earns a performance share (configured on the master,
e.g. 20%) and investors keep the rest; on a loss the manager absorbs nothing and investors share the full loss.
This suite measures what the platform actually credits and compares it to that rule, printing every number.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.common import TOL, Ctx, Evidence, main_wrapper  # noqa: E402


def snapshot(ctx: Ctx, emails):
    out = {}
    for e in emails:
        sub = ctx.active_sub(e) or {}
        out[e] = {"running": float(sub.get("running_pnl") or 0), "fid": sub.get("follower_client_id"),
                  "balance": ctx.c.positions(e, ctx.instance).get("funds", {}).get("balance")}
    return out


def body(ctx: Ctx, ev: Evidence):
    sample = ctx.investors[: min(5, len(ctx.investors))]
    share = (ctx.share_percent or 0) / 100.0
    got_win = got_loss = False
    for attempt in range(1, 7):
        if got_win and got_loss:
            break
        want = +1 if not got_win else -1
        side = "BUY" if attempt % 2 else "SELL"
        before = snapshot(ctx, sample)
        mgr_bal0 = ctx.c.positions(ctx.mgr, ctx.instance).get("funds", {}).get("balance")
        res = ctx.trade_and_settle(ev, side=side, want_sign=want, min_abs_pnl=1.0)
        if not res or not res["rows"] or abs(res["realized_pnl"]) < 0.5:
            ev.info(f"attempt {attempt}", "no usable PnL (quiet market) - retrying")
            continue
        pnl = res["rows"][0]["order_pnl"]
        win = pnl > 0
        if (win and got_win) or (not win and got_loss):
            continue
        label = "WIN" if win else "LOSS"
        got_win, got_loss = got_win or win, got_loss or not win
        rows = res["rows"]
        total_shared = sum(r["shared_pnl"] for r in rows)
        ratio = total_shared / pnl
        expected_ratio = (1 - share) if win else 1.0
        rounding = 0.005 * len(rows) + 0.01          # each investor's share is rounded to a cent
        ev.check(abs(total_shared - pnl * expected_ratio) <= rounding, f"{label} {pnl:+.4f}: investors' share of PnL matches the documented rule",
                 f"sum(shared_pnl)={total_shared:+.4f} vs expected {pnl * expected_ratio:+.4f} (ratio {ratio:.4f}, expected {expected_ratio:.2f}; "
                 f"manager share {share:.0%} on profit, 0% on loss; rounding allowance +/-{rounding:.2f})")
        after = snapshot(ctx, sample)
        by_fid = {r["follower"]: r for r in rows}
        for e in sample:
            fid = before[e]["fid"]
            r = by_fid.get(fid)
            if not r:
                dep_pct = (ctx.active_sub(e) or {}).get("deposit", 0) / max(sum(p["deposit"] for p in ctx.pool()), 1)
                if abs(pnl * dep_pct) < 0.005:
                    ev.info(f"{label}: {e} no allocation row", f"share {pnl * dep_pct:+.5f} rounds to $0.00 (rounding dust is not recorded)")
                else:
                    ev.failed(f"{label}: {e} allocated", f"no row, expected share {pnl * dep_pct:+.4f}")
                continue
            d_run = after[e]["running"] - before[e]["running"]
            ev.check(abs(d_run - r["shared_pnl"]) <= TOL, f"{label}: {e} running_pnl moved by shared_pnl",
                     f"running {before[e]['running']:+.2f}->{after[e]['running']:+.2f} (delta {d_run:+.4f}) vs shared_pnl {r['shared_pnl']:+.4f}")
            ev.info(f"{label}: {e} wallet balance", f"{before[e]['balance']} -> {after[e]['balance']} (is PnL credited to the wallet or only tracked as running_pnl?)")
        mgr_bal1 = ctx.c.positions(ctx.mgr, ctx.instance).get("funds", {}).get("balance")
        ev.info(f"{label}: manager wallet balance", f"{mgr_bal0} -> {mgr_bal1} (delta {float(mgr_bal1) - float(mgr_bal0):+.4f}; trade realized {res['realized_pnl']:+.4f})")
        # conservation: investors' shares + remainder must equal the trade PnL within rounding
        remainder = pnl - total_shared
        ev.check(abs(remainder - (pnl * share if win else 0)) <= rounding,
                 f"{label}: conservation - PnL not paid to investors equals the manager's share",
                 f"order_pnl={pnl:+.4f} - sum(shared)={total_shared:+.4f} = remainder {remainder:+.4f}; manager share expected "
                 f"{pnl * share if win else 0:+.4f}")
    if not (got_win and got_loss):
        ev.skip("both a winning and a losing trade were observed", f"win={got_win} loss={got_loss}: market too quiet; re-run")


if __name__ == "__main__":
    main_wrapper("fees", "PAMM fees, profit sharing, money conservation", body)
