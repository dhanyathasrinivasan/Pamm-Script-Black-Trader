#!/usr/bin/env python3
"""
Suite: Join / leave lifecycle, idempotency and mid-trade membership changes.

Uses ONE investor (the last in --start..--end) and restores it to its original state in a finally block.
Checks: unfollow semantics, idempotent join (same operation_key), no double membership, joining while a
manager trade is open (no dilution: percentages still sum to 100%), leaving while a trade is open.
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.common import Ctx, Evidence, main_wrapper  # noqa: E402


def funds(ctx: Ctx, email: str) -> dict:
    return ctx.c.positions(email, ctx.instance).get("funds") or {}


def active_count(ctx: Ctx, email: str) -> int:
    return sum(1 for s in ctx.subs(email) if s.get("status") == "active")


def body(ctx: Ctx, ev: Evidence):
    inv = ctx.investors[-1]
    sub0 = ctx.active_sub(inv)
    deposit = float(sub0["deposit"]) if sub0 else 500.0
    ev.info("test investor", f"{inv} originally {'active deposit=' + str(deposit) if sub0 else 'NOT in pool'}")
    others = ctx.foreign_positions()
    if others and ctx.args.close_manager_positions:
        n = ctx.c.squareoff_all(ctx.mgr, ctx.instance)
        ev.info("closed pre-existing manager positions (opt-in flag)", f"{n} closed")
        others = ctx.foreign_positions()
    if others:
        # Platform rule under test: investors cannot leave while the manager has open trades.
        ev.info("manager has open positions", f"{len(others)} open - lifecycle checks need a flat manager "
                "(re-run with --close-manager-positions to let the suite close them)")
        r = ctx.c.unfollow(inv, ctx.master_id, ctx.instance)
        ev.check(r.status == 400 and "open trades" in r.error, "leave is refused while the manager has open trades",
                 f"HTTP {r.status} '{r.error}'")
        ev.check(active_count(ctx, inv) == 1 if sub0 else True, "refused leave left the subscription active",
                 f"active={active_count(ctx, inv)}")
        r = ctx.c.join(inv, ctx.master_id, deposit, f"mem_{uuid.uuid4().hex[:10]}", ctx.instance)
        ev.check(not r.ok and r.status < 500, "join while already active is rejected", f"HTTP {r.status} '{r.error}'")
        ev.check(active_count(ctx, inv) <= 1, "no duplicate membership created", f"active={active_count(ctx, inv)}")
        ev.skip("leave/rejoin/mid-trade lifecycle", "blocked by the manager's pre-existing open positions")
        return
    try:
        # ---- A: leave ----------------------------------------------------------------
        if sub0:
            bal0 = funds(ctx, inv).get("balance")
            r = ctx.c.unfollow(inv, ctx.master_id, ctx.instance)
            ev.check(r.ok, "unfollow accepted", f"HTTP {r.status} {r.error} body={str(r.data)[:160]}")
            ev.check(active_count(ctx, inv) == 0, "after unfollow: no active subscription", f"active={active_count(ctx, inv)}")
            bal1 = funds(ctx, inv).get("balance")
            ev.info("balance around unfollow", f"before={bal0} after={bal1} (deposit was {deposit})")
            r2 = ctx.c.unfollow(inv, ctx.master_id, ctx.instance)
            ev.check(r2.status < 500, "second unfollow is harmless (no 5xx)", f"HTTP {r2.status} {r2.error}")
            ev.check(active_count(ctx, inv) == 0, "second unfollow did not change state", f"active={active_count(ctx, inv)}")

        # ---- B/C/D: join + idempotency ---------------------------------------------------
        pool_before = sum(p["deposit"] for p in ctx.pool())
        key = f"mem_{uuid.uuid4().hex[:10]}"
        r = ctx.c.join(inv, ctx.master_id, deposit, key, ctx.instance)
        ev.check(r.ok, "join accepted", f"HTTP {r.status} {r.error} body={str(r.data)[:200]}")
        ev.check(active_count(ctx, inv) == 1, "exactly one active subscription after join", f"active={active_count(ctx, inv)}")
        pool_after = sum(p["deposit"] for p in ctx.pool())
        ev.check(abs((pool_after - pool_before) - deposit) < 0.01, "pool total grew by exactly the deposit",
                 f"before={pool_before} after={pool_after} deposit={deposit}")
        r_dup = ctx.c.join(inv, ctx.master_id, deposit, key, ctx.instance)
        ev.check(r_dup.status < 500, "duplicate join (same operation_key) -> no 5xx", f"HTTP {r_dup.status} {r_dup.error}")
        ev.check(active_count(ctx, inv) == 1, "duplicate join did not create a 2nd membership", f"active={active_count(ctx, inv)}")
        pool_dup = sum(p["deposit"] for p in ctx.pool())
        ev.check(abs(pool_dup - pool_after) < 0.01, "duplicate join did not change pool total", f"{pool_after} -> {pool_dup}")
        r_new = ctx.c.join(inv, ctx.master_id, deposit, f"mem_{uuid.uuid4().hex[:10]}", ctx.instance)
        ev.check(not r_new.ok, "join while already active (new key) is rejected", f"HTTP {r_new.status} {r_new.error}")
        ev.check(active_count(ctx, inv) == 1, "still exactly one active subscription", f"active={active_count(ctx, inv)}")

        # ---- E: leave then join WHILE a manager trade is open --------------------------------
        ctx.c.unfollow(inv, ctx.master_id, ctx.instance)

        def join_mid_trade():
            return ctx.c.join(inv, ctx.master_id, deposit, f"mid_{uuid.uuid4().hex[:10]}", ctx.instance)

        res = ctx.trade_and_settle(ev, want_sign=0, min_abs_pnl=0.5, during=join_mid_trade)
        if res:
            jr = res["during"]
            ev.check(jr is not None and jr.ok, "join while manager trade is open accepted", f"HTTP {getattr(jr, 'status', None)}")
            rows = res["rows"]
            if rows and abs(res["realized_pnl"]) > 0:
                mine = [r for r in rows if r["follower"] == (ctx.active_sub(inv) or {}).get("follower_client_id")]
                pct_sum = sum(r["percentage"] for r in rows)
                ev.check(abs(pct_sum - 100) < 0.05, "mid-trade join: percentages still sum to 100% (no dilution/inflation)", f"sum={pct_sum:.5f}")
                ev.info("late joiner allocation", f"joined while position open; received {len(mine)} row(s) for the trade "
                        f"(shared_pnl={[r['shared_pnl'] for r in mine]}) - platform rule to confirm: allocation at close time")
            else:
                ev.skip("mid-trade join allocation", f"no PnL/rows (realized={res['realized_pnl']})")

        # ---- F: leave while a trade is open ---------------------------------------------------
        def leave_mid_trade():
            return ctx.c.unfollow(inv, ctx.master_id, ctx.instance)

        fid = (ctx.active_sub(inv) or {}).get("follower_client_id")
        res = ctx.trade_and_settle(ev, want_sign=0, min_abs_pnl=0.5, during=leave_mid_trade)
        if res:
            lr = res["during"]
            ev.check(lr is not None and lr.ok, "leave while manager trade is open accepted", f"HTTP {getattr(lr, 'status', None)}")
            if res["rows"]:
                got = [r for r in res["rows"] if r["follower"] == fid]
                ev.check(len(got) == 0, "investor who left before close gets no allocation", f"rows for leaver={len(got)}")
                pct_sum = sum(r["percentage"] for r in res["rows"])
                ev.check(abs(pct_sum - 100) < 0.05, "after mid-trade leave: percentages still sum to 100%", f"sum={pct_sum:.5f}")
            else:
                ev.skip("mid-trade leave allocation", "no rows (zero PnL)")
            bal = funds(ctx, inv)
            ev.check(float(bal.get("balance", 0)) >= 0, "leaver balance not negative", f"balance={bal.get('balance')}")

        # ---- G: rejoin ----------------------------------------------------------------------
        r = ctx.c.join(inv, ctx.master_id, deposit, f"re_{uuid.uuid4().hex[:10]}", ctx.instance)
        ev.check(r.ok, "re-join after leaving works", f"HTTP {r.status} {r.error}")
    finally:
        # restore: investor must end in its original state
        cur = ctx.active_sub(inv)
        if sub0 and not cur:
            ctx.c.join(inv, ctx.master_id, deposit, f"restore_{uuid.uuid4().hex[:10]}", ctx.instance)
        if not sub0 and cur:
            ctx.c.unfollow(inv, ctx.master_id, ctx.instance)
        ev.info("restored", f"{inv} active={bool(ctx.active_sub(inv))} (originally {bool(sub0)})")


if __name__ == "__main__":
    main_wrapper("membership", "PAMM join/leave lifecycle", body,
                 extra=lambda p: p.add_argument("--close-manager-positions", action="store_true",
                                                help="close ALL open manager positions first (needed for leave tests)"))
