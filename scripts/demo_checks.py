"""Run every deliberately broken scenario through the real check runner and print what fires.

    uv run python scripts/demo_checks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from broken_scenarios import SCENARIOS, clean_obs, context, run_scenario  # noqa: E402

from cleantape.checks.runner import overall, run_checks  # noqa: E402
from cleantape.config import load_config  # noqa: E402


def main() -> None:
    cfg = load_config()
    base = run_checks(context(cfg, clean_obs()))
    print(f"{'clean real data':28s} overall={overall(base).name}")
    for sc in SCENARIOS:
        res = run_scenario(cfg, sc)
        hit = res[(res["check"] == sc.check) & (res["subject"] == sc.subject)
                  & (res["severity"] != "OK")]
        msg = hit["message"].iloc[0] if not hit.empty else "NOT CAUGHT"
        sev = hit["severity"].iloc[0] if not hit.empty else "-"
        print(f"{sc.name:28s} overall={overall(res).name:4s} {sc.check:12s} {sev:4s} {msg}")


if __name__ == "__main__":
    main()
