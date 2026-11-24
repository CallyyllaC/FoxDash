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
        help="Enable linked ambient-lux dashboard palette, HyperPixel PWM backlight and LED intensity.",
    )
    power = run.add_argument_group("ignition power management")
    power.add_argument(
        "--enable-power-management",
        action="store_true",
        help="Watch the UPS external-power signal and shut down gracefully after a countdown",
    )
    power.add_argument("--power-gpio", type=int, default=10, help="BCM GPIO wired to the UPS DetcVIN signal (default: 10)")
    power.add_argument("--power-gpio-chip", default="/dev/gpiochip0", help="GPIO character device (default: /dev/gpiochip0)")
    power.add_argument(
        "--power-shutdown-delay",
        type=float,
        default=60.0,
        help="Seconds on capacitor power before shutdown is committed (default: 60)",
    )
    power.add_argument(
        "--power-debounce",
        type=float,
        default=0.25,
        help="Seconds a power-signal change must persist before it counts (default: 0.25)",
    )
    power.add_argument(
        "--power-dry-run",
        action="store_true",
        help="Run the countdown and shutdown decision but never stop FoxDash or power off",
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
    if args.power_gpio < 0:
        parser.error("--power-gpio must be a non-negative BCM number")
    if not 5.0 <= args.power_shutdown_delay <= 3600.0:
        parser.error("--power-shutdown-delay must be between 5 and 3600 seconds")
    if not 0.0 <= args.power_debounce <= 5.0:
        parser.error("--power-debounce must be between 0 and 5 seconds")

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
    power_manager = None
    if args.enable_power_management:
        from .power_manager import PowerConfig
        power_manager = _build_power_manager(PowerConfig(
            enabled=True,
            gpio_pin=args.power_gpio,
            gpio_chip=args.power_gpio_chip,
            shutdown_delay_s=args.power_shutdown_delay,
            dry_run=args.power_dry_run,
            debounce_s=args.power_debounce,
        ), runtime=runtime, app_ref=lambda: app)
    app = FoxDashApp(
        state_store=runtime.store,
        refresh_hz=args.refresh_hz,
        show_frame_counter=not args.no_frame_counter,
        layout_mode=args.layout,
        emoji_mode=not args.no_emoji,
        ui_brightness=args.ui_brightness,
        use_ambient_brightness=args.enable_ambient_brightness,
        power_status=power_manager.status if power_manager is not None else None,
    )
    if power_manager is not None:
        power_manager.start()
    try:
        app.run()
    finally:
        app.stop_brightness_output()
        runtime.stop()
        if power_manager is not None:
            power_manager.stop()
    return 0


def _build_power_manager(config, *, runtime, app_ref):
    """Wire the ignition monitor to FoxDash's logger and teardown.

    Power events go to the session text log (existing architecture) and to
    the system journal, which outlives the session file and records the final
    shutdown request after the session logger has been closed.
    """
    import syslog

    from .backlight import HyperPixelBacklightController
    from .power_manager import PowerEventLog, PowerManager, ShutdownSequence, SystemPower

    syslog.openlog("foxdash-power", 0, syslog.LOG_USER)
    # Durable across boots (the Pi's journal is RAM-only); outside the psa_*
    # names, so journey archiving leaves it in place.
    events = PowerEventLog(runtime.logger.log_dir / "power_events.log", session_id=runtime.session_id)

    def log(message: str) -> None:
        events.write(message)
        runtime.logger.note(f"{PowerEventLog.timestamp()} {message}")
        try:
            syslog.syslog(syslog.LOG_NOTICE, message)
        except Exception:
            pass

    system = SystemPower()
    manager: PowerManager

    def stop_application() -> None:
        # Stop the backlight writer first so it cannot undo the dim below.
        app = app_ref()
        if app is not None:
            app.stop_brightness_output()
        # Reader, ambient sensor and logs; the LED worker blanks the BlinkStick.
        runtime.stop()

    def dim_display() -> None:
        status = HyperPixelBacklightController().set(0)  # clamps to the safe minimum
        if not status.available:
            log(f"Power: backlight dim unavailable: {status.error}")

    sequence = ShutdownSequence(
        stop_application=stop_application,
        dim_display=dim_display,
        read_external_power=lambda: manager.read_now(),
        system=system,
        log=log,
        set_detail=lambda detail: manager.set_detail(detail),
    )
    manager = PowerManager(config, log=log, on_commit=sequence, startup_probe=system.describe_permissions)
    return manager


if __name__ == "__main__":
    raise SystemExit(main())
