# PAMM API notes (discovered, not vendor documentation)

Found by reading the trader terminal's frontend bundle (`chartv2.blacktrader.com`) and by calling the endpoints on the
**paper** instance. Field names below were taken from live responses, not guessed. Anything marked *unverified* has not
been exercised. All OMS calls need `Authorization: Bearer <chart-token>`.

Base: `PLATFORM_BASE_URL` (e.g. `https://lpadmin.blackfx.com`), OMS prefix `/v1/oms/{instance}` where instance is `paper` or `real`.

## Authentication

| Purpose | Call | Notes |
|---|---|---|
| Back-office admin | `POST /admin/api/login` `{email,password,remember}` -> `{token}` | creates members, sets balances |
| Trader / investor / manager | `POST /api/auth/login` `{email,password}` (sets a session cookie) then `POST /api/auth/chart-token` -> `{token}` | use an **isolated** HTTP session per login (cookie races between threads) |
| Public config | `GET /api/config` | `oms_domain`, `broker_token`, ... |

## Back-office

| Call | Body | Response fields used |
|---|---|---|
| `GET /admin/api/members` | | `members[]`: `member_id` (== client id `mem_...`), `email`, `status`, `balances.{paper,real}` |
| `POST /admin/api/members` | `{email,password,name}` | `member.member_id` |
| `POST /admin/api/members/{id}/balance` | `{instance,balance,remark,force}` | |

## PAMM (`/v1/oms/{instance}/pamm/...`)

The terminal only exposes these six PAMM routes; there are **no** separate deposit, withdraw, settle or fee endpoints.

| Call | Purpose | Key fields |
|---|---|---|
| `GET masters` | public list of masters | `masters[]`: `client_id`, `name`, `share_percent`, `stats.{followers,fund,drawdown,growth,trades,rank,risk_level}` |
| `GET follows` | caller's view. For a **master**: `is_master`, `master{client_id,display_name,share_percent,status}`, `pool[]`, `pool_total`, `pool_history[]`. For an **investor**: `subscriptions[]` | pool/subscription row: `pamm_id`, `master_client_id`, `follower_client_id`, `deposit`, `share_percent`, `running_pnl`, `status` (`active`...), `joined_at` |
| `POST join` | `{master_client_id, deposit, operation_key}` | `subscription.pamm_id`. Debits the investor's wallet by `deposit`. Rejects: deposit <= 0, deposit above free margin, already active |
| `POST unfollow` | `{master_client_id}` | refunds the deposit. **Refused with 400 while the manager has open trades the investor shares in** |
| `POST master` | `{display_name, share_percent, ...}` | become a master (not exercised; blocked for accounts that follow a master) |
| `GET allocations?role=master&limit=&offset=` | settlement rows, newest first | `alloc_id`, `trade_id`, `follower`, `master`, `pamm_id`, `deposit`, `pool_total`, `percentage` (of 100), `order_pnl`, `shared_pnl`, `running_pnl`, `at` |

`limit` is honoured up to **500**; a larger value silently falls back to 50. Page with `offset`. Without `role=master`, an
investor only sees their own rows.

## Orders and positions (manager trades; used to create PnL)

| Call | Notes |
|---|---|
| `POST orders` `{symbol,side,qty,type:"MARKET"}` | response `order.{market_order_id,state,filled_qty,avg_fill_price}`, `position.{net_qty,status}` |
| `GET positions` | `positions[].{market_order_id,symbol,side,net_qty,unrealized_pnl,status}` and `funds.{balance,equity,free_margin,used_margin,unrealized_pnl}` |
| `POST positions/{market_order_id}/squareoff` `{}` | `realized_pnl`, `exit_price`. Closing is what **settles** the PnL into the pool |
| `GET history?limit=`, `GET orders` | |

## Settlement model (observed)

* Investors never receive mirrored positions. When the manager **closes** a trade, one allocation row per investor is written.
* `percentage = 100 * deposit / pool_total` at close time; `shared_pnl = round(order_pnl * percentage / 100, 2)`.
* An investor whose share rounds to `$0.00` gets **no row**.
* `running_pnl` on the subscription equals the running sum of that investor's `shared_pnl`.
* Allocation lag after the close is typically 4-5 seconds.
* A trade closed at exactly the entry price has zero PnL and creates no rows.

## Unverified

Real-instance behaviour, `POST master`, whether a manager performance fee is applied at leave/settlement time, pending
orders and TP/SL interaction with the pool, and any scheduled (non per-trade) settlement.
