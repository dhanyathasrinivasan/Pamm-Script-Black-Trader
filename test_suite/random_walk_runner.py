#!/usr/bin/env python3
"""
Suite: Seeded random walk. A reproducible random sequence of manager trades (side, size) and read-only
investor checks; every settled trade is model-checked with the same invariants as the allocation suite.
Re-run a failure exactly with --seed N. The operation log is printed on failure.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_suite.allocation_runner import verify_trade  # noqa: E402
from test_suite.common import Ctx, Evidence, main_wrapper  # noqa: E402


def body(ctx: Ctx, ev: Evidence):
    seed = ctx.args.seed if ctx.args.seed is not None else random.randrange(1, 10**6)
    rnd = random.Random(seed)
    ev.info("seed", f"{seed} (re-run with --seed {seed})")
    oplog = []
    for step in range(1, ctx.args.ops + 1):
        side = rnd.choice(["BUY", "SELL"])
        lot = round(rnd.choice([0.1, 0.25, 0.5, 1.0]), 2)
        sign = rnd.choice([+1, -1, 0])
        oplog.append(f"step{step}: {side} {lot} want_sign={sign}")
        pool = ctx.pool_by_investor()
        res = ctx.trade_and_settle(ev, side=side, qty=lot, want_sign=sign, min_abs_pnl=1.0, max_wait=30)
        if not res:
            continue
        if abs(res["realized_pnl"]) < 0.01:
            ev.skip(f"step {step}", f"{side} {lot}: zero PnL, nothing allocated (tape={res['tape']})")
            continue
        verify_trade(ctx, ev, f"step{step} {side} {lot} {res['realized_pnl']:+.2f}", res, pool)
    ev.data["oplog"] = oplog
    if ev.n_fail:
        print("OPERATION LOG:\n  " + "\n  ".join(oplog))


if __name__ == "__main__":
    main_wrapper("random_walk", "PAMM seeded random walk", body,
                 extra=lambda p: p.add_argument("--ops", type=int, default=4))
