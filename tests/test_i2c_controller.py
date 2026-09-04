from __future__ import annotations

import unittest

from foxdash_lite.i2c_controller import (
    BH1750_CONTINUOUS_HIGH_RESOLUTION_MODE,
    BH1750_ONE_TIME_HIGH_RESOLUTION_MODE,
    BH1750_POWER_DOWN,
    I2cController,
)


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


class RawReadBus:
    def __init__(self) -> None:
        self.mode = BH1750_CONTINUOUS_HIGH_RESOLUTION_MODE
        self.readings = iter(([0x00, 27], [0x01, 202]))
        self.raw_reads: list[tuple[int, int]] = []

    def i2c_rdwr(self, message: FakeReadMessage) -> None:
        self.raw_reads.append((message.address, message.length))
        message.data[:] = next(self.readings)

    def read_i2c_block_data(self, address: int, command: int, length: int):
        raise AssertionError("register-style SMBus read would power down BH1750")


class LegacyBus:
    def __init__(self) -> None:
        self.mode = BH1750_POWER_DOWN
        self.pending = iter(([0x00, 27], [0x01, 202]))
        self.result = [0, 0]
        self.commands: list[int] = []

    def write_byte(self, address: int, command: int) -> None:
        self.commands.append(command)
        self.mode = command
        if command == BH1750_ONE_TIME_HIGH_RESOLUTION_MODE:
            self.result = list(next(self.pending))

    def read_i2c_block_data(self, address: int, command: int, length: int):
        self.commands.append(command)
        self.mode = command
        return self.result[:length]


class I2cControllerTests(unittest.TestCase):
    def make_controller(self) -> I2cController:
        return I2cController(lambda _snapshot: None, bus_number=11, address=0x23)

    def test_smbus2_path_reads_two_bytes_without_sending_command(self) -> None:
        controller = self.make_controller()
        bus = RawReadBus()
        controller._bus = bus
        controller._i2c_msg = FakeI2cMsg

        self.assertAlmostEqual(controller._read_raw_lux(), 22.5)
        self.assertAlmostEqual(controller._read_raw_lux(), 458 / 1.2)
        self.assertEqual(bus.raw_reads, [(0x23, 2), (0x23, 2)])

    def test_legacy_path_starts_a_fresh_one_time_conversion_per_read(self) -> None:
        controller = self.make_controller()
        bus = LegacyBus()
        controller._bus = bus
        controller._i2c_msg = None

        self.assertAlmostEqual(controller._read_raw_lux(), 22.5)
        self.assertAlmostEqual(controller._read_raw_lux(), 458 / 1.2)
        self.assertEqual(
            bus.commands,
            [
                BH1750_ONE_TIME_HIGH_RESOLUTION_MODE,
                BH1750_POWER_DOWN,
                BH1750_ONE_TIME_HIGH_RESOLUTION_MODE,
                BH1750_POWER_DOWN,
            ],
        )


if __name__ == "__main__":
    unittest.main()
