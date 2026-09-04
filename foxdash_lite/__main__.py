from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="FoxDash runtime")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the dashboard with a managed source")
    run.add_argument("--refresh-hz", type=float, default=10.0)
    run.add_argument("--source", choices=["live", "replay", "sweep"], default="live")
    run.add_argument("--log", default=None, help="CSV path required for replay")
    run.add_argument("--replay-speed", type=float, default=1.0)
    run.add_argument("--no-random-start", action="store_true")
    run.add_argument("--layout", choices=["compact", "relaxed"], default="compact")
    run.add_argument("--no-frame-counter", action="store_true")
    run.add_argument("--no-emoji", action="store_true")
    run.add_argument("--ui-brightness", type=float, default=100.0)
    run.add_argument("--log-dir", default=None, help="Optional session-log directory")
    run.add_argument("--enable-leds", action="store_true", help="Enable the 24-pixel RGBW BlinkStick bar")
    run.add_argument("--reverse-leds", action="store_true", help="Reverse physical LED-bar direction after the census test")
    run.add_argument(
        "--led-width-cap",
        type=float,
        default=0.50,
        help="Maximum mood-band width as a 0.05..1.0 fraction of the bar (default: 0.50)",
    )
    run.add_argument("--disable-i2c", action="store_true", help="Disable the BH1750 ambient-light worker")
    run.add_argument("--i2c-bus", type=int, default=11, help="BH1750 I²C bus (HyperPixel alternate header: 11)")
    run.add_argument(
        "--bh1750-address",
        type=lambda value: int(value, 0),
        default=0x23,
        help="BH1750 7-bit I²C address (default: 0x23)",
    )
    run.add_argument("--ambient-poll-seconds", type=float, default=1.0, help="Ambient-light sample interval in seconds")
    run.add_argument(
        "--enable-ambient-brightness",
        action="store_true",
        help="Apply ambient lux to LED brightness. Leave off while collecting calibration logs.",
    )

    calibrate = sub.add_parser("ambient-calibrate", help="Manually tune the real HyperPixel backlight against live BH1750 readings")
    calibrate.add_argument("--i2c-bus", type=int, default=11, help="BH1750 I²C bus (HyperPixel alternate header: 11)")
    calibrate.add_argument(
        "--bh1750-address",
        type=lambda value: int(value, 0),
        default=0x23,
        help="BH1750 7-bit I²C address (default: 0x23)",
    )
    calibrate.add_argument("--poll-seconds", type=float, default=0.25, help="BH1750 sample interval while calibrating (default: 0.25)")
    calibrate.add_argument("--filter-tau-seconds", type=float, default=0.8, help="Smoothing time constant shown beside raw lux (default: 0.8)")
    calibrate.add_argument("--start-backlight", "--start-palette-brightness", dest="start_backlight", type=int, default=56, help="Initial real HyperPixel backlight 6..56 (default: 56)")
    calibrate.add_argument("--points-file", default=None, help="Optional CSV destination for saved comfort anchors")

    conv = sub.add_parser("convert-log", help="Convert/re-score decoded or UI CSV into current UI display CSV")
    conv.add_argument("input")
    conv.add_argument("output")
    conv.add_argument("--max-rows", type=int, default=None)

    args = parser.parse_args()
    if args.command == "ambient-calibrate":
        if args.i2c_bus < 0:
            parser.error("--i2c-bus must be non-negative")
        if not 0 <= args.bh1750_address <= 0x7F:
            parser.error("--bh1750-address must be a 7-bit I²C address")
        if args.poll_seconds < 0.2:
            parser.error("--poll-seconds must be at least 0.2")
        if args.filter_tau_seconds <= 0:
            parser.error("--filter-tau-seconds must be greater than zero")
        if not 6 <= args.start_backlight <= 56:
            parser.error("--start-backlight must be between 6 and 56")
        from .ambient_calibration import run_ambient_calibration
        return run_ambient_calibration(
            bus_number=args.i2c_bus,
            address=args.bh1750_address,
            poll_interval_s=args.poll_seconds,
            filter_tau_s=args.filter_tau_seconds,
            start_backlight=args.start_backlight,
            points_path=args.points_file,
        )

    if args.command == "convert-log":
        from .conversion import convert_decoded_csv_to_display_csv
        count = convert_decoded_csv_to_display_csv(args.input, args.output, max_rows=args.max_rows)
        print(f"Converted {count} rows -> {args.output}")
        return 0

    if args.command is None:
        args = parser.parse_args(["run"])

    if args.source == "replay" and not args.log:
        parser.error("--source replay requires --log PATH")
    if not 0.05 <= args.led_width_cap <= 1.0:
        parser.error("--led-width-cap must be between 0.05 and 1.0")
    if args.i2c_bus < 0:
        parser.error("--i2c-bus must be non-negative")
    if not 0 <= args.bh1750_address <= 0x7F:
        parser.error("--bh1750-address must be a 7-bit I²C address")
    if args.ambient_poll_seconds < 0.2:
        parser.error("--ambient-poll-seconds must be at least 0.2")

    from .app import FoxDashApp
    from .runtime import FoxDashRuntime, RuntimeConfig

    runtime = FoxDashRuntime(RuntimeConfig(
        source=args.source,
        replay_log=args.log,
        replay_random_start=not args.no_random_start,
        replay_speed=args.replay_speed,
        log_dir=args.log_dir,
        enable_leds=args.enable_leds,
        led_reverse=args.reverse_leds,
        led_max_band_width_fraction=args.led_width_cap,
        enable_i2c=not args.disable_i2c,
        i2c_bus=args.i2c_bus,
        bh1750_address=args.bh1750_address,
        ambient_poll_interval_s=args.ambient_poll_seconds,
        enable_ambient_brightness=args.enable_ambient_brightness,
    ))
    runtime.start()
    app = FoxDashApp(
        state_store=runtime.store,
        refresh_hz=args.refresh_hz,
        show_frame_counter=not args.no_frame_counter,
        layout_mode=args.layout,
        emoji_mode=not args.no_emoji,
        ui_brightness=args.ui_brightness,
    )
    try:
        app.run()
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
