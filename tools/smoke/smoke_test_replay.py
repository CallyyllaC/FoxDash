from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import shutil
import time

from foxdash_lite.runtime import FoxDashRuntime, RuntimeConfig


ROOT = REPO_ROOT
LOGS = ROOT / ".smoke_logs"
SOURCE = ROOT / "sample_data" / "replay_sample.csv"


def main() -> int:
    shutil.rmtree(LOGS, ignore_errors=True)
    runtime = FoxDashRuntime(RuntimeConfig(
        source="replay",
        replay_log=str(SOURCE),
        replay_random_start=False,
        replay_speed=8.0,
        log_dir=LOGS,
        enable_i2c=False,
    ))
    runtime.start()
    deadline = time.monotonic() + 8.0
    try:
        while time.monotonic() < deadline and runtime.store.latest().sequence == 0:
            time.sleep(0.05)
        state = runtime.store.latest()
        assert state.sequence > 0, "Replay never published a state"
        assert state.telemetry.obdConnection == "replay"
        assert state.telemetry.efficiencyScore is not None
        assert state.telemetry.moodScore is not None
        print(f"OK: replay published sample {state.telemetry.sample} at sequence {state.sequence}")
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
