from __future__ import annotations

"""Small, deliberately bounded controller for the HyperPixel PWM backlight.

The driver exposes 0..255, but testing on Cali's HyperPixel showed the usable
range is 6..56. Values above 56 do not provide meaningful extra brightness;
values below 5 can blank the panel. Keep that policy here rather than asking
callers to rediscover it.
"""

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Final


HYPERPIXEL_BACKLIGHT_PATH: Final[Path] = Path("/sys/class/backlight/backlight/brightness")
HYPERPIXEL_MAX_PATH: Final[Path] = Path("/sys/class/backlight/backlight/max_brightness")
USABLE_MIN: Final[int] = 6
USABLE_MAX: Final[int] = 56


@dataclass(frozen=True)
class BacklightStatus:
    available: bool
    current: int | None
    hardware_max: int | None
    error: str | None = None


class HyperPixelBacklightController:
    """Control the HyperPixel PWM backlight without ever requesting unsafe ends.

    Direct writes are attempted first. On normal Raspberry Pi OS installations,
    sysfs backlight writes are root-only, so the fallback uses a narrowly scoped
    passwordless sudo permission installed by setup_backlight_control.sh.
    """

    def __init__(
        self,
        *,
        brightness_path: str | Path = HYPERPIXEL_BACKLIGHT_PATH,
        max_path: str | Path = HYPERPIXEL_MAX_PATH,
        usable_min: int = USABLE_MIN,
        usable_max: int = USABLE_MAX,
    ) -> None:
        self.brightness_path = Path(brightness_path)
        self.max_path = Path(max_path)
        self.usable_min = int(usable_min)
        self.usable_max = int(usable_max)
        if self.usable_min < 1 or self.usable_max < self.usable_min:
            raise ValueError("Invalid usable HyperPixel backlight range")

    def clamp(self, value: int | float) -> int:
        return max(self.usable_min, min(self.usable_max, int(round(float(value)))))

    def status(self) -> BacklightStatus:
        if not self.brightness_path.exists():
            return BacklightStatus(False, None, None, f"Missing {self.brightness_path}")
        try:
            current = int(self.brightness_path.read_text(encoding="utf-8").strip())
            hardware_max = int(self.max_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            return BacklightStatus(False, None, None, f"{type(exc).__name__}: {exc}")
        return BacklightStatus(True, current, hardware_max)

    def set(self, value: int | float) -> BacklightStatus:
        target = self.clamp(value)
        if not self.brightness_path.exists():
            return BacklightStatus(False, None, None, f"Missing {self.brightness_path}")

        payload = f"{target}\n"
        try:
            self.brightness_path.write_text(payload, encoding="utf-8")
        except PermissionError:
            # This is intentionally a fixed command and fixed sysfs target. The
            # one-time setup script creates the matching narrow sudoers rule.
            try:
                result = subprocess.run(
                    ["sudo", "-n", "/usr/bin/tee", str(self.brightness_path)],
                    input=payload,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=2.0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return BacklightStatus(False, None, None, f"Backlight write failed: {type(exc).__name__}: {exc}")
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "sudo permission not configured").strip()
                return BacklightStatus(
                    False,
                    None,
                    None,
                    "Backlight write needs setup_backlight_control.sh: " + detail,
                )
        except OSError as exc:
            return BacklightStatus(False, None, None, f"Backlight write failed: {type(exc).__name__}: {exc}")

        return self.status()
