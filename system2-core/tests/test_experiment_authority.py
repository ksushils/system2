#!/usr/bin/env python3
"""Regression fixtures for frozen-experiment authority provenance."""
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_telemetry_common as common


class ExperimentAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = common.RESEARCH_ROOT
        common.RESEARCH_ROOT = Path(self.temp.name)
        self.session, self.experiment = "2026-09-23", "FULL_STAGE2_QUARTILES_V1"

    def tearDown(self):
        common.RESEARCH_ROOT = self.original
        self.temp.cleanup()

    def artifact(self, run):
        path = common.RESEARCH_ROOT / self.session / run / "membership.json"
        common.write_immutable(path, {"run_id": run})
        return path

    def row(self, run, **extra):
        return {"experiment_name": self.experiment, "run_id": run,
                "intended_xnys_session": self.session, "symbol": "XYZ", **extra}

    def test_a_one_session_one_run_capture_and_updater_agree(self):
        path = self.artifact("run-a")
        common.publish_experiment_authority(self.experiment, self.session, "run-a", "t-a", path)
        selected, terminal = common.authoritative_experiment_membership_rows([(path, self.row("run-a"))], ("experiment_name", "symbol"))
        self.assertEqual((len(selected), terminal), (1, []))

    def test_b_two_runs_have_one_explicit_authority(self):
        first, second = self.artifact("run-a"), self.artifact("run-b")
        common.publish_experiment_authority(self.experiment, self.session, "run-a", "t-a", first)
        decision = common.publish_experiment_authority(self.experiment, self.session, "run-b", "t-b", second)
        self.assertEqual((decision["previous_run_id"], decision["new_run_id"]), ("run-a", "run-b"))

    def test_c_authoritative_final_membership_is_accepted(self):
        path = self.artifact("run-final")
        common.publish_experiment_authority(self.experiment, self.session, "run-final", "t", path)
        selected, terminal = common.authoritative_experiment_membership_rows([(path, self.row("run-final"))], ("experiment_name", "symbol"))
        self.assertEqual(len(selected), 1); self.assertFalse(terminal)

    def test_d_superseded_or_invalid_membership_is_terminal_not_dropped(self):
        path = self.artifact("run-old")
        common.publish_experiment_authority(self.experiment, self.session, "run-old", "t", path, status="INVALID_FOR_PERFORMANCE", reason="SOURCE_MISMATCH")
        selected, terminal = common.authoritative_experiment_membership_rows([(path, self.row("run-old"))], ("experiment_name", "symbol"))
        self.assertFalse(selected); self.assertEqual(terminal[0]["classification"], "INVALID_SESSION_MEMBERSHIP")

    def test_e_missing_capture_commit_stays_unknown(self):
        self.assertEqual(self.row("run-a").get("capture_code_commit", "UNKNOWN"), "UNKNOWN")

    def test_f_registry_and_capture_commit_are_separate(self):
        record = {"capture_code_commit": "UNKNOWN", "registry_git_commit": "registry-commit"}
        self.assertNotEqual(record["capture_code_commit"], record["registry_git_commit"])


if __name__ == "__main__":
    unittest.main()
