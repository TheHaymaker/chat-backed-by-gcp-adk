#!/usr/bin/env python3
"""run-evals: CI gate. Runs all structural evalsets through the gateway pipeline.

Usage: python tools/run_evals.py [--runner mock|adk]
Exit non-zero on any failure. With --runner adk, set ADK_BASE_URL to point the
same cases at the real agent (see gateway/adk_runner.py).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.eval.harness import Harness  # noqa: E402


def main() -> int:
    runner_factory = None
    if "--runner" in sys.argv and "adk" in sys.argv:
        from gateway.adk_runner import AdkRunner
        runner_factory = lambda registry: AdkRunner()  # noqa: E731
    h = Harness(services_path=ROOT / "services.yaml", runner=runner_factory)
    results = h.run_all(ROOT / "tests" / "eval" / "cases")
    width = max(len(r.case_id) for r in results)
    failed = 0
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  {mark}  {r.case_id.ljust(width)}")
        for f in r.failures:
            failed_line = f"         - {f}"
            print(failed_line)
        failed += 0 if r.passed else 1
    print(f"\n{len(results) - failed}/{len(results)} eval cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
