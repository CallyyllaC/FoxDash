from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import foxdash_lite.app as app_module

from rich.align import Align
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from foxdash_lite.app import FoxDashApp, render_key
from foxdash_lite.state_store import DashboardStateStore


def _card(value: str, *, style: str = "bold white") -> Table:
    table = Table.grid(expand=True)
    table.add_column(width=8, style="dim")
    table.add_column(ratio=1)
    table.add_row("RPM", Align.center(Text(value, style=style)))
    return table


class RenderKeyTests(unittest.TestCase):
    def test_identical_renderables_share_a_key(self) -> None:
        self.assertEqual(render_key(_card("1500")), render_key(_card("1500")))

    def test_text_and_style_changes_change_the_key(self) -> None:
        self.assertNotEqual(render_key(_card("1500")), render_key(_card("1510")))
        self.assertNotEqual(render_key(_card("1500")), render_key(_card("1500", style="bold red")))

    def test_unknown_renderables_never_match(self) -> None:
        marker = object()
        self.assertNotEqual(render_key(marker), render_key(marker))


class _CountingApp(FoxDashApp):
    CSS_PATH = str(Path(app_module.__file__).with_name("theme.tcss"))

    def set_interval(self, *args, **kwargs):  # drive ticks by hand
        return None


class RefreshSkipTests(unittest.TestCase):
    def test_unchanged_tick_does_not_touch_cards(self) -> None:
        async def scenario() -> tuple[int, int]:
            app = _CountingApp(state_store=DashboardStateStore(source_name="test"))
            calls: list[str] = []
            original = Static.update

            def counting_update(widget, content="", *, layout=True):
                calls.append(widget.id or "card-content")
                original(widget, content, layout=layout)

            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                Static.update = counting_update
                try:
                    app.refresh_dashboard()
                    first = len(calls)
                    calls.clear()
                    app.refresh_dashboard()
                    second = calls.count("card-content")
                finally:
                    Static.update = original
            return first, second

        first, second = asyncio.run(scenario())
        self.assertGreaterEqual(first, 0)
        self.assertEqual(second, 0)


if __name__ == "__main__":
    unittest.main()
