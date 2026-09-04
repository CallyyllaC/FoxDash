#!/usr/bin/env python3
"""FoxDash 24-pixel RGBW LED bar bench test.

This is intentionally separate from the live dashboard. It speaks raw GRBW to
one BlinkStick Pro channel so we can prove the optics, physical direction,
colour order, and brightness behaviour before the telemetry mapping gets near
it. Civilisation has enough accidental integrations already.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse
import math
import sys
import time
from typing import Callable, Iterable, Sequence

from foxdash_lite.blinkstick_rgbw import BlinkStickProRgbw, RGBW

LED_COUNT = 24
FPS = 40.0

BLACK: RGBW = (0, 0, 0, 0)
RED: RGBW = (255, 0, 0, 0)
GREEN: RGBW = (0, 255, 0, 0)
BLUE: RGBW = (0, 0, 255, 0)
WHITE: RGBW = (0, 0, 0, 255)
WARM_WHITE: RGBW = (70, 20, 0, 180)
FUCHSIA: RGBW = (255, 0, 125, 0)
CYAN: RGBW = (0, 210, 255, 0)
VIOLET: RGBW = (95, 0, 180, 0)
AMBER: RGBW = (255, 76, 0, 0)

Frame = list[RGBW]


def clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def scale(colour: RGBW, amount: float) -> RGBW:
    return tuple(clamp(channel * amount) for channel in colour)  # type: ignore[return-value]


def add(first: RGBW, second: RGBW) -> RGBW:
    return tuple(clamp(a + b) for a, b in zip(first, second))  # type: ignore[return-value]


def blank(count: int) -> Frame:
    return [BLACK] * count


def show_frame(strip: BlinkStickProRgbw, frame: Sequence[RGBW], seconds: float) -> None:
    strip.set_frame(frame)
    strip.show()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(min(0.05, end - time.monotonic()))


def channel_witness(strip: BlinkStickProRgbw) -> None:
    print("\n[1/4] Channel witness: R, G, B, dedicated W, warm mix, off")
    stages: tuple[tuple[str, RGBW], ...] = (
        ("RED", RED),
        ("GREEN", GREEN),
        ("BLUE", BLUE),
        ("WHITE CHANNEL", WHITE),
        ("WARM MIX", WARM_WHITE),
        ("OFF", BLACK),
    )
    for label, colour in stages:
        print(f"  {label}")
        show_frame(strip, [colour] * strip.led_count, 1.2)


def pixel_census(strip: BlinkStickProRgbw, passes: int = 2) -> None:
    print("\n[2/4] Pixel census: one physical RGBW pixel walks the entire bar")
    colours = (RED, GREEN, BLUE, WHITE)
    for step in range(strip.led_count * passes):
        index = step % strip.led_count
        colour = colours[(step // strip.led_count) % len(colours)]
        frame = blank(strip.led_count)
        frame[index] = colour
        for tail in range(1, 4):
            previous = index - tail
            if previous >= 0:
                frame[previous] = scale(colour, 0.18 / tail)
        show_frame(strip, frame, 0.14)
    show_frame(strip, blank(strip.led_count), 0.4)


def status_bar(strip: BlinkStickProRgbw, seconds: float = 7.5) -> None:
    print("\n[3/4] Status-bar simulation: colour, width, and position behaviour")
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < seconds:
        frame = blank(strip.led_count)
        # Mood colour cycles: green -> cyan -> fuchsia -> amber -> red.
        hue = (math.sin(elapsed * 0.55) + 1.0) * 0.5
        mood = add(scale(CYAN, 1.0 - hue), scale(FUCHSIA, hue))
        efficiency = 0.18 + 0.78 * ((math.sin(elapsed * 0.85) + 1.0) * 0.5)
        lit = max(1, min(strip.led_count, int(round(efficiency * strip.led_count))))
        for index in range(lit):
            taper = 0.48 + 0.52 * (index / max(1, lit - 1))
            frame[index] = scale(mood, taper)
        # A user-guidance marker moves independently of width.
        marker = min(strip.led_count - 1, int(((math.sin(elapsed * 1.1) + 1.0) * 0.5) * (strip.led_count - 1)))
        frame[marker] = add(frame[marker], scale(WHITE, 0.60))
        strip.set_frame(frame)
        strip.show()
        strip.wait_for_next_frame()
    show_frame(strip, blank(strip.led_count), 0.4)


def foxfire(strip: BlinkStickProRgbw, seconds: float = 8.0) -> None:
    print("\n[4/4] Foxfire: diffuse gradient / hotspot / brightness sanity check")
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < seconds:
        frame: Frame = []
        for index in range(strip.led_count):
            position = index / max(1, strip.led_count - 1)
            fuchsia = ((math.sin((position * 2.3 - elapsed * 0.30) * math.tau) + 1.0) * 0.5) ** 3
            cyan = ((math.sin((position * 3.2 + elapsed * 0.22) * math.tau + 1.1) + 1.0) * 0.5) ** 4
            colour = add(scale(VIOLET, 0.06), scale(FUCHSIA, fuchsia * 0.78))
            colour = add(colour, scale(CYAN, cyan * 0.62))
            if fuchsia + cyan > 1.45:
                colour = add(colour, scale(WHITE, 0.18))
            frame.append(colour)
        strip.set_frame(frame)
        strip.show()
        strip.wait_for_next_frame()
    show_frame(strip, blank(strip.led_count), 0.4)


def run_all(strip: BlinkStickProRgbw) -> None:
    channel_witness(strip)
    pixel_census(strip)
    status_bar(strip)
    foxfire(strip)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bench-test the 24-LED FoxDash RGBW bar through a BlinkStick Pro.")
    parser.add_argument(
        "--test",
        choices=("all", "channels", "census", "status", "foxfire"),
        default="all",
        help="Test to run (default: all)",
    )
    parser.add_argument("--loop", action="store_true", help="Repeat the selected test until Ctrl+C")
    parser.add_argument("--leds", type=int, default=LED_COUNT, help="Physical RGBW LED count (default: 24)")
    parser.add_argument("--brightness", type=float, default=0.55, help="User brightness 0.0-1.0 before USB safety cap (default: 0.55)")
    parser.add_argument("--usb-ma", type=float, default=300.0, help="Conservative USB LED budget in mA (default: 300)")
    parser.add_argument("--pixel-ma", type=float, default=50.0, help="Estimated RGBW full-white mA per pixel (default: 50)")
    args = parser.parse_args()

    if args.leds <= 0:
        parser.error("--leds must be positive")
    if not 0.0 <= args.brightness <= 1.0:
        parser.error("--brightness must be 0.0-1.0")

    strip = BlinkStickProRgbw(
        args.leds,
        fps=FPS,
        usb_ma_budget=args.usb_ma,
        estimated_pixel_max_ma=args.pixel_ma,
    )
    strip.set_brightness(args.brightness)

    actions: dict[str, Callable[[BlinkStickProRgbw], None]] = {
        "all": run_all,
        "channels": channel_witness,
        "census": pixel_census,
        "status": status_bar,
        "foxfire": foxfire,
    }

    print("FOXDASH LED BAR BENCH TEST")
    print(f"Physical pixels: {args.leds} RGBW  | raw payload: {args.leds * 4} GRBW bytes on BlinkStick R")
    print(f"USB cap: {strip.usb_limit * 100:.1f}% | user brightness: {args.brightness * 100:.0f}% | effective: {strip.effective_brightness * 100:.1f}%")
    print("Ctrl+C always turns the strip off. Remarkable restraint from modern electronics.")

    try:
        strip.connect()
        while True:
            actions[args.test](strip)
            if not args.loop:
                break
            print("\nRepeating in 1 second. Ctrl+C to stop.")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping test.")
    except Exception as exc:
        print(f"\nLED test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            strip.close()
        except Exception:
            pass
        print("Strip off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
