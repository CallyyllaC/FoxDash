from __future__ import annotations

"""Editable presentation text for FoxDash.

This module keeps *display wording* separate from telemetry and scoring logic.
Edit ``ui_text.json`` and restart FoxDash to change efficiency labels/badges,
mood faces/art, mood labels, or driving-tip sentences without touching Python
code.
"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "efficiency": {"labels": {}, "badges": {}},
    "mood": {"states": {}},
    "tips": {},
}


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _three_lines(value: Any, fallback: list[str]) -> list[str]:
    """Return exactly three display lines from JSON-friendly input.

    ``art`` and ``badge`` are deliberately data, not code: a user can replace
    the placeholder blobs later without reopening the dashboard internals.
    Invalid/missing entries gracefully fall back to an ordinary one-line label.
    """
    lines: list[str] = []
    if isinstance(value, (list, tuple)):
        lines = [str(item) for item in value if isinstance(item, (str, int, float))]
    elif isinstance(value, str):
        lines = value.splitlines()

    if not lines:
        lines = list(fallback)
    lines = lines[:3]
    return lines + [""] * (3 - len(lines))


class UiText:
    """Small read-only facade over the user-editable JSON presentation file."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def load(cls, path: str | Path | None = None) -> "UiText":
        config_path = Path(path) if path is not None else Path(__file__).with_name("ui_text.json")
        data = deepcopy(DEFAULTS)
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _deep_merge(data, loaded)
        except (OSError, json.JSONDecodeError):
            # A malformed wording file should not brick the dashboard. Use empty
            # fallbacks rather than turning a small spelling edit into a roadside
            # IT incident.
            pass
        return cls(data)

    def efficiency_label(self, state: str) -> str:
        labels = self._data.get("efficiency", {}).get("labels", {})
        if isinstance(labels, dict):
            value = labels.get(state)
            if isinstance(value, str) and value.strip():
                return value
        return state

    def efficiency_badge(self, state: str) -> list[str]:
        """Three-line text-only state badge for the Efficiency card."""
        efficiency = self._data.get("efficiency", {})
        badges = efficiency.get("badges", {}) if isinstance(efficiency, dict) else {}
        value = badges.get(state) if isinstance(badges, dict) else None
        label = self.efficiency_label(state)
        words = label.split(maxsplit=1)
        fallback = [words[0] if words else state, words[1] if len(words) > 1 else "", ""]
        return _three_lines(value, fallback)

    def mood(self, state: str) -> tuple[str, str]:
        states = self._data.get("mood", {}).get("states", {})
        item = states.get(state, {}) if isinstance(states, dict) else {}
        if not isinstance(item, dict):
            item = {}
        face = item.get("face")
        label = item.get("label")
        return (
            str(face) if isinstance(face, str) and face else ":|",
            str(label) if isinstance(label, str) and label else state,
        )

    def mood_art(self, state: str) -> list[str]:
        """Three-line terminal-safe mood blob; editable in ``ui_text.json``."""
        states = self._data.get("mood", {}).get("states", {})
        item = states.get(state, {}) if isinstance(states, dict) else {}
        if not isinstance(item, dict):
            item = {}
        face, _label = self.mood(state)
        return _three_lines(item.get("art"), ["", face, ""])

    def tip(self, key: str) -> str:
        tips = self._data.get("tips", {})
        if isinstance(tips, dict):
            value = tips.get(key)
            if isinstance(value, str) and value.strip():
                return value
            fallback = tips.get("default")
            if isinstance(fallback, str) and fallback.strip():
                return fallback
        return ""
