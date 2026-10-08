from __future__ import annotations

import time
import unittest

from foxdash_lite.backlight import BacklightStatus, USABLE_MIN, USABLE_MAX
from foxdash_lite.linked_brightness import LinkedBacklightOutput, pwm_for_palette


class FakeHyperPixel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.writes: list[int] = []

    def set(self, value: int) -> BacklightStatus:
        self.writes.append(value)
        if self.fail:
            return BacklightStatus(False, None, None, "sudo permission not configured")
        return BacklightStatus(True, value, 255)


def wait_for(check, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for backlight worker")


class LinkedBacklightTests(unittest.TestCase):
    def test_palette_and_pwm_share_calibration_factor(self) -> None:
        self.assertEqual(pwm_for_palette(1.0), USABLE_MIN)
        self.assertEqual(pwm_for_palette(100.0), USABLE_MAX)
        self.assertEqual(pwm_for_palette(25.0), 18)
        self.assertEqual(pwm_for_palette(48.0), 30)
        self.assertEqual(pwm_for_palette(-100.0), USABLE_MIN)
        self.assertEqual(pwm_for_palette(100000.0), USABLE_MAX)
        for step in range(1, 101):
            self.assertGreaterEqual(pwm_for_palette(float(step)), USABLE_MIN)
            self.assertLessEqual(pwm_for_palette(float(step)), USABLE_MAX)

    def test_invalid_factor_never_writes_pwm(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    pwm_for_palette(value)

    def test_worker_starts_idle_and_coalesces_identical_targets(self) -> None:
        fake = FakeHyperPixel()
        output = LinkedBacklightOutput(fake, min_write_interval_s=0.1)
        output.start()
        try:
            time.sleep(0.02)
            self.assertEqual(fake.writes, [])
            output.request_palette(25.0)
            wait_for(lambda: output.snapshot().actual_pwm == 18)
            self.assertEqual(fake.writes, [18])
            output.request_palette(25.2)  # same integer PWM
            time.sleep(0.15)
            self.assertEqual(fake.writes, [18])
            output.request_palette(100.0)
            wait_for(lambda: output.snapshot().actual_pwm == USABLE_MAX)
            self.assertEqual(fake.writes[-1], USABLE_MAX)
            self.assertEqual(output.snapshot().target_pwm, USABLE_MAX)
        finally:
            output.stop()

    def test_failing_hardware_is_reported_once_and_ui_can_continue(self) -> None:
        fake = FakeHyperPixel(fail=True)
        output = LinkedBacklightOutput(fake, min_write_interval_s=0.1)
        output.start()
        try:
            output.request_palette(60.0)
            wait_for(lambda: output.snapshot().error is not None)
            self.assertIn("sudo", output.snapshot().error or "")
            self.assertIsNone(output.snapshot().actual_pwm)
            output.request_palette(100.0)
            time.sleep(0.15)
            self.assertEqual(len(fake.writes), 1)
        finally:
            output.stop()


if __name__ == "__main__":
    unittest.main()
