# PAMM Test Hub

Automated test suites and a web dashboard for the **PAMM (percent allocation management)** module on BlackTrader / BlackFX.
It runs a manager trade, then checks every investor's allocation, fees, accounting and the system's behaviour under
concurrency and hostile input. Everything defaults to **paper** (simulated money).

## 3-minute quickstart

```bash
python scripts/bootstrap.py          # venv + dependencies + .env from .env.example
# edit .env with your credentials (see table below)
python scripts/doctor.py             # checks Python, packages, credentials, login, manager, investors, pool
python dashboard/app.py              # open http://localhost:5001
```

First run in the dashboard: **Getting Started -> Safe first run** (the read-only Accounting Audit).

## Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `PLATFORM_BASE_URL` | platform URL |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | the PAMM **manager** account that places trades |
| `BLACKADMIN_EMAIL` / `BLACKADMIN_PASSWORD` | back-office admin (creates throw-away members, sets paper balances) |
| `INVESTOR_PREFIX`, `INVESTOR_DOMAIN`, `INVESTOR_PASSWORD`, `TEST_FOLLOWER_COUNT` | investors are `<prefix><1..N>@<domain>` |
| `MANAGER_SHARE_PERCENT` | expected performance share (the master's own setting is also read live) |
| `DEFAULT_INSTANCE`, `DEFAULT_SYMBOL`, `MAX_CONCURRENT_THREADS` | defaults |
| `DASHBOARD_HOST` / `PORT` / `TOKEN` | dashboard binding; a token is **required** to listen on anything but localhost |

The PAMM master id is discovered by logging in as the manager. No identities or credentials are hard-coded.

## Dashboard

Launcher on the left (generated from `dashboard/suites.py`), summary cards computed from real evidence, and tabs:
**Terminal** (live log, pass/fail counters), **Investors** (live grid + per-account drawer), **Order flow** (settled trades and their fan-out),
**Evidence** (latest result per suite, report viewer), **Dispatch & Verify** (one manager order, every investor compared to the expected value),
**History** (re-run, archived logs), **Getting Started** (doctor results with a copyable fix per failure).
Real-money and destructive actions need a typed confirmation (`REAL` / `TEARDOWN`).

## Suites

| Suite | What it proves | Risk |
|---|---|---|
| Allocation Sizing | pro-rata percentage and PnL share for a win and a loss, nobody missing/duplicated | safe |
| Fees & Money Conservation | documented manager share vs credited PnL; running_pnl, wallets | safe |
| Seeded Random Walk | random trades, model-checked, reproducible with `--seed` | safe |
| Accounting Audit | read-only totals, duplicates, running_pnl vs history | safe |
| Join / Leave Rules | leave blocked by open trades, join idempotency | safe |
| Join / Leave Lifecycle | mid-trade join/leave (**closes manager positions**) | destructive |
| Risk Limits & Deposits | deposit validation, wallet debit/refund | safe |
| Breakpoint & Race Hunter | fuzz, concurrent joins/unfollows, order burst ramp | safe |
| Emergency Teardown | closes all manager positions | destructive |

Run any from the CLI too: `python test_suite/allocation_runner.py --lot 0.5`. Evidence goes to `reports/evidence/`.
Details, severities and "how to add a suite": [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md). API map: [docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md).

## Repo map

```
config/       settings (.env loader) and the setup doctor
scripts/      bootstrap.py, doctor.py
test_suite/   client.py (thread-safe, never retries POSTs), common.py (evidence, helpers), one *_runner.py per suite
dashboard/    app.py (Flask), suites.py (registry), templates/index.html
tests/        offline smoke tests (python -m pytest)
docs/         API notes and testing guide
src/, run_pamm_suite.py   the original audit CLI (kept; credentials removed, now read from .env)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Doctor says "no PAMM master profile" | the `ADMIN_USERNAME` account must be a PAMM master |
| Lifecycle suite SKIPs | the manager has open positions; run the lifecycle suite (confirms) or Emergency Teardown |
| A suite reports "market did not move enough" | quiet market; re-run or raise the lot |
| Dashboard 403 | non-localhost access needs `DASHBOARD_TOKEN` |

## Security

`.env` is git-ignored; `/api/config` never returns secrets (tested); the dashboard is localhost-only by default and
refuses non-loopback binding without a token; run logs and evidence stay out of git. Rotate any credential that was ever committed.
