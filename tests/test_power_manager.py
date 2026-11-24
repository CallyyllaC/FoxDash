from __future__ import annotations

import gc
import threading
import time
import tracemalloc
import unittest

from foxdash_lite.power_manager import (
    PowerConfig,
    PowerEvent,
    PowerManager,
    PowerState,
    PowerStateMachine,
    ShutdownSequence,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGpio:
    """Scriptable DetcVIN line. ``value`` True = external power present."""

    def __init__(self, value: bool = True) -> None:
        self.value = value
        self.fail_reads = False
        self.reads = 0
        self.closed = False

    def read(self) -> bool:
        self.reads += 1
        if self.fail_reads:
            raise OSError("simulated GPIO read failure")
        return self.value

    def close(self) -> None:
        self.closed = True


class FakeSystem:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def poweroff(self) -> tuple[bool, str]:
        self.calls.append("poweroff")
        return True, "ok"

    def reboot(self) -> tuple[bool, str]:
        self.calls.append("reboot")
        return True, "ok"


class Harness:
    """PowerManager driven synchronously with a fake clock (no thread)."""

    def __init__(self, *, delay: float = 60.0, debounce: float = 0.25, dry_run: bool = False,
                 initial: bool = True, open_fails: bool = False) -> None:
        self.clock = FakeClock()
        self.gpio = FakeGpio(initial)
        self.logs: list[str] = []
        self.commits = 0
        self.open_fails = open_fails
        self.opens = 0

        def factory() -> FakeGpio:
            self.opens += 1
            if self.open_fails:
                raise PermissionError("simulated /dev/gpiochip0 permission denied")
            return self.gpio

        def on_commit() -> None:
            self.commits += 1

        self.manager = PowerManager(
            PowerConfig(enabled=True, shutdown_delay_s=delay, debounce_s=debounce, dry_run=dry_run),
            log=self.logs.append,
            on_commit=on_commit,
            gpio_factory=factory,
            clock=self.clock,
        )

    def run(self, seconds: float, step: float = 0.1) -> list[PowerEvent]:
        events: list[PowerEvent] = []
        ticks = int(round(seconds / step))
        for _ in range(ticks):
            self.clock.advance(step)
            events.extend(self.manager.poll_once())
        return events

    def set_power(self, present: bool) -> None:
        self.gpio.value = present

    @property
    def status(self):
        return self.manager.status()


class PowerStateMachineTests(unittest.TestCase):
    def test_debounce_rejects_short_glitches(self) -> None:
        machine = PowerStateMachine(shutdown_delay_s=60, debounce_s=0.25, dry_run=False)
        self.assertEqual(machine.observe(True, 0.0), [])
        self.assertEqual(machine.observe(True, 0.3), [PowerEvent.POWER_PRESENT])
        # 0.2 s dip is shorter than the debounce: ignored entirely.
        machine.observe(False, 1.0)
        machine.observe(False, 1.2)
        machine.observe(True, 1.21)
        machine.observe(True, 2.0)
        self.assertIs(machine.state, PowerState.NORMAL)
        self.assertEqual(machine.countdown_id, 0)

    def test_countdown_measured_from_first_low_sample(self) -> None:
        machine = PowerStateMachine(shutdown_delay_s=60, debounce_s=0.25, dry_run=False)
        machine.observe(True, 0.0)
        machine.observe(True, 0.5)
        machine.observe(False, 10.0)
        events = machine.observe(False, 10.3)
        self.assertIn(PowerEvent.COUNTDOWN_STARTED, events)
        self.assertAlmostEqual(machine.remaining(10.3), 59.7, places=6)

    def test_unknown_reading_never_starts_countdown(self) -> None:
        machine = PowerStateMachine(shutdown_delay_s=60, debounce_s=0.25, dry_run=False)
        for t in range(100):
            self.assertEqual(machine.observe(None, float(t)), [])
        self.assertIs(machine.state, PowerState.NORMAL)
        self.assertIsNone(machine.external_power)


class PowerManagerTests(unittest.TestCase):
    def test_normal_operation_with_external_power(self) -> None:
        h = Harness()
        events = h.run(30)
        self.assertEqual(events, [PowerEvent.POWER_PRESENT])
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertTrue(h.status.external_power)
        self.assertTrue(h.status.gpio_ok)
        self.assertIsNone(h.status.remaining_s)
        # One log line for the transition, none per poll (300 polls).
        self.assertEqual(len([line for line in h.logs if line.startswith("Power")]), 1)

    def test_power_loss_starts_countdown_and_progresses(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        events = h.run(0.5)
        self.assertIn(PowerEvent.POWER_LOST, events)
        self.assertIn(PowerEvent.COUNTDOWN_STARTED, events)
        self.assertIs(h.status.state, PowerState.PENDING)
        first = h.status.remaining_s
        self.assertAlmostEqual(first, 59.6, delta=0.11)
        h.run(20)
        self.assertAlmostEqual(h.status.remaining_s, first - 20, delta=0.11)
        self.assertLess(h.status.progress, 0.7)
        self.assertEqual(h.commits, 0)

    def test_restoration_cancels_immediately_and_resets(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        h.run(30)
        h.set_power(True)
        # Debounce (0.25 s) at 10 Hz polling: confirmed on the 3rd-4th poll.
        events = h.run(0.4)
        self.assertIn(PowerEvent.POWER_RESTORED, events)
        self.assertIn(PowerEvent.COUNTDOWN_CANCELLED, events)
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertIsNone(h.status.remaining_s)
        h.run(120)  # long after the old deadline: nothing fires
        self.assertEqual(h.commits, 0)

    def test_repeated_interruptions_start_fresh_countdowns(self) -> None:
        h = Harness()
        h.run(1)
        for cycle in range(1, 51):
            h.set_power(False)
            h.run(45)
            self.assertIs(h.status.state, PowerState.PENDING)
            self.assertEqual(h.status.countdown_id, cycle)
            h.set_power(True)
            h.run(1)
            self.assertIs(h.status.state, PowerState.NORMAL)
        h.set_power(False)
        h.run(0.4)
        # Fresh, full-length countdown after 50 cancellations.
        self.assertAlmostEqual(h.status.remaining_s, 59.7, delta=0.11)
        self.assertEqual(h.commits, 0)

    def test_rapid_transitions_do_not_restart_or_cancel(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        h.run(10)
        deadline_remaining = h.status.remaining_s
        # Chatter: alternate every 0.1 s for 5 s while on capacitors.
        for _ in range(25):
            h.set_power(True)
            h.run(0.1)
            h.set_power(False)
            h.run(0.1)
        self.assertIs(h.status.state, PowerState.PENDING)
        self.assertEqual(h.status.countdown_id, 1)
        self.assertAlmostEqual(h.status.remaining_s, deadline_remaining - 5.0, delta=0.11)

    def test_rapid_transitions_while_powered_do_not_start_countdown(self) -> None:
        h = Harness()
        h.run(1)
        for _ in range(50):
            h.set_power(False)
            h.run(0.1)
            h.set_power(True)
            h.run(0.1)
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertEqual(h.status.countdown_id, 0)

    def test_countdown_expiry_commits_once(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        events = h.run(61)
        self.assertIn(PowerEvent.COMMITTED, events)
        self.assertIs(h.status.state, PowerState.COMMITTED)
        self.assertEqual(h.commits, 1)
        # Power keeps flapping, time keeps passing: never requested again.
        h.set_power(True)
        h.run(5)
        h.set_power(False)
        h.run(120)
        self.assertEqual(h.commits, 1)
        self.assertIs(h.status.state, PowerState.COMMITTED)

    def test_restoration_immediately_before_expiry(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        h.run(59.5)  # ~0.4 s left (countdown started at first LOW sample)
        self.assertIs(h.status.state, PowerState.PENDING)
        h.set_power(True)
        h.run(1.0)
        self.assertEqual(h.commits, 0)
        self.assertIs(h.status.state, PowerState.NORMAL)

    def test_high_reading_at_expiry_withholds_commit(self) -> None:
        # Power returns exactly as the deadline passes but has not yet been
        # debounced: the fresh recheck must refuse to commit.
        h = Harness(debounce=0.5)
        h.run(1)
        h.set_power(False)
        h.run(59.9)
        self.assertIs(h.status.state, PowerState.PENDING)
        self.assertLess(h.status.remaining_s, 0.3)
        h.set_power(True)
        events = h.run(0.3)
        self.assertIn(PowerEvent.EXPIRY_AWAITING_LOW, events)
        self.assertEqual(h.commits, 0)
        h.run(1)
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertEqual(h.commits, 0)

    def test_startup_without_external_power(self) -> None:
        h = Harness(initial=False)
        events = h.run(0.5)
        self.assertEqual(events[:2], [PowerEvent.POWER_LOST, PowerEvent.COUNTDOWN_STARTED])
        self.assertIs(h.status.state, PowerState.PENDING)
        h.run(61)
        self.assertEqual(h.commits, 1)

    def test_gpio_open_failure_is_logged_not_power_loss(self) -> None:
        h = Harness(open_fails=True)
        h.run(30)
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertFalse(h.status.gpio_ok)
        self.assertIn("permission denied", h.status.gpio_error)
        self.assertEqual(h.commits, 0)
        # Retries with back-off, logs once.
        self.assertLessEqual(h.opens, 16)
        self.assertEqual(len([line for line in h.logs if "error" in line]), 1)
        h.open_fails = False
        h.run(3)
        self.assertTrue(h.status.gpio_ok)
        self.assertTrue(any("recovered" in line for line in h.logs))

    def test_gpio_read_failure_during_countdown_blocks_commit(self) -> None:
        h = Harness()
        h.run(1)
        h.set_power(False)
        h.run(30)
        h.gpio.fail_reads = True
        events = h.run(40)
        self.assertIn(PowerEvent.EXPIRY_AWAITING_READ, events)
        self.assertEqual(h.commits, 0)
        self.assertIs(h.status.state, PowerState.PENDING)
        self.assertEqual(h.status.remaining_s, 0.0)
        self.assertEqual(len([line for line in h.logs if "read failed" in line]), 1)
        h.gpio.fail_reads = False
        h.run(3)  # reopen after back-off, read LOW, commit
        self.assertEqual(h.commits, 1)

    def test_gpio_failure_while_powered_never_shuts_down(self) -> None:
        h = Harness()
        h.run(1)
        h.gpio.fail_reads = True
        h.run(300)
        self.assertIs(h.status.state, PowerState.NORMAL)
        self.assertEqual(h.commits, 0)

    def test_dry_run_exercises_decision_without_commit_handler(self) -> None:
        h = Harness(dry_run=True)
        h.run(1)
        h.set_power(False)
        events = h.run(61)
        self.assertIn(PowerEvent.COMMITTED, events)
        self.assertIs(h.status.state, PowerState.COMMITTED)
        self.assertTrue(h.status.dry_run)
        self.assertEqual(h.commits, 0)
        self.assertTrue(any("DRY RUN" in line for line in h.logs))
        # Restoring power resets the test so it can be repeated.
        h.set_power(True)
        events = h.run(0.5)
        self.assertIn(PowerEvent.DRY_RUN_RESET, events)
        self.assertIs(h.status.state, PowerState.NORMAL)
        h.set_power(False)
        h.run(0.5)
        self.assertEqual(h.status.countdown_id, 2)
        self.assertIs(h.status.state, PowerState.PENDING)

    def test_configurable_countdown_duration(self) -> None:
        for delay in (5.0, 15.0, 90.0):
            with self.subTest(delay=delay):
                h = Harness(delay=delay)
                h.run(1)
                h.set_power(False)
                h.run(delay - 1)
                self.assertEqual(h.commits, 0)
                h.run(2)
                self.assertEqual(h.commits, 1)

    def test_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            PowerConfig(shutdown_delay_s=1)
        with self.assertRaises(ValueError):
            PowerConfig(gpio_pin=-1)
        self.assertEqual(PowerConfig().gpio_pin, 10)
        self.assertEqual(PowerConfig().shutdown_delay_s, 60.0)

    def test_repeated_cycles_do_not_grow_memory(self) -> None:
        h = Harness()
        h.run(1)

        def cycles(count: int) -> None:
            for _ in range(count):
                h.set_power(False)
                h.run(5)
                h.set_power(True)
                h.run(1)

        cycles(20)
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        cycles(200)
        gc.collect()
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
        # Logs list grows by design in this harness; allow for it, nothing else.
        self.assertLess(growth, 200 * 2 * 200)

    def test_worker_thread_starts_and_stops_cleanly(self) -> None:
        gpio = FakeGpio(True)
        logs: list[str] = []
        manager = PowerManager(
            PowerConfig(enabled=True, poll_interval_s=0.02),
            log=logs.append,
            gpio_factory=lambda: gpio,
            startup_probe=lambda: "yes",
        )
        before = threading.active_count()
        manager.start()
        deadline = time.monotonic() + 2.0
        while manager.status().external_power is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(manager.status().external_power)
        manager.stop()
        manager.stop()  # idempotent
        self.assertEqual(threading.active_count(), before)
        self.assertTrue(gpio.closed)
        self.assertTrue(any("shutdown permission: yes" in line for line in logs))


class ShutdownSequenceTests(unittest.TestCase):
    def _sequence(self, readings: list[bool | None]):
        calls: list[str] = []
        system = FakeSystem()
        values = iter(readings)
        sequence = ShutdownSequence(
            stop_application=lambda: calls.append("stop"),
            dim_display=lambda: calls.append("dim"),
            read_external_power=lambda: next(values, False),
            system=system,  # type: ignore[arg-type]
            log=lambda _message: None,
            sleep=lambda _s: None,
        )
        return sequence, calls, system

    def test_order_and_exactly_once(self) -> None:
        sequence, calls, system = self._sequence([False])
        sequence()
        sequence()
        self.assertEqual(calls, ["stop", "dim"])
        self.assertEqual(system.calls, ["poweroff"])
        self.assertEqual(sequence.outcome, "poweroff")

    def test_power_returned_during_preparation_reboots(self) -> None:
        sequence, _calls, system = self._sequence([True, True, True])
        sequence()
        self.assertEqual(system.calls, ["reboot"])

    def test_unconfirmed_restoration_still_powers_off(self) -> None:
        for readings in ([True, None, True], [True, False, True], [None]):
            with self.subTest(readings=readings):
                sequence, _calls, system = self._sequence(readings)
                sequence()
                self.assertEqual(system.calls, ["poweroff"])

    def test_failures_in_preparation_do_not_prevent_poweroff(self) -> None:
        system = FakeSystem()

        def boom() -> None:
            raise RuntimeError("logger exploded")

        logs: list[str] = []
        sequence = ShutdownSequence(
            stop_application=boom,
            dim_display=boom,
            read_external_power=lambda: False,
            system=system,  # type: ignore[arg-type]
            log=logs.append,
            sleep=lambda _s: None,
        )
        sequence()
        self.assertEqual(system.calls, ["poweroff"])
        self.assertTrue(any("stop application failed" in line for line in logs))

    def test_concurrent_calls_run_once(self) -> None:
        sequence, calls, system = self._sequence([False] * 10)
        threads = [threading.Thread(target=sequence) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(system.calls, ["poweroff"])
        self.assertEqual(calls, ["stop", "dim"])


class PowerEventLogTests(unittest.TestCase):
    def test_appends_timestamped_lines_across_instances(self) -> None:
        import tempfile
        from pathlib import Path
        from foxdash_lite.power_manager import PowerEventLog

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "power_events.log"
            PowerEventLog(path, session_id="S1").write("Power: external power lost")
            PowerEventLog(path, session_id="S2").write("Power: requesting systemctl poweroff\n")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}.* S1 Power: external power lost$")
            self.assertTrue(lines[1].endswith("S2 Power: requesting systemctl poweroff"))

    def test_unwritable_path_never_raises(self) -> None:
        from foxdash_lite.power_manager import PowerEventLog
        PowerEventLog("\0invalid/power_events.log").write("ignored")


class FakeRunner:
    """Records argv and answers per-command return codes; never executes."""

    def __init__(self, codes: dict[str, int]) -> None:
        self.codes = codes
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        import subprocess
        self.calls.append(list(argv))
        key = "sudo" if argv[0].endswith("sudo") else argv[0].rsplit("/", 1)[-1]
        code = self.codes.get(key, 0)
        return subprocess.CompletedProcess(argv, code, stdout='s "challenge"' if key == "busctl" else "",
                                           stderr="" if code == 0 else "Access denied")


class SystemPowerTests(unittest.TestCase):
    def test_logind_first_without_sudo(self) -> None:
        from foxdash_lite.power_manager import SystemPower
        runner = FakeRunner({"systemctl": 0})
        ok, detail = SystemPower(runner).poweroff()
        self.assertTrue(ok)
        self.assertEqual(runner.calls, [["/usr/bin/systemctl", "--no-ask-password", "poweroff"]])
        self.assertIn("logind", detail)

    def test_sudo_fallback_when_polkit_challenges(self) -> None:
        from foxdash_lite.power_manager import SystemPower
        runner = FakeRunner({"systemctl": 1, "sudo": 0})
        ok, detail = SystemPower(runner).reboot()
        self.assertTrue(ok)
        self.assertEqual(runner.calls[-1], ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reboot"])
        self.assertIn("sudo accepted", detail)

    def test_both_refused_reports_failure(self) -> None:
        from foxdash_lite.power_manager import SystemPower
        ok, detail = SystemPower(FakeRunner({"systemctl": 1, "sudo": 1})).poweroff()
        self.assertFalse(ok)
        self.assertIn("sudo refused", detail)

    def test_permission_probe_requires_nopasswd_rule_and_never_executes(self) -> None:
        import subprocess
        from foxdash_lite.power_manager import SystemPower

        def runner_for(listing: str):
            calls: list[list[str]] = []

            def run(argv, **_kwargs):
                calls.append(list(argv))
                if argv[0].endswith("busctl"):
                    return subprocess.CompletedProcess(argv, 0, stdout='s "challenge"', stderr="")
                return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")
            return run, calls

        admin_only = "User awo may run the following commands on awocore:\n    (ALL : ALL) ALL\n"
        run, calls = runner_for(admin_only)
        self.assertIn("sudo fallback=NOT configured", SystemPower(run).describe_permissions())

        with_rule = admin_only + "    (root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot\n"
        run, calls = runner_for(with_rule)
        text = SystemPower(run).describe_permissions()
        self.assertIn("CanPowerOff=challenge", text)
        self.assertIn("sudo fallback=available", text)
        for argv in calls:
            self.assertNotIn("poweroff", argv)
            self.assertNotIn("reboot", argv)


if __name__ == "__main__":
    unittest.main()
