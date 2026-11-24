from __future__ import annotations

import asyncio
import io
import time
import unittest

from rich.console import Console

from foxdash_lite.app import FoxDashApp
from foxdash_lite.power_manager import PowerState, PowerStatus
from foxdash_lite.state_store import DashboardStateStore


def _plain(renderable, width: int = 60) -> str:
    console = Console(width=width, color_system=None, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


class PowerSource:
    """Mutable stand-in for PowerManager.status()."""

    def __init__(self) -> None:
        self.status = PowerStatus(enabled=True, state=PowerState.NORMAL, external_power=True, gpio_ok=True)

    def __call__(self) -> PowerStatus:
        return self.status

    def pending(self, remaining: float, countdown_id: int = 1, *, dry_run: bool = False) -> None:
        self.status = PowerStatus(
            enabled=True, state=PowerState.PENDING, external_power=False, remaining_s=remaining,
            shutdown_delay_s=60.0, gpio_ok=True, countdown_id=countdown_id, dry_run=dry_run,
        )

    def normal(self) -> None:
        self.status = PowerStatus(enabled=True, state=PowerState.NORMAL, external_power=True, gpio_ok=True)


class PowerBannerRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FoxDashApp(state_store=DashboardStateStore())

    def test_pending_banner_text_and_rail(self) -> None:
        status = PowerStatus(enabled=True, state=PowerState.PENDING, remaining_s=41.2, shutdown_delay_s=60.0)
        text = _plain(self.app.render_power_banner(status, width=40), width=40)
        self.assertIn("EXTERNAL POWER LOST", text)
        self.assertIn("shutdown in 42s", text)
        self.assertNotIn("DRY RUN", text)
        rail = self.app._power_rail(status.progress, 40, urgent=False)
        self.assertEqual(rail.cell_len, 40)

    def test_rail_drains_proportionally(self) -> None:
        full = self.app._power_rail(1.0, 50, urgent=False)
        half = self.app._power_rail(0.5, 50, urgent=False)
        empty = self.app._power_rail(0.0, 50, urgent=True)
        for rail in (full, half, empty):
            self.assertEqual(rail.cell_len, 50)
        self.assertEqual(len(half.spans), 3)  # fill, leading edge, track
        self.assertEqual(half.spans[0].end + 1, 25)

    def test_dry_run_labels(self) -> None:
        status = PowerStatus(enabled=True, state=PowerState.PENDING, remaining_s=10.0, dry_run=True)
        self.assertIn("DRY RUN", _plain(self.app.render_power_banner(status, width=60)))
        fired = PowerStatus(enabled=True, state=PowerState.COMMITTED, dry_run=True)
        text = _plain(self.app.render_power_banner(fired, width=60))
        self.assertIn("SHUTDOWN WOULD START NOW", text)

    def test_urgent_class_in_final_seconds(self) -> None:
        self.assertEqual(self.app._power_banner_class(PowerStatus(state=PowerState.PENDING, remaining_s=30)), "feedback-power")
        self.assertEqual(self.app._power_banner_class(PowerStatus(state=PowerState.PENDING, remaining_s=9.5)), "feedback-power-urgent")
        self.assertEqual(self.app._power_banner_class(PowerStatus(state=PowerState.COMMITTED)), "feedback-power-urgent")
        self.assertEqual(self.app._power_banner_class(PowerStatus(state=PowerState.COMMITTED, dry_run=True)), "feedback-power")

    def test_gpio_error_indicator_in_status_bar(self) -> None:
        snap = self.app.state_store.latest().telemetry
        bad = PowerStatus(enabled=True, gpio_ok=False, gpio_error="open failed")
        self.assertIn("PWR ?", _plain(self.app.render_status(snap, bad)))
        good = PowerStatus(enabled=True, gpio_ok=True)
        self.assertNotIn("PWR", _plain(self.app.render_status(snap, good)))
        self.assertNotIn("PWR", _plain(self.app.render_status(snap, None)))


class PowerBannerLiveTests(unittest.TestCase):
    # Plain asyncio.run: IsolatedAsyncioTestCase enables asyncio debug mode,
    # which slows Textual enough to make its exit race the refresh interval.
    def test_repeated_pending_and_cancel_cycles_restore_dashboard(self) -> None:
        asyncio.run(self._cycles())

    async def _cycles(self) -> None:
        power = PowerSource()
        app = FoxDashApp(state_store=DashboardStateStore(), refresh_hz=10.0, power_status=power)
        async with app.run_test(size=(70, 40)) as pilot:
            await pilot.pause()
            feedback = app.query_one("#feedback_bar")
            baseline_classes = set(feedback.classes)
            baseline_content = _plain(feedback.content)
            baseline_timers = len(app._timers)
            self.assertTrue(any(c.startswith("feedback-") for c in baseline_classes))
            self.assertNotIn("feedback-power", baseline_classes)

            durations: list[float] = []
            for cycle in range(1, 41):
                power.pending(60.0 - cycle, countdown_id=cycle)
                started = time.perf_counter()
                app.refresh_dashboard()
                durations.append(time.perf_counter() - started)
                await pilot.pause()
                self.assertIn("feedback-power", feedback.classes)
                self.assertIn("EXTERNAL POWER LOST", _plain(feedback.content))

                power.normal()
                app.refresh_dashboard()
                await pilot.pause()
                # Exactly the original cue: classes, text, no leftovers.
                self.assertEqual(set(feedback.classes), baseline_classes)
                self.assertEqual(_plain(feedback.content), baseline_content)
                self.assertFalse(feedback.styles.inline.has_rule("border_left"))

            # No timers or intervals were added by the countdown.
            self.assertEqual(len(app._timers), baseline_timers)

            # Final-seconds urgent styling, then cancel just before expiry.
            power.pending(0.4, countdown_id=41)
            app.refresh_dashboard()
            await pilot.pause()
            self.assertIn("feedback-power-urgent", feedback.classes)
            power.normal()
            app.refresh_dashboard()
            await pilot.pause()
            self.assertEqual(set(feedback.classes), baseline_classes)
            self.assertLess(max(durations), 0.5)
            # The dashboard's own refresh interval otherwise races widget
            # teardown at exit (pre-existing; unrelated to power state).
            for timer in list(app._timers):
                timer.stop()


if __name__ == "__main__":
    unittest.main()
