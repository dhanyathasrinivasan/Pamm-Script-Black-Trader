# Testing guide

## Mental model

PAMM here is a **settlement** model, not a copy-trading model: investors get no positions. When the manager closes a trade,
the platform writes one *allocation row* per investor. Every suite therefore does the same thing:

1. the manager opens a (paper) trade and waits for the price to move, so the PnL is not zero,
2. closes it,
3. waits until the expected investors have rows,
4. compares every row with what the rules say it should be, and **prints the numbers it compared**.

A suite that produces no PASS marker is reported as a failure ("suite checked nothing"), so a run can never pass vacuously.

## Account topology

| Role | Config | Used for |
|---|---|---|
| Manager | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | places and closes trades; its master id is **discovered**, never configured |
| Back-office admin | `BLACKADMIN_*` | creates throw-away members, sets paper balances |
| Investors | `INVESTOR_PREFIX<1..N>@INVESTOR_DOMAIN`, `INVESTOR_PASSWORD` | the shared pool being verified. Only `Join / Leave Lifecycle` ever modifies one (and restores it) |
| Throw-away members | `pammtest*@...` created per run | all race, fuzz and risk tests, so shared investors are never abused |

## Suites

| Suite | Proves | Risk | ~Time |
|---|---|---|---|
| Allocation Sizing | percentage = deposit / pool, shared_pnl = pnl x percentage, rows add up, nobody missing or duplicated | safe | 2 m |
| Fees & Money Conservation | documented manager share vs credited PnL, running_pnl and wallet movement, manager wallet | safe | 4 m |
| Seeded Random Walk | random manager trades, each model-checked; `--seed N` reproduces, op log on failure | safe | 4 m |
| Accounting Audit | read-only: pool totals, duplicates, per-trade 100%, running_pnl = sum(shared_pnl) | safe | 1 m |
| Join / Leave Rules | leave refused while the manager has open trades, join idempotency, no double membership | safe | 1 m |
| Join / Leave Lifecycle | full mid-trade join and leave. **Closes all open manager positions first** | destructive | 3 m |
| Risk Limits & Deposits | over-wallet / zero / negative deposits, wallet debit and refund, minimum deposit | safe | 1 m |
| Breakpoint & Race Hunter | 31 hostile payloads, concurrent joins (same and different keys), unfollow storm, order burst ramp, final audit | safe | 5 m |
| Emergency Teardown | closes every open manager position | destructive | 1 m |

## Reading a report

Each run writes `reports/evidence/<suite>_evidence_<timestamp>.json` and `.md`. Statuses:

* `PASS` the compared values agreed. `FAIL` / `HIGH` an invariant was violated (a bug or a wrong model). `MED` suspicious, counted as a warning.
* `INFO` a measurement. `SKIP` something could not be tested (the reason is printed, e.g. the market did not move).

Severity model: `HIGH` = invariant violated -> FAIL. `MED` = suspicious / non-idempotent -> WARN. `INFO` = measurement.

## Safety model

* Everything defaults to **paper**. The real instance needs `--i-understand-real-money` on the CLI and the typed word `REAL` in the dashboard.
* Destructive suites need the typed word `TEARDOWN`. Stopping a run reminds you to run Emergency Teardown.
* POSTs are never auto-retried (a retry can duplicate a join or an unfollow).
* Race/fuzz tests use brand-new members. Suites restore anything they change inside `finally`.
* The dashboard binds to `127.0.0.1`, rejects non-localhost `Host` headers and refuses to listen elsewhere without `DASHBOARD_TOKEN`.

## Add a suite in four steps

1. Create `test_suite/<name>_runner.py`; write `def body(ctx, ev)` using `ctx.trade_and_settle(...)` and `ev.check(cond, name, detail)`.
2. End with `main_wrapper("<name>", "<description>", body)` (gives `--start --end --instance --lot --seed --ramp` for free).
3. Add one entry to `SUITES` in `dashboard/suites.py` (flags must exist in your runner; `pytest` verifies it).
4. `python -m pytest`, run it once on paper, check the evidence file.

## Known platform findings (paper, 2026-11-12)

Each was observed with exact numbers; the evidence files are in `reports/evidence/` after a run.

1. **Performance share is not visible.** On a winning trade (+4.88), `sum(shared_pnl)` = +4.90, i.e. investors are credited 100% of
   the pro-rata profit; the documented 20% manager share is not deducted from `shared_pnl` or `running_pnl`, investor wallets do
   not change, and the manager wallet receives the whole trade PnL. The fee may be applied at leave/settlement time, which could not
   be tested while the manager holds open positions. *Fees & Money Conservation* fails on this until the rule is confirmed.
2. **Sub-cent shares are dropped.** An investor whose share rounds to $0.00 gets no allocation row (all 22 missing investors had an exact
   share below 0.005, the smallest recorded share was 0.0051). Small investors never accumulate PnL on small trades, and
   `sum(shared_pnl)` deviates from the trade PnL by up to 0.005 x investors (e.g. -1.20 vs -1.145).
3. **`/pamm/allocations?limit=` above 500 silently becomes 50** instead of being rejected or capped. Easy to under-read the pool.
4. **Leaving is blocked while the manager has open trades** (HTTP 400, "You can leave once the manager's open trades you share in are
   closed"). Five manager positions opened outside these suites were open during testing, so the full lifecycle needs
   `Join / Leave Lifecycle` (which closes them, with confirmation).
5. **Error text has an encoding glitch**: the "already have an active subscription ... leave it to settle" message contains a corrupted dash.
6. **Held up well**: 31 hostile join payloads (zero/negative/NaN/Infinity/huge/wrong types/injection) all got 4xx; 10 concurrent joins with the
   same or different keys created exactly one membership and credited the deposit once; 10 concurrent unfollows refunded once;
   unauthenticated join/unfollow got 401; no duplicate (trade, investor) rows across 1,600+ allocation rows.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "market did not move enough" / SKIP | quiet market; the suite holds up to ~45 s. Re-run, or use a larger `--lot` |
| Leave refused / lifecycle SKIP | the manager has open positions; run `Join / Leave Lifecycle` or Emergency Teardown |
| Login 401 for investors | `INVESTOR_PASSWORD` missing in `.env` |
| Dashboard 403 | opened via a non-localhost host; set `DASHBOARD_TOKEN` and use `/?token=...` |
