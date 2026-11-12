"""
PAMM API client: thread-safe, token-caching.

IMPORTANT: POSTs are NEVER retried automatically. A retry after a 5xx or timeout can duplicate a
join, unfollow or order. GETs are retried a couple of times on transient errors.
Every call returns a Resp(status, data, text) - it never raises on HTTP errors.
"""

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import settings as S  # noqa: E402


@dataclass
class Resp:
    status: int
    data: Any
    text: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def get(self, key, default=None):
        return self.data.get(key, default) if isinstance(self.data, dict) else default

    @property
    def error(self) -> str:
        if isinstance(self.data, dict):
            return str(self.data.get("error") or self.data.get("message") or "")
        return (self.text or "")[:200]


class PammClient:
    def __init__(self, base_url: str = None, max_workers: int = 10, timeout: int = 20):
        self.base_url = (base_url or S.BASE_URL).rstrip("/")
        self.timeout = timeout
        self._lock = threading.Lock()
        self._tokens: Dict[str, str] = {}
        self._admin_token: Optional[str] = None
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=max_workers * 2, pool_maxsize=max_workers * 2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ---- low level -------------------------------------------------------------------
    def _send(self, method: str, path: str, token: str = None, body: Any = None, params: dict = None,
              raw_body: bytes = None) -> Resp:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1          # never retry non-idempotent calls
        last = None
        for n in range(attempts):
            t0 = time.time()
            try:
                kw = {"json": body} if raw_body is None else {"data": raw_body}
                r = self.session.request(method, self.base_url + path, headers=headers, params=params,
                                         timeout=self.timeout, **kw)
                try:
                    data = r.json()
                except ValueError:
                    data = None
                last = Resp(r.status_code, data, r.text[:500], time.time() - t0)
                if r.status_code < 500 or method != "GET":
                    return last
            except requests.RequestException as e:
                last = Resp(0, None, f"{type(e).__name__}: {e}"[:300], time.time() - t0)
                if method != "GET":
                    return last
            time.sleep(0.4 * (n + 1))
        return last

    # ---- auth ------------------------------------------------------------------------
    def admin_token(self) -> str:
        with self._lock:
            if self._admin_token:
                return self._admin_token
        r = self._send("POST", "/admin/api/login",
                       body={"email": S.ADMIN_EMAIL, "password": S.ADMIN_PASSWORD, "remember": True})
        if not r.ok or not r.get("token"):
            raise RuntimeError(f"Admin login failed: HTTP {r.status}")
        with self._lock:
            self._admin_token = r.get("token")
        return self._admin_token

    def token(self, email: str, password: str = None, refresh: bool = False) -> str:
        with self._lock:
            if not refresh and email in self._tokens:
                return self._tokens[email]
        if password is None:
            is_mgr = email.lower() == (S.MANAGER_EMAIL or "").lower()
            password = S.MANAGER_PASSWORD if is_mgr else S.INVESTOR_PASSWORD
        sess = requests.Session()           # isolated session: no cookie races between threads
        r = sess.post(self.base_url + "/api/auth/login", json={"email": email, "password": password},
                      timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Login failed for {email}: HTTP {r.status_code}")
        t = sess.post(self.base_url + "/api/auth/chart-token", timeout=self.timeout)
        if t.status_code != 200 or not t.json().get("token"):
            raise RuntimeError(f"chart-token failed for {email}: HTTP {t.status_code}")
        with self._lock:
            self._tokens[email] = t.json()["token"]
        return self._tokens[email]

    # ---- admin -----------------------------------------------------------------------
    def admin_members(self) -> List[dict]:
        r = self._send("GET", "/admin/api/members", token=self.admin_token())
        return r.get("members", []) if r.ok else []

    def admin_create_member(self, email: str, password: str, name: str = "PAMM Investor") -> Resp:
        return self._send("POST", "/admin/api/members", token=self.admin_token(),
                          body={"email": email, "password": password, "name": name})

    def admin_set_balance(self, member_id: str, balance: float, instance: str = "paper") -> Resp:
        return self._send("POST", f"/admin/api/members/{member_id}/balance", token=self.admin_token(),
                          body={"instance": instance, "balance": balance, "remark": "PAMM test seed", "force": True})

    # ---- generic OMS -----------------------------------------------------------------
    def oms(self, email: str, method: str, path: str, instance: str = "paper", body: Any = None,
            params: dict = None, raw_body: bytes = None) -> Resp:
        return self._send(method, f"/v1/oms/{instance}{path}", token=self.token(email), body=body,
                          params=params, raw_body=raw_body)

    # ---- PAMM ------------------------------------------------------------------------
    def masters(self, email: str, instance="paper") -> Resp:
        return self.oms(email, "GET", "/pamm/masters", instance)

    def follows(self, email: str, instance="paper") -> Resp:
        return self.oms(email, "GET", "/pamm/follows", instance)

    def allocations(self, email: str, role: str = None, limit: int = 500, instance="paper") -> Resp:
        p = {"limit": limit}
        if role:
            p["role"] = role
        return self.oms(email, "GET", "/pamm/allocations", instance, params=p)

    def join(self, email: str, master_client_id: str, deposit, op_key: str, instance="paper") -> Resp:
        return self.oms(email, "POST", "/pamm/join", instance,
                        body={"master_client_id": master_client_id, "deposit": deposit, "operation_key": op_key})

    def unfollow(self, email: str, master_client_id: str, instance="paper") -> Resp:
        return self.oms(email, "POST", "/pamm/unfollow", instance, body={"master_client_id": master_client_id})

    # ---- trading ---------------------------------------------------------------------
    def positions(self, email: str, instance="paper") -> Resp:
        return self.oms(email, "GET", "/positions", instance)

    def orders(self, email: str, instance="paper") -> Resp:
        return self.oms(email, "GET", "/orders", instance)

    def history(self, email: str, limit: int = 100, instance="paper") -> Resp:
        return self.oms(email, "GET", "/history", instance, params={"limit": limit})

    def place_order(self, email: str, side: str, qty: float, symbol: str = None, instance="paper", **extra) -> Resp:
        body = {"symbol": symbol or S.DEFAULT_SYMBOL, "side": side.upper(), "qty": qty, "type": "MARKET"}
        body.update(extra)
        return self.oms(email, "POST", "/orders", instance, body=body)

    def squareoff(self, email: str, market_order_id: str, instance="paper") -> Resp:
        return self.oms(email, "POST", f"/positions/{market_order_id}/squareoff", instance, body={})

    def squareoff_all(self, email: str, instance="paper") -> int:
        """Close every open position of an account. Returns how many were closed."""
        r = self.positions(email, instance)
        n = 0
        for p in (r.get("positions") or []):
            mid = p.get("market_order_id")
            if mid and self.squareoff(email, mid, instance).ok:
                n += 1
        return n
