"""Output-boundary privacy and the real shared-budget controller, offline."""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import subtitle_audit as audit
import smart_subtitles as smart
from tests import test_candidate_source_recovery as fixtures


class ReportPrivacyTests(unittest.TestCase):
    def test_query_credentials_case_encoding_and_duplicates_preserve_identity(self):
        examples = (
            '?api_key=FIXTUREsecret&file_id=42&api_key=OTHERsecret',
            '?API-KEY=FIXTUREsecret&file_id=42',
            '?api%5fkey=FIXTUREsecret&file_id=42',
            '%3Fapi%5Fkey%3DFIXTUREsecret%26file_id%3D42',
            '%253Fapi%255Fkey%253DFIXTUREsecret%2526file_id%253D42',
            '?access_token=FIXTUREsecret&amp;file_id=42',
            '?key=FIXTUREsecret|parent:sd42',
            '?token=FIXTUREsecret&file_id=42',
        )
        for suffix in examples:
            with self.subTest(suffix=suffix):
                value = 'SubDL:42:https://fixture.invalid/file.srt' + suffix
                result = audit.redacted_text(value)
                self.assertNotIn('FIXTUREsecret', result)
                self.assertNotIn('OTHERsecret', result)
                self.assertTrue(result.startswith('SubDL:42:https://fixture.invalid/file.srt'))
                self.assertIn('42', result)
                self.assertEqual(result, audit.redacted_text(result))

    def test_named_headers_and_basic_auth_cannot_leak_in_exception_messages(self):
        examples = (
            'request failed https://user:FIXTUREsecret@fixture.invalid/movie?id=42',
            'Authorization: Bearer FIXTUREsecret',
            'Authorization: Basic FIXTUREsecret',
            'headers={"Authorization": "Bearer FIXTUREsecret"}',
            'subdl_api_key = "FIXTUREsecret with spaces"',
            "client_secret='FIXTUREsecret,with&punctuation'",
            'API-Key: FIXTUREsecret',
            'Bearer FIXTUREsecret',
        )
        for value in examples:
            with self.subTest(value=value):
                self.assertNotIn('FIXTUREsecret', audit.redacted_text(value))

    def test_structured_projection_is_separate_and_keeps_provenance(self):
        source = {'records': [{'candidate_key': 'SubDL:42:https://fixture.invalid/x?api_key=FIXTUREsecret',
            'digest': 'f' * 64, 'file_id': '42', 'evidence': {'SUBDL_API_KEY': 'OTHERsecret',
            'safe_url': 'https://fixture.invalid/42?language=en'}}],
            'tuple': ({'access_token': 'THIRDsecret'},),
            'https://fixture.invalid/?api_key=URLKEYsecret': 'safe',
            'attempted_candidates': 1}
        before = copy.deepcopy(source)
        result = audit.report_projection(source)
        self.assertEqual(source, before)
        serialized = json.dumps(result)
        for secret in ('FIXTUREsecret', 'OTHERsecret', 'THIRDsecret', 'URLKEYsecret'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result['records'][0]['digest'], 'f' * 64)
        self.assertEqual(result['records'][0]['file_id'], '42')
        self.assertEqual(result['records'][0]['evidence']['safe_url'], source['records'][0]['evidence']['safe_url'])

    def test_unrelated_subtitle_labels_and_local_paths_remain_exact(self):
        examples = ('2010 The Year We Make Contact （1984）.mkv',
            r'C:\ExampleMovies\The.Island.2005.SF_work\track-2.srt',
            'KEY LIGHT / A station / {y:i} / SHA256=123',
            'https://fixture.invalid/subtitle?file_id=42&language=en')
        for value in examples:
            self.assertEqual(audit.redacted_text(value), value)

    def test_prefixed_secret_fields_redact_but_file_signatures_remain_verifiable(self):
        source = {'service_password': 'PASSWORDsecret', 'provider_token': 'TOKENsecret',
            'nested': {'servicePassword': 'CAMELsecret', 'provider-access-token': 'ACCESSsecret'},
            'media_signature': {'size': 42, 'mtime_ns': 12345},
            'source_signature': 'sha256:1234', 'candidate_key': 'SubDL:42'}
        result = audit.report_projection(source)
        for secret in ('PASSWORDsecret', 'TOKENsecret', 'CAMELsecret', 'ACCESSsecret'):
            self.assertNotIn(secret, json.dumps(result))
        for key in ('media_signature', 'source_signature', 'candidate_key'):
            self.assertEqual(result[key], source[key])
        self.assertEqual(audit.report_projection(result), result)

    def test_cli_and_nested_labels_keep_nonsecret_context(self):
        for value in ('--api-key=FIXTUREsecret', '--api-key="FIXTUREsecret with spaces"',
                      '--provider-token=FIXTUREsecret', 'reason: api_key=FIXTUREsecret',
                      'service_password: FIXTUREsecret', 'provider_token=FIXTUREsecret'):
            with self.subTest(value=value):
                result = audit.redacted_text(value)
                self.assertNotIn('FIXTUREsecret', result)
                self.assertEqual(audit.redacted_text(result), result)
        for value in ('media_signature: 1234', 'source_signature=sha256:1234'):
            self.assertEqual(audit.redacted_text(value), value)

    def test_long_hyphenated_label_does_not_hide_following_cli_credential(self):
        damaged = 'part-' * 20000
        value = damaged + ' --api-key=FIXTUREsecret'
        result = audit.redacted_text(value)
        self.assertEqual(result, damaged + ' --api-key=[REDACTED]')

    def test_files_callback_and_return_value_redacted_without_mutating_internal_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            movie = root / 'Film.2020.mkv'
            movie.write_bytes(b'fixture')
            item = audit.FilmAudit(movie, root / 'reports')
            raw_key = 'SubDL:42:https://fixture.invalid/x?api_key=FIXTUREsecret|parent:sd42'
            row = item.add('provider request ?api_key=OTHERsecret', candidate_key=raw_key,
                nested={'password': 'NESTEDsecret'})
            item.update(row, 'selection', 'rejected', 'request failed ?access_token=LASTsecret')
            logs = []
            result, path = item.write('failed', 'Authorization: Bearer HEADERsecret', logs.append)
            self.assertEqual(row['candidate_key'], raw_key)
            bodies = [json.dumps(result), Path(path).read_text(encoding='utf8'),
                Path(path).with_suffix('.json').read_text(encoding='utf8'), '\n'.join(logs)]
            for body in bodies:
                for secret in ('FIXTUREsecret', 'OTHERsecret', 'NESTEDsecret', 'LASTsecret', 'HEADERsecret'):
                    self.assertNotIn(secret, body)
            self.assertIn('|parent:sd42', json.dumps(result))


class RecoveryStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CandidateSourceRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.logs = []
        self.fixture.stack.enter_context(patch.object(smart.pro_core, 'persistent_subtitle_logger',
            side_effect=lambda movie, log: self.logs.append))

    def test_numeric_two_stage_controller_reports_paid_requests_without_confirming_global_ids(self):
        reports = []
        original = smart.find_verified_english
        def with_audit(*args, **kwargs):
            record = audit.FilmAudit(args[0], self.fixture.root / 'reports')
            reports.append(record)
            with audit.scope(record):
                return original(*args, **kwargs)
        with patch.object(smart, 'find_verified_english', side_effect=with_audit):
            self.fixture.test_numeric_original_controller_uses_runtime_then_original_gates()
        rows = reports[0].searches
        measured = [row for row in rows if 'candidate_recovery_requests' in row]
        self.assertEqual(len(measured), 1)
        self.assertEqual(measured[0]['candidate_recovery_requests'], 2)
        self.assertEqual(measured[0]['shared_evidence_requests'], 2)
        self.assertGreaterEqual(measured[0]['candidate_recovery_seconds'], 0)
        self.assertEqual(sum(row.get('candidate_recovery_requests', 0) for row in rows), 2)
        self.assertEqual(rows[-1]['identity_resolution_status'], 'pending')
        self.assertEqual(rows[-1]['identity_resolution_requests'], 0)
        self.assertTrue(any('候选级恢复本次额外补证请求 2 次' in line for line in self.logs))
        result, path = reports[0].write('completed', '', self.logs.append)
        self.assertIn('本次 2 次', Path(path).read_text(encoding='utf8'))
        self.assertEqual(sum(row.get('candidate_recovery_requests', 0) for row in result['searches']), 2)

    def test_empty_recovery_still_reports_paid_request(self):
        run, recover, *_ = self.fixture.controller()
        def no_source(movie, identity, key, session, log, cancel=None):
            session.evidence_budget().request('fixture request', lambda: {})
            return [], {}
        recover.side_effect = no_source
        record = audit.FilmAudit(self.fixture.movie, self.fixture.root / 'reports')
        with audit.scope(record), self.assertRaises(RuntimeError):
            run()
        measured = [row for row in record.searches if 'candidate_recovery_requests' in row]
        self.assertEqual(len(measured), 1)
        self.assertEqual(measured[0]['candidate_recovery_requests'], 1)

    def test_failed_recovery_preserves_shared_budget_and_reports_paid_attempt(self):
        run, recover, *_ = self.fixture.controller()
        sessions = []
        def failed(movie, identity, key, session, log, cancel=None):
            sessions.append(session)
            def error():
                raise RuntimeError('fixture failure ?api_key=FIXTUREsecret')
            session.evidence_budget().request('fixture request', error)
        recover.side_effect = failed
        record = audit.FilmAudit(self.fixture.movie, self.fixture.root / 'reports')
        with audit.scope(record), self.assertRaises(RuntimeError):
            run()
        self.assertEqual(sessions[0].budget.request_count, 1)
        self.assertEqual(sessions[0].budget.max_requests, 2)
        measured = [row for row in record.searches if 'candidate_recovery_requests' in row]
        self.assertEqual(measured[0]['candidate_recovery_requests'], 1)
        self.assertNotIn('FIXTUREsecret', '\n'.join(self.logs))

    def test_prepaid_global_evidence_is_not_counted_again_as_candidate_recovery(self):
        import work_identity_resolver as work
        run, recover, *_ = self.fixture.controller()
        expected = recover.return_value
        normal = smart.online_subtitles.search.side_effect
        sessions = []
        def prepaid_search(*args, **kwargs):
            session = kwargs['identity_recovery_session']
            sessions.append(session)
            session.evidence_budget().request('earlier global evidence', lambda: {})
            return normal(*args, **kwargs)
        smart.online_subtitles.search.side_effect = prepaid_search
        def recovering(movie, identity, key, session, log, cancel=None):
            session.evidence_budget().request('candidate evidence', lambda: {})
            return expected
        recover.side_effect = recovering
        record = audit.FilmAudit(self.fixture.movie, self.fixture.root / 'reports')
        with audit.scope(record):
            run()
        measured = [row for row in record.searches if 'candidate_recovery_requests' in row]
        self.assertEqual(measured[0]['candidate_recovery_requests'], 1)
        self.assertEqual(measured[0]['shared_evidence_requests'], 2)
        with self.assertRaises(work.EvidenceBudgetExceeded):
            sessions[0].budget.request('third request must remain blocked', lambda: {})

    def test_user_stop_during_recovery_is_not_relabelled_as_search_failure(self):
        run, recover, *_ = self.fixture.controller()
        def stopped(movie, identity, key, session, log, cancel=None):
            session.evidence_budget().request('fixture request', lambda: {})
            raise smart.core.CancelledError('fixture stop')
        recover.side_effect = stopped
        record = audit.FilmAudit(self.fixture.movie, self.fixture.root / 'reports')
        with audit.scope(record), self.assertRaises(smart.core.CancelledError):
            run()
        measured = [row for row in record.searches if 'candidate_recovery_requests' in row]
        self.assertEqual(measured[0]['candidate_recovery_requests'], 1)

    def test_normal_source_does_not_start_or_report_new_recovery(self):
        run, recover, *_ = self.fixture.controller(ordinary=True)
        record = audit.FilmAudit(self.fixture.movie, self.fixture.root / 'reports')
        with audit.scope(record):
            run()
        recover.assert_not_called()
        self.assertFalse(any('candidate_recovery_requests' in row for row in record.searches))

    def test_warm_checkpoint_does_not_charge_or_repeat_recovery(self):
        from unittest.mock import Mock
        acceptance = Mock(side_effect=smart.pro_core.RetainedSubtitleVerificationError('unverified', {}, None))
        run, recover, *_ = self.fixture.controller(acceptance)
        expected = recover.return_value
        def recovering(movie, identity, key, session, log, cancel=None):
            session.evidence_budget().request('fixture request', lambda: {})
            return expected
        recover.side_effect = recovering
        records = [audit.FilmAudit(self.fixture.movie, self.fixture.root / f'reports-{i}') for i in range(2)]
        with audit.scope(records[0]), self.assertRaises(smart.pro_core.RetainedSubtitleVerificationError):
            run()
        acceptance.side_effect = None
        with audit.scope(records[1]):
            run()
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(sum(row.get('candidate_recovery_requests', 0) for row in records[0].searches), 1)
        self.assertFalse(any('candidate_recovery_requests' in row for row in records[1].searches))


if __name__ == '__main__':
    unittest.main()
