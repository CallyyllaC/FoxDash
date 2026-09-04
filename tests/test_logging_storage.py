from __future__ import annotations

import csv
import json
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from foxdash_lite.conversion import detect_csv_kind, load_snapshots_from_csv
from foxdash_lite.log_archive import (
    ArchiveTextReader,
    CleanupSummary,
    JourneyFiles,
    archive_journey,
    cleanup_completed_journeys,
    discover_completed_journeys,
    verify_archive,
)
from foxdash_lite.log_format import CompactDictWriter, compact_value, decode_status
from foxdash_lite.telemetry_logger import TelemetryLogger


class LoggingStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write(self, name: str, content: str | bytes) -> Path:
        path = self.log_dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def _journey(self, stamp: str = "20260722_120000", suffix: str = "abc123", boot: str = "boot-A") -> tuple[str, list[Path]]:
        session_id = f"{stamp}-{suffix}"
        session = self._write(
            f"psa_session_{stamp}.txt",
            f"FoxDash session started 2026-07-22T12:00:00+01:00\nSession ID: {session_id}\nBoot ID: {boot}\nUnicode: ° Δ →\n",
        )
        decoded = self._write(
            f"psa_decoded_core_{stamp}.csv",
            "timestamp,sample,rpm,speed_mph,turboMeasured,fuelRailMeasured_bar\n"
            "2026-07-22T12:00:01+01:00,1,1234,30.5,1012.25,350.5\n",
        )
        return session_id, [session, decoded]

    def test_utf8_without_bom_unicode_and_ascii_round_trip(self) -> None:
        path = self.log_dir / "compact.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = CompactDictWriter(handle, fieldnames=["text", "value"])
            writer.writeheader()
            writer.writerow({"text": "° Δ →", "value": 12.340000})
            writer.writerow({"text": "plain ASCII", "value": 5.0})
        payload = path.read_bytes()
        self.assertFalse(payload.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", payload)
        self.assertIn("° Δ →".encode("utf-8"), payload)
        self.assertIn(b"plain ASCII,5\n", payload)
        self.assertEqual(path.read_text(encoding="utf-8").splitlines()[1], "° Δ →,12.34")

    def test_numeric_formatting_is_bounded_and_trimmed(self) -> None:
        self.assertEqual(compact_value(7.0, field_name="rpm"), "7")
        self.assertEqual(compact_value(12.340000, field_name="batteryV"), "12.34")
        self.assertEqual(compact_value(0.123456789, field_name="relativeAccel_g"), "0.12346")
        self.assertEqual(compact_value(True), 1)
        self.assertEqual(compact_value("lugging", field_name="drivingState"), 13)
        self.assertEqual(decode_status("drivingState", "13"), "lugging")

    def test_active_session_schema_retains_change_metadata_once(self) -> None:
        logger = TelemetryLogger(self.log_dir)
        logger.start(
            source_name="test",
            session_id="20260722_120000-schema",
            boot_id="boot-schema",
            session_started_at="2026-07-22T12:00:00Z",
        )
        try:
            path = logger.paths["schema"]
            payload = path.read_bytes()
            self.assertFalse(payload.startswith(b"\xef\xbb\xbf"))
            schema = json.loads(payload.decode("utf-8"))
            self.assertEqual(schema["boot_id"], "boot-schema")
            self.assertTrue(schema["change_fields"])
            self.assertIn("unit", schema["change_fields"][0])
            self.assertEqual(schema["status_enums"]["gear"]["0"], "N")
        finally:
            logger.close()

    def test_logger_start_does_not_wait_for_completed_journey_archival(self) -> None:
        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()

        def blocked_cleanup(*_args, **_kwargs):
            cleanup_entered.set()
            allow_cleanup.wait(2.0)
            return CleanupSummary()

        logger = TelemetryLogger(self.log_dir)
        try:
            with mock.patch(
                "foxdash_lite.telemetry_logger.cleanup_completed_journeys",
                side_effect=blocked_cleanup,
            ):
                started = time.monotonic()
                logger.start(source_name="test", session_id="20260722_120000-background")
                elapsed = time.monotonic() - started
                self.assertTrue(cleanup_entered.wait(0.5))
                self.assertLess(elapsed, 0.5)
                self.assertTrue(logger.paths["ambient"].exists())
                self.assertTrue(logger._cleanup_thread.is_alive())

                allow_cleanup.set()
                logger._cleanup_thread.join(timeout=1.0)
                self.assertFalse(logger._cleanup_thread.is_alive())
                session_text = logger.paths["text"].read_text(encoding="utf-8")
                self.assertIn("journey_cleanup scheduled in background", session_text)
                self.assertIn("journey_cleanup {", session_text)
        finally:
            allow_cleanup.set()
            logger.close()

    def test_empty_and_header_only_cleanup_preserves_diagnostic_text(self) -> None:
        session_id, _ = self._journey()
        empty = self._write("psa_raw_blocks_20260722_120000.csv", b"")
        header = self._write("psa_unknown_report_20260722_120000.csv", "command,offset\n")
        diagnostic = self._write("psa_forensic_20260722_120000.txt", "Exception: sensor → offline\n")
        diagnostic_csv = self._write("psa_diagnostic_20260722_120000.csv", "Exception: sensor → offline\n")
        summary = cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        self.assertEqual(summary.zero_byte_files_deleted, 1)
        self.assertEqual(summary.header_only_files_deleted, 1)
        self.assertFalse(empty.exists())
        self.assertFalse(header.exists())
        archive = self.log_dir / f"journey_{session_id}.zip"
        manifest = verify_archive(archive, session_id=session_id)
        archived_names = {entry["name"] for entry in manifest["files"]}
        self.assertIn(diagnostic.name, archived_names)
        self.assertIn(diagnostic_csv.name, archived_names)

    def test_active_session_excluded_even_if_clock_moves_backwards(self) -> None:
        active_id, active_files = self._journey("20250101_010101", "active", "boot-new")
        old_id, old_files = self._journey("20260722_120000", "older", "boot-old")
        journeys = discover_completed_journeys(self.log_dir, active_id)
        self.assertEqual([journey.session_id for journey in journeys], [old_id])
        self.assertEqual(journeys[0].boot_id, "boot-old")
        self.assertTrue(all(path not in journeys[0].files for path in active_files))
        self.assertTrue(all(path in journeys[0].files for path in old_files))

    def test_successful_archive_has_manifest_and_deletes_sources_after_verification(self) -> None:
        session_id, files = self._journey()
        journey = JourneyFiles(session_id, "boot-A", "2026-07-22T12:00:00+01:00", tuple(files))
        summary = CleanupSummary()
        with mock.patch("foxdash_lite.log_archive.verify_archive", wraps=verify_archive) as verifier:
            self.assertTrue(archive_journey(journey, summary))
        self.assertGreaterEqual(verifier.call_count, 2)
        self.assertTrue(all(not path.exists() for path in files))
        archive = self.log_dir / f"journey_{session_id}.zip"
        manifest = verify_archive(archive, session_id=session_id)
        self.assertEqual(manifest["encoding"], "utf-8")
        self.assertEqual(manifest["newline"], "\\n")
        self.assertEqual(manifest["boot_id"], "boot-A")
        self.assertEqual(manifest["status_enums"]["gear"]["0"], "N")
        with zipfile.ZipFile(archive) as zipped:
            text = zipped.read(files[0].name).decode("utf-8")
        self.assertIn("° Δ →", text)

    def test_verification_failure_and_creation_failure_retain_sources(self) -> None:
        session_id, files = self._journey()
        journey = JourneyFiles(session_id, "boot-A", "", tuple(files))
        with mock.patch("foxdash_lite.log_archive.verify_archive", side_effect=ValueError("injected verification failure")):
            self.assertFalse(archive_journey(journey, CleanupSummary()))
        self.assertTrue(all(path.exists() for path in files))
        (self.log_dir / f"journey_{session_id}.zip").unlink(missing_ok=True)
        with mock.patch.object(zipfile.ZipFile, "write", side_effect=OSError("injected write failure")):
            self.assertFalse(archive_journey(journey, CleanupSummary()))
        self.assertTrue(all(path.exists() for path in files))

    def test_stale_temporary_recovery(self) -> None:
        session_id, _ = self._journey()
        stale = self._write(f"journey_{session_id}.zip.tmp", b"partial")
        summary = cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        self.assertFalse(stale.exists())
        self.assertEqual(summary.journeys_archived, 1)
        self.assertTrue((self.log_dir / f"journey_{session_id}.zip").exists())

    def test_valid_archive_removes_matching_duplicate_without_rearchive(self) -> None:
        session_id, files = self._journey()
        original_payload = files[1].read_bytes()
        summary = cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        archive = self.log_dir / f"journey_{session_id}.zip"
        original_archive = archive.read_bytes()
        duplicate = self._write(files[1].name, original_payload)
        summary = cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        self.assertEqual(summary.journeys_already_archived, 1)
        self.assertFalse(duplicate.exists())
        self.assertEqual(archive.read_bytes(), original_archive)

    def test_incomplete_archive_is_not_replaced_from_partial_source_set(self) -> None:
        session_id, files = self._journey()
        declared = [{"name": path.name, "uncompressed_length": path.stat().st_size} for path in files]
        archive = self.log_dir / f"journey_{session_id}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("manifest.json", json.dumps({
                "manifest_version": 1,
                "session_id": session_id,
                "boot_id": "boot-A",
                "journey_start_time": "",
                "files": declared,
            }))
        files[0].unlink()
        summary = cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        self.assertTrue(files[1].exists())
        self.assertTrue(archive.exists())
        self.assertEqual(summary.retained_source_files, 1)
        self.assertTrue(any("source files are unavailable" in failure for failure in summary.failures))

    def test_loose_and_archived_logs_are_both_readable_without_extraction(self) -> None:
        session_id, files = self._journey()
        loose = files[1]
        self.assertEqual(detect_csv_kind(loose), "decoded_core")
        self.assertEqual(len(load_snapshots_from_csv(loose)), 1)
        copy = self.log_dir / "loose-copy.csv"
        shutil.copyfile(loose, copy)
        cleanup_completed_journeys(self.log_dir, active_session_id="20260722_130000-new")
        archive = self.log_dir / f"journey_{session_id}.zip"
        self.assertEqual(detect_csv_kind(archive), "decoded_core")
        self.assertEqual(len(load_snapshots_from_csv(archive)), 1)
        with ArchiveTextReader(copy) as handle:
            self.assertEqual(next(csv.reader(handle))[0], "timestamp")


if __name__ == "__main__":
    unittest.main()
