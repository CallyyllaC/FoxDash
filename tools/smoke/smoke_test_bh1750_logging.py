from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import csv
import shutil
import time
import types

from foxdash_lite.i2c_controller import I2cController
from foxdash_lite.telemetry_logger import TelemetryLogger


ROOT = REPO_ROOT
LOGS = ROOT / ".smoke_ambient_logs"


class FakeSMBus:
    """Enough BH1750-shaped I²C behaviour to test the worker without Pi GPIO."""

    readings = ([0x00, 27], [0x01, 202])  # 22.5 lx, 381.7 lx

    def __init__(self, bus_number: int) -> None:
        assert bus_number == 11
        self.index = 0
        self.mode: int | None = None
        self.closed = False

    def write_byte(self, address: int, value: int) -> None:
        assert address == 0x23
        self.mode = value

    def i2c_rdwr(self, message: "FakeReadMessage") -> None:
        assert message.address == 0x23 and message.length == 2 and self.mode == 0x10
        value = self.readings[min(self.index, len(self.readings) - 1)]
        self.index += 1
        message.data[:] = value

    def read_i2c_block_data(self, address: int, command: int, length: int):
        raise AssertionError("BH1750 must be read without an SMBus command byte")

    def close(self) -> None:
        self.closed = True


class FakeReadMessage:
    def __init__(self, address: int, length: int) -> None:
        self.address = address
        self.length = length
        self.data = [0] * length

    def __iter__(self):
        return iter(self.data)


class FakeI2cMsg:
    @staticmethod
    def read(address: int, length: int) -> FakeReadMessage:
        return FakeReadMessage(address, length)


def main() -> int:
    saved = sys.modules.get("smbus2")
    fake_module = types.ModuleType("smbus2")
    fake_module.SMBus = FakeSMBus
    fake_module.i2c_msg = FakeI2cMsg
    sys.modules["smbus2"] = fake_module

    shutil.rmtree(LOGS, ignore_errors=True)
    logger = TelemetryLogger(LOGS, fsync_every_rows=1)
    logger.start(source_name="smoke")
    controller = I2cController(logger.log_environment, bus_number=11, address=0x23, poll_interval_s=0.2, filter_tau_s=0.4)
    controller.start()
    try:
        time.sleep(0.68)
    finally:
        controller.stop()
        logger.close()
        if saved is None:
            sys.modules.pop("smbus2", None)
        else:
            sys.modules["smbus2"] = saved

    path = next(LOGS.glob("psa_ambient_light_*.csv"))
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    good = [row for row in rows if row["sensor_ok"] == "1"]
    assert len(good) >= 2, f"Expected at least two valid samples, got {rows!r}"
    assert abs(float(good[0]["ambient_lux_raw"]) - 22.5) < 0.01
    assert abs(float(good[1]["ambient_lux_raw"]) - (458 / 1.2)) < 0.01
    assert all(row["sensor_bus"] == "11" and row["sensor_address"] == "35" for row in good)
    assert float(good[1]["ambient_lux_filtered"]) > float(good[0]["ambient_lux_filtered"])
    print("OK: BH1750 worker read bus 11 / 0x23 and wrote raw + filtered ambient CSV rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
