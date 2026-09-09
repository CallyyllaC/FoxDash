from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foxdash_lite.log_archive import cleanup_completed_journeys, discover_completed_journeys, verify_archive
from foxdash_lite.session_identity import allocate_session_identity, session_sequence_from_id
from foxdash_lite.telemetry_logger import TelemetryLogger


class SessionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_allocations_are_persistently_monotonic(self) -> None:
        first = allocate_session_identity(self.log_dir)
        second = allocate_session_identity(self.log_dir)

        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertTrue(first.session_id.startswith("S00000001-"))
        self.assertTrue(second.session_id.startswith("S00000002-"))
        self.assertEqual(session_sequence_from_id(first.session_id), 1)
        self.assertEqual(session_sequence_from_id(second.session_id), 2)

        state = json.loads((self.log_dir / ".session-sequence.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_sequence"], 2)

    def test_missing_counter_recovers_from_existing_sequence_filename(self) -> None:
        first = allocate_session_identity(self.log_dir)
        self.assertEqual(first.sequence, 1)
        (self.log_dir / ".session-sequence.json").unlink()
        (self.log_dir / "journey_S00000007-20240101_000000-deadbe.zip").write_bytes(b"")

        recovered = allocate_session_identity(self.log_dir)
        self.assertEqual(recovered.sequence, 8)
        self.assertTrue(recovered.session_id.startswith("S00000008-"))

    def test_logger_records_sequence_and_wall_clock_policy(self) -> None:
        identity = allocate_session_identity(self.log_dir)
        logger = TelemetryLogger(self.log_dir)
        logger.start(
            source_name="test",
            session_id=identity.session_id,
            boot_id="boot-sequence-test",
            session_started_at=identity.started_at,
        )
        try:
            schema = json.loads(logger.paths["schema"].read_text(encoding="utf-8"))
            self.assertEqual(schema["schema_version"], 2)
            self.assertEqual(schema["session_sequence"], 1)
            session_text = logger.paths["text"].read_text(encoding="utf-8")
            self.assertIn("Session Sequence: 1", session_text)
            self.assertIn("wall clock informational", session_text)
        finally:
            logger.close()

    def test_new_format_journey_is_discovered_and_archived(self) -> None:
        identity = allocate_session_identity(self.log_dir)
        session = self.log_dir / f"psa_session_{identity.session_id}.txt"
        session.write_text(
            f"FoxDash session started {identity.started_at}\n"
            f"Session ID: {identity.session_id}\n"
            "Session Sequence: 1\n"
            "Boot ID: boot-A\n",
            encoding="utf-8",
            newline="\n",
        )
        decoded = self.log_dir / f"psa_decoded_core_{identity.session_id}.csv"
        decoded.write_text(
            "timestamp,sample,rpm,speed_mph\n"
            "2020-01-01T00:00:00+00:00,1,800,0\n",
            encoding="utf-8",
            newline="\n",
        )

        journeys = discover_completed_journeys(
            self.log_dir,
            active_session_id="S00000002-19990101_000000-active",
        )
        self.assertEqual([journey.session_id for journey in journeys], [identity.session_id])

        summary = cleanup_completed_journeys(
            self.log_dir,
            active_session_id="S00000002-19990101_000000-active",
        )
        self.assertEqual(summary.journeys_archived, 1)
        archive = self.log_dir / f"journey_{identity.session_id}.zip"
        manifest = verify_archive(archive, session_id=identity.session_id)
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["session_sequence"], 1)
        self.assertFalse(manifest["wall_clock_authoritative"])


if __name__ == "__main__":
    unittest.main()
