from __future__ import annotations

"""Cross-platform FoxDash environment installer.

Run this from the project folder. It creates or repairs ``.venv`` for the
operating system that is running it, then installs project requirements.

A copied project folder may contain a virtual environment created on another
OS, or the installer may itself be launched from the soon-to-be-deleted venv.
The latter is important: we capture a stable *base* Python before removing
``.venv`` so rebuilding does not delete the interpreter we are about to use.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python() -> Path:
    """Return the local venv interpreter location for the current OS."""
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def bootstrap_python() -> Path:
    """Return a Python executable that will survive deleting ``.venv``.

    If install.py was launched while the local venv was active, ``sys.executable``
    points inside ``.venv``.  Deleting that directory and then invoking it to
    create a replacement produces the entertaining but useless ENOENT failure.
    CPython exposes the base interpreter through ``sys._base_executable``.
    """
    current = Path(sys.executable)
    base_raw = getattr(sys, "_base_executable", None)
    if base_raw:
        base = Path(base_raw)
        if base.is_file() and base != current:
            return base

    # Usually reached only outside a venv.  Keep the current interpreter rather
    # than guessing a platform-specific Python path.
    return current


def _remove_readonly(func: object, path: str, _exc_info: object) -> None:
    """Allow cleanup of copied Windows environments with read-only files."""
    os.chmod(path, stat.S_IWRITE)
    func(path)  # type: ignore[operator]


def remove_venv() -> None:
    if VENV.is_symlink():
        VENV.unlink()
    elif VENV.exists():
        shutil.rmtree(VENV, onerror=_remove_readonly)


def probe_venv(python: Path) -> tuple[bool, str]:
    """Check that the candidate venv can run on this OS and belongs here."""
    if not python.is_file():
        return False, f"expected interpreter is missing: {python}"

    has_windows_layout = (VENV / "Scripts").exists()
    has_unix_layout = (VENV / "bin").exists()
    if has_windows_layout and has_unix_layout:
        return False, "mixed Windows/Linux virtualenv layout detected"

    try:
        probe = subprocess.run(
            [str(python), "-c", "import os, sys; print(os.name); print(sys.prefix)"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"could not start virtualenv Python: {exc}"

    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().replace("\n", " | ")
        return False, f"virtualenv Python exited {probe.returncode}: {detail}"

    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return False, "virtualenv probe returned incomplete information"

    if lines[0] != os.name:
        return False, f"virtualenv reports OS {lines[0]!r}, current OS is {os.name!r}"

    try:
        actual_prefix = Path(lines[1]).resolve()
        expected_prefix = VENV.resolve()
    except OSError as exc:
        return False, f"could not resolve virtualenv location: {exc}"

    if actual_prefix != expected_prefix:
        return False, f"virtualenv points at {actual_prefix}, not {expected_prefix}"

    return True, "usable"


def ensure_venv(force_recreate: bool = False) -> Path:
    # Capture this before potentially deleting .venv.  This fixes the exact
    # "current shell had the stale venv activated" failure mode.
    creator_python = bootstrap_python()
    python = venv_python()
    usable, reason = probe_venv(python)

    if force_recreate:
        usable = False
        reason = "forced rebuild requested"

    if not usable:
        if VENV.exists() or VENV.is_symlink():
            print(f"Rebuilding {VENV}: {reason}")
            remove_venv()
        else:
            print(f"Creating {VENV}")

        subprocess.check_call([str(creator_python), "-m", "venv", str(VENV)])
        python = venv_python()

        usable, reason = probe_venv(python)
        if not usable:
            raise RuntimeError(f"Fresh virtualenv validation failed: {reason}")

    return python


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or repair FoxDash's local Python environment.")
    parser.add_argument(
        "--recreate-venv",
        action="store_true",
        help="delete and rebuild .venv even when it looks usable",
    )
    parser.add_argument(
        "--check-venv",
        action="store_true",
        help="only validate the local .venv; return non-zero if it needs rebuilding",
    )
    args = parser.parse_args()

    if args.check_venv:
        usable, reason = probe_venv(venv_python())
        if usable:
            print("FoxDash virtual environment is usable.")
            return 0
        print(f"FoxDash virtual environment needs repair: {reason}")
        return 2

    python = ensure_venv(force_recreate=args.recreate_venv)
    subprocess.check_call([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    print("FoxDash environment ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
