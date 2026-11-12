#!/usr/bin/env python3
"""
Setup doctor - run this first after cloning.

    python scripts/doctor.py            # full check (needs network + credentials)
    python scripts/doctor.py --offline  # local checks only
Exit code 0 = ready, 1 = something must be fixed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.doctor import run_checks, summarize  # noqa: E402

ICON = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}


def main() -> int:
    ap = argparse.ArgumentParser(description="PAMM Test Hub setup doctor")
    ap.add_argument("--offline", action="store_true", help="skip network / login checks")
    args = ap.parse_args()
    results = run_checks(offline=args.offline)
    print("\nPAMM Test Hub - setup check\n" + "=" * 46)
    for r in results:
        line = f"{ICON[r['status']]} {r['label']}"
        if r["detail"]:
            line += f"  - {r['detail']}"
        print(line)
        if r["status"] in ("fail", "warn") and r["fix"]:
            print(f"         fix: {r['fix']}")
    overall = summarize(results)
    print("=" * 46)
    print({"ok": "Ready. Start the dashboard: python dashboard/app.py",
           "warn": "Usable, with warnings above.",
           "fail": "Not ready - fix the [FAIL] items above."}[overall])
    return 1 if overall == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
