#!/usr/bin/env python3
"""
Suite: Breakpoint & Race Hunter - tries to BREAK the PAMM system.

All join/leave abuse targets brand-new members created through the admin API, never the shared investors.
Every check states an invariant. HIGH = invariant violated (bug) -> FAIL, MED = suspicious -> WARN.

  1. Hostile input fuzz on /pamm/join  -> only 4xx, never 5xx, never a state change
  2. N concurrent joins, SAME operation_key -> credited once
  3. N concurrent joins, DIFFERENT keys     -> exactly one membership, credited once
  4. N concurrent unfollow storm             -> no 5xx, no double refund
  5. Manager order burst ramp                -> every trade allocated exactly once per investor; latency p50/p95
  6. Final consistency audit
"""
import json
import statistics
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings as S  # noqa: E402
from test_suite.common import Ctx, Evidence, fresh_member, main_wrapper, manager_lock, run_parallel, split_dust  # noqa: E402


def pool_total(ctx):
    return sum(p["deposit"] for p in ctx.pool())


def active_subs(ctx, email):
    return sum(1 for s in ctx.subs(email) if s.get("status") == "active")


def wallet(ctx, email):
    return float(ctx.c.positions(email, ctx.instance).get("funds", {}).get("balance", 0))


def fuzz(ctx, ev):
    f = fresh_member(ctx, ev, "fz")
    if not f:
        return
    mid = ctx.master_id
    total0 = pool_total(ctx)
    k = lambda: f"fz_{uuid.uuid4().hex[:10]}"  # noqa: E731
    bodies = []
    for dep in (0, -100, -0.0, "abc", "", None, True, [], {}, "1; DROP TABLE users", 1e308, 99999999999, "NaN"):
        bodies.append((f"deposit={dep!r}", {"master_client_id": mid, "deposit": dep, "operation_key": k()}))
    for m in (None, "", "mem_doesnotexist", "' OR 1=1 --", "<script>alert(1)</script>", "A" * 5000, 12345, [], {}):
        bodies.append((f"master_client_id={str(m)[:30]!r}", {"master_client_id": m, "deposit": 100, "operation_key": k()}))
    bodies.append(("missing master_client_id", {"deposit": 100, "operation_key": k()}))
    bodies.append(("missing deposit", {"master_client_id": mid, "operation_key": k()}))
    bodies.append(("empty object", {}))
    bodies.append(("operation_key=5000 chars", {"master_client_id": mid, "deposit": 0, "operation_key": "K" * 5000}))
    raws = [("deposit=NaN (raw JSON)", b'{"master_client_id":"%s","deposit":NaN,"operation_key":"fz_nan"}' % mid.encode()),
            ("deposit=Infinity (raw JSON)", b'{"master_client_id":"%s","deposit":Infinity,"operation_key":"fz_inf"}' % mid.encode()),
            ("truncated JSON", b'{"master_client_id":'), ("not JSON", b"hello"), ("empty body", b"")]
    five = []
    accepted = []
    for name, b in bodies:
        r = ctx.c.oms(f, "POST", "/pamm/join", ctx.instance, body=b)
        if r.status >= 500 or r.status == 0:
            five.append(f"{name} -> {r.status}")
        elif r.ok:
            accepted.append(name)
    for name, raw in raws:
        r = ctx.c.oms(f, "POST", "/pamm/join", ctx.instance, raw_body=raw)
        if r.status >= 500 or r.status == 0:
            five.append(f"{name} -> {r.status}")
        elif r.ok:
            accepted.append(name)
    n = len(bodies) + len(raws)
    ev.check(not five, f"fuzz: {n} hostile join payloads never cause 5xx", f"5xx/timeouts: {five}" if five else "all rejected with 4xx", sev="HIGH")
    ev.check(not accepted and active_subs(ctx, f) == 0, "fuzz: no hostile payload changed state (no membership created)",
             f"accepted={accepted} active_subs={active_subs(ctx, f)}", sev="HIGH")
    ev.check(abs(pool_total(ctx) - total0) < 0.01, "fuzz: pool total unchanged", f"{total0} -> {pool_total(ctx)}", sev="HIGH")
    for tail in ("/pamm/join", "/pamm/unfollow"):  # unauthenticated / garbage token
        r = ctx.c._send("POST", f"/v1/oms/{ctx.instance}{tail}", token="garbage", body={"master_client_id": mid})
        ev.check(r.status in (400, 401, 403), f"unauthenticated POST {tail} rejected", f"HTTP {r.status}", sev="HIGH")


def race_same_key(ctx, ev, n):
    g = fresh_member(ctx, ev, "rk")
    if not g:
        return None
    dep, key, t0 = 100.0, f"race_{uuid.uuid4().hex[:8]}", pool_total(ctx)
    res = run_parallel(lambda _: ctx.c.join(g, ctx.master_id, dep, key, ctx.instance), range(n), workers=n)
    codes = sorted(r.status for r in res)
    t1 = pool_total(ctx)
    ev.check(active_subs(ctx, g) == 1, f"{n} concurrent joins, same operation_key -> exactly one membership",
             f"statuses={codes} active_subs={active_subs(ctx, g)}", sev="HIGH")
    ev.check(abs((t1 - t0) - dep) < 0.01, "same-key join race: deposit credited once", f"pool {t0} -> {t1} (deposit {dep})", sev="HIGH")
    ev.check(not any(c >= 500 or c == 0 for c in codes), "same-key join race: no 5xx", f"statuses={codes}", sev="HIGH")
    return g


def race_diff_keys(ctx, ev, n):
    g = fresh_member(ctx, ev, "rd")
    if not g:
        return None
    dep, t0 = 100.0, pool_total(ctx)
    res = run_parallel(lambda i: ctx.c.join(g, ctx.master_id, dep, f"rd_{i}_{uuid.uuid4().hex[:8]}", ctx.instance), range(n), workers=n)
    codes = sorted(r.status for r in res)
    t1 = pool_total(ctx)
    ev.check(active_subs(ctx, g) == 1, f"{n} concurrent joins, different keys -> exactly one membership",
             f"statuses={codes} active_subs={active_subs(ctx, g)}", sev="HIGH")
    ev.check(abs((t1 - t0) - dep) < 0.01, "different-key join race: deposit credited once", f"pool {t0} -> {t1} (deposit {dep})", sev="HIGH")
    ev.check(not any(c >= 500 or c == 0 for c in codes), "different-key join race: no 5xx", f"statuses={codes}", sev="HIGH")
    return g


def unfollow_storm(ctx, ev, who, n):
    if not who:
        return
    b0 = wallet(ctx, who)
    res = run_parallel(lambda _: ctx.c.unfollow(who, ctx.master_id, ctx.instance), range(n), workers=n)
    codes = sorted(r.status for r in res)
    ev.check(not any(c >= 500 or c == 0 for c in codes), f"{n} concurrent unfollows: no 5xx", f"statuses={codes}", sev="HIGH")
    ok = sum(1 for r in res if r.ok)
    ev.check(ok <= 1, "concurrent unfollows: processed at most once", f"{ok} accepted", sev="HIGH")
    b1 = wallet(ctx, who)
    ev.check(b1 - b0 <= 100.0 + 0.01, "concurrent unfollows: no multiple refund of the deposit", f"wallet {b0} -> {b1} (deposit was 100)", sev="HIGH")
    ev.check(b1 >= 0, "wallet never negative", f"balance={b1}", sev="HIGH")


def order_burst(ctx, ev, ramp):
    for n in ramp:
        base = ctx.max_alloc_id()
        expected = len(ctx.pool())
        placed = run_parallel(lambda _: ctx.c.place_order(ctx.mgr, "BUY", max(ctx.args.lot, 0.01), instance=ctx.instance), range(n), workers=min(n, S.MAX_THREADS))
        ok = [r for r in placed if r.ok]
        lat = sorted(r.elapsed for r in placed)
        p50, p95 = statistics.median(lat), lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        ev.info(f"burst x{n}", f"accepted={len(ok)}/{n} order latency p50={p50:.2f}s p95={p95:.2f}s")
        ids = [(r.get("order") or {}).get("market_order_id") for r in ok]
        import time
        t0 = time.time()                       # hold until the price moves, otherwise closes are zero-PnL (no allocations)
        while ids and time.time() - t0 < 30:
            time.sleep(1.0)
            mine = [p for p in (ctx.c.positions(ctx.mgr, ctx.instance).get("positions") or []) if p.get("market_order_id") in ids]
            if mine and abs(float(mine[0].get("unrealized_pnl") or 0)) >= 0.5:
                break
        closed = run_parallel(lambda m: ctx.c.squareoff(ctx.mgr, m, ctx.instance), [i for i in ids if i], workers=min(n, S.MAX_THREADS))
        ev.check(all(c.ok for c in closed), f"burst x{n}: every opened position closed", f"{sum(c.ok for c in closed)}/{len(closed)} closed", sev="HIGH")
        time.sleep(8)
        rows = ctx.alloc_rows_after(base)
        if not rows:
            ev.skip(f"burst x{n}: allocation checks", "price never moved enough, so all closes were zero-PnL and created no allocation rows (not a pass)")
            continue
        pairs = [(r["trade_id"], r["follower"]) for r in rows]
        ev.check(len(pairs) == len(set(pairs)), f"burst x{n}: no duplicate (trade, investor) allocations", f"rows={len(rows)} unique={len(set(pairs))}", sev="HIGH")
        by_trade = {}
        for r in rows:
            by_trade.setdefault(r["trade_id"], set()).add(r["follower"])
        pool_now = ctx.pool_by_investor()
        short = {}
        for t, got in by_trade.items():
            pnl_t = next(r["order_pnl"] for r in rows if r["trade_id"] == t)
            must, _ = split_dust(pool_now, pnl_t)
            if must - got:
                short[t] = len(must - got)
        ev.check(not short, f"burst x{n}: every settled trade allocated to every investor whose share >= $0.005",
                 f"trades={len(by_trade)} incomplete={short}" if short else f"{len(by_trade)} settled trades fully allocated "
                 f"(zero-PnL closes create no rows)", sev="MED")
        if short or len(ok) < n:
            ev.info("first degradation point", f"ramp stopped at {n} concurrent orders")
            break


def final_audit(ctx, ev):
    rows = ctx.c.allocations(ctx.mgr, role="master", limit=500, instance=ctx.instance).get("allocations") or []
    pairs = [(r["trade_id"], r["follower"]) for r in rows]
    ev.check(len(pairs) == len(set(pairs)), "final audit: no duplicate (trade, investor) allocation in the latest 500 rows", f"rows={len(rows)}", sev="HIGH")
    pool = ctx.pool()
    ev.check(len({p["follower_client_id"] for p in pool}) == len(pool), "final audit: no duplicate pool membership", f"{len(pool)} active members", sev="HIGH")


def body(ctx: Ctx, ev: Evidence):
    n = max(3, min(ctx.args.concurrency, S.MAX_THREADS))
    try:
        fuzz(ctx, ev)
        g1 = race_same_key(ctx, ev, n)
        g2 = race_diff_keys(ctx, ev, n)
        unfollow_storm(ctx, ev, g1, n)
        unfollow_storm(ctx, ev, g2, n)
        with manager_lock():
            order_burst(ctx, ev, [int(x) for x in ctx.args.ramp.split(",") if x.strip()])
    finally:
        final_audit(ctx, ev)
    ev.notes.append("Test members (pammtest*) are created fresh each run and leave the pool again during the unfollow storm.")


if __name__ == "__main__":
    main_wrapper("breakpoint", "PAMM breakpoint & race hunter", body,
                 extra=lambda p: p.add_argument("--concurrency", type=int, default=10))
