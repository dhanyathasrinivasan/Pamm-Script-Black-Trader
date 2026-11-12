"""
Offline smoke tests - no network, no credentials, no trades.   Run:  python -m pytest -q
They protect what a new contributor is most likely to break: the suite registry, command building,
real-money guards, dashboard access control, the setup doctor and the core test helpers.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import suites as S  # noqa: E402

LOCAL = {"Host": "localhost"}
DEFAULTS = {"investor_count": 100}
KNOWN_FLAGS = {"start", "end", "instance", "lot", "ramp", "concurrency", "ops", "seed"}


@pytest.fixture()
def client():
    from dashboard.app import app
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------- registry
def test_registry_is_consistent():
    keys = [s["key"] for s in S.SUITES]
    assert len(keys) == len(set(keys)), "duplicate suite keys"
    for s in S.SUITES:
        assert s["risk"] in ("safe", "real", "destructive"), s["key"]
        assert set(s["flags"]) <= KNOWN_FLAGS, s["key"]
        if s["script"]:
            assert (ROOT / s["script"]).exists(), f"{s['key']}: missing {s['script']}"


def test_every_runner_accepts_the_flags_the_registry_passes():
    """Catches a registry flag (or `extra` argument) that the runner's argparse would reject."""
    flag_map = {"start": "--start", "end": "--end", "instance": "--instance", "lot": "--lot", "ramp": "--ramp",
                "concurrency": "--concurrency", "ops": "--ops", "seed": "--seed"}
    common = (ROOT / "test_suite" / "common.py").read_text(encoding="utf-8")
    for s in S.SUITES:
        if not s["script"]:
            continue
        text = (ROOT / s["script"]).read_text(encoding="utf-8") + common
        for f in s["flags"]:
            assert f'"{flag_map[f]}"' in text, f"{s['key']}: {s['script']} has no {flag_map[f]}"
        for extra in s.get("extra", []):
            assert f'"{extra}"' in text, f"{s['key']}: runner does not define {extra}"


@pytest.mark.parametrize("name", sorted(p.stem for p in (ROOT / "test_suite").glob("*.py") if p.stem != "__init__"))
def test_runner_modules_import(name):
    importlib.import_module(f"test_suite.{name}")


def test_pytest_only_collects_tests_dir():
    ini = (ROOT / "pytest.ini").read_text()
    assert "testpaths = tests" in ini        # live runners must never be collected


# ---------------------------------------------------------------- command building
def test_build_command_basic_and_clamped():
    s = S.SUITE_INDEX["fees"]
    cmd = S.build_command(s, {"start": 5, "end": 2, "instance": "real"}, DEFAULTS)   # swapped on purpose
    assert cmd[cmd.index("--start") + 1] == "2" and cmd[cmd.index("--end") + 1] == "5"
    assert cmd[cmd.index("--instance") + 1] == "real" and "--i-understand-real-money" in cmd


def test_build_command_rejects_injection_and_junk():
    s = S.SUITE_INDEX["breakpoint"]
    cmd = S.build_command(s, {"ramp": "10,abc,$(id)", "concurrency": "1; rm -rf /", "instance": "../../etc", "lot": "9999"}, DEFAULTS)
    assert all(";" not in c and "$" not in c for c in cmd)
    assert cmd[cmd.index("--instance") + 1] == "paper"
    assert float(cmd[cmd.index("--lot") + 1]) <= 100        # clamped
    assert "--i-understand-real-money" not in cmd


def test_fixed_instance_cannot_be_overridden():
    assert S.effective_instance(S.SUITE_INDEX["teardown"], {"instance": "real"}) == "paper"


def test_confirmation_rules():
    assert S.needs_confirmation(S.SUITE_INDEX["teardown"], {})
    assert S.needs_confirmation(S.SUITE_INDEX["membership_full"], {})
    assert S.needs_confirmation(S.SUITE_INDEX["allocation"], {"instance": "real"})
    assert not S.needs_confirmation(S.SUITE_INDEX["allocation"], {"instance": "paper"})


def test_destructive_opt_in_flag_only_on_the_destructive_suite():
    assert "--close-manager-positions" in S.build_command(S.SUITE_INDEX["membership_full"], {}, DEFAULTS)
    assert "--close-manager-positions" not in S.build_command(S.SUITE_INDEX["membership"], {}, DEFAULTS)


# ---------------------------------------------------------------- dashboard API
def test_config_endpoint_never_leaks_secrets(client):
    from config import settings
    body = client.get("/api/config", headers=LOCAL).get_data(as_text=True)
    for secret in (settings.MANAGER_PASSWORD, settings.ADMIN_PASSWORD, settings.INVESTOR_PASSWORD):
        if secret:
            assert secret not in body
    assert "password" not in body.lower() and "token" not in json.loads(body).keys()


def test_suites_endpoint(client):
    d = client.get("/api/suites", headers=LOCAL).get_json()
    assert len(d["suites"]) == len(S.SUITES) and "investor_count" in d["defaults"]


@pytest.mark.parametrize("payload,code", [
    ({"suite": "nope"}, 400),
    ({"suite": "teardown"}, 412),
    ({"suite": "membership_full"}, 412),
    ({"suite": "allocation", "params": {"instance": "real"}}, 412),
])
def test_run_guards(client, payload, code):
    assert client.post("/api/run", json=payload, headers=LOCAL).status_code == code


def test_dispatch_guards(client):
    assert client.post("/api/dispatch", json={"instance": "real", "confirm": True}, headers=LOCAL).status_code == 400
    assert client.post("/api/dispatch", json={"side": "BUY"}, headers=LOCAL).status_code == 412
    assert client.post("/api/dispatch", json={"confirm": True, "side": "HODL"}, headers=LOCAL).status_code == 400
    assert client.post("/api/dispatch", json={"confirm": True, "qty": 999}, headers=LOCAL).status_code == 400


def test_account_endpoint_rejects_unknown_accounts(client):
    assert client.get("/api/account/someone@else.com", headers=LOCAL).status_code == 404


def test_non_localhost_is_blocked_without_token(client):
    assert client.get("/api/config", headers={"Host": "evil.example.com"}).status_code == 403


def test_token_protection(client, monkeypatch):
    import dashboard.app as app_mod
    monkeypatch.setattr(app_mod, "DASHBOARD_TOKEN", "s3cret")
    assert client.get("/api/config", headers={"Host": "10.0.0.5"}).status_code == 401
    assert client.get("/api/config?token=wrong", headers={"Host": "10.0.0.5"}).status_code == 401
    ok = client.get("/api/config?token=s3cret", headers={"Host": "10.0.0.5"})
    assert ok.status_code == 200 and "bt_dash" in ok.headers.get("Set-Cookie", "")


def test_history_log_endpoint_rejects_path_traversal(client):
    assert client.get("/api/history/..%2F..%2Fetc%2Fpasswd/log", headers=LOCAL).status_code in (400, 404)


def test_reports_endpoint_rejects_path_traversal(client):
    assert client.get("/api/reports/..%2F.env", headers=LOCAL).status_code == 404


# ---------------------------------------------------------------- doctor
def test_doctor_flags_placeholder_credentials(monkeypatch):
    from config import doctor, settings
    monkeypatch.setattr(settings, "MANAGER_PASSWORD", "your_password_here")
    monkeypatch.setattr(settings, "MANAGER_EMAIL", "")
    res = {r["id"]: r for r in doctor.check_credentials()}
    assert res["var_ADMIN_PASSWORD"]["status"] == "fail" and res["var_ADMIN_USERNAME"]["status"] == "fail"
    assert "your_password_here" not in str(res)            # never echo secrets


def test_doctor_offline_runs_without_network():
    from config import doctor
    ids = {r["id"] for r in doctor.run_checks(offline=True)}
    assert {"python", "env_file", "writable"} <= ids


# ---------------------------------------------------------------- core helpers
def test_client_never_retries_posts(monkeypatch):
    """A retried POST could duplicate a join/unfollow/order."""
    from test_suite.client import PammClient
    c = PammClient()
    calls = []

    class R:
        status_code = 503
        text = "boom"

        def json(self):
            raise ValueError

    monkeypatch.setattr(c.session, "request", lambda *a, **k: (calls.append(a[0]) or R()))
    assert c._send("POST", "/x", body={}).status == 503 and calls == ["POST"]
    calls.clear()
    c._send("GET", "/x")
    assert len(calls) == 3                                  # GETs are retried


def test_split_dust_models_subcent_shares():
    from test_suite.common import split_dust
    pool = {"a": {"deposit": 1000.0}, "b": {"deposit": 1000.0}, "c": {"deposit": 1.0}}
    must, dust = split_dust(pool, 1.0)
    assert "a" in must and "b" in must and "c" in dust       # c's share of $1.00 is far below half a cent


def test_evidence_vacuous_run_is_a_failure(tmp_path, monkeypatch):
    import test_suite.common as C
    monkeypatch.setattr(C, "EVIDENCE_DIR", tmp_path)
    ev = C.Evidence("unit")
    ev.info("only informational")
    assert ev.finish() == 1                                  # a suite that checked nothing must not pass
    ev2 = C.Evidence("unit2")
    ev2.check(True, "something compared", "1 == 1")
    assert ev2.finish() == 0
    assert list(tmp_path.glob("unit2_evidence_*.json")) and list(tmp_path.glob("unit2_evidence_*.md"))


def test_evidence_severity_mapping(tmp_path, monkeypatch):
    import test_suite.common as C
    monkeypatch.setattr(C, "EVIDENCE_DIR", tmp_path)
    ev = C.Evidence("sev")
    ev.passed("p")
    ev.check(False, "soft", "x", sev="MED")
    assert ev.n_fail == 0                                    # MED is a warning
    ev.check(False, "hard", "x", sev="HIGH")
    assert ev.n_fail == 1                                    # HIGH is a failure


def test_real_instance_requires_explicit_flag():
    from test_suite.common import guard_instance, make_parser
    args = make_parser("x").parse_args(["--instance", "real"])
    with pytest.raises(SystemExit):
        guard_instance(args)
    guard_instance(make_parser("x").parse_args(["--instance", "real", "--i-understand-real-money"]))
