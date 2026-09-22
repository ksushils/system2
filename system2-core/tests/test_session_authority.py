#!/usr/bin/env python3
"""No live data: immutable supersession and single effective membership."""
import tempfile
import unittest
from pathlib import Path
import sys

if Path(__file__).resolve().parent.name == 'tests':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_telemetry_common as common


class SessionAuthorityTest(unittest.TestCase):
    def test_latest_run_only_and_old_artifacts_retained(self):
        original_root = common.RESEARCH_ROOT
        with tempfile.TemporaryDirectory() as folder:
            try:
                common.RESEARCH_ROOT = Path(folder)
                session = '2026-09-22'
                first = common.RESEARCH_ROOT / session / 'run-a' / 'funnel_membership.json'
                second = common.RESEARCH_ROOT / session / 'run-b' / 'funnel_membership.json'
                common.write_immutable(first, {'run_id': 'run-a'})
                common.publish_session_authority(session, 'run-a', '2026-09-21T02:00:00Z', first)
                common.write_immutable(second, {'run_id': 'run-b'})
                decision = common.publish_session_authority(session, 'run-b', '2026-09-22T02:00:00Z', second)
                self.assertEqual(decision['supersedes_run_id'], 'run-a')
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                rows = [(first, {'cohort': 'A', 'symbol': 'XYZ', 'trading_date': session, 'run_id': 'run-a'}),
                        (second, {'cohort': 'A', 'symbol': 'XYZ', 'trading_date': session, 'run_id': 'run-b'})]
                selected, duplicates = common.independent_membership_rows(rows, ('cohort', 'symbol', 'trading_date'))
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0][1]['run_id'], 'run-b')
                self.assertEqual(duplicates[0]['classification'], 'SUPERSEDED_INTENDED_SESSION_RUN')
            finally:
                common.RESEARCH_ROOT = original_root


if __name__ == '__main__':
    unittest.main()
