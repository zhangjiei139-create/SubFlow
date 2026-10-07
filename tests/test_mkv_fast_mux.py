import copy
import io
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mkv_fast_mux as fast
import subtitle_tool_core as core


def elem(eid, value):
    width = next(n for n in range(1, 9) if len(value) < (1 << (7 * n)) - 1)
    return (eid.to_bytes((eid.bit_length() + 7) // 8, 'big') +
            (len(value) | (1 << (7 * width))).to_bytes(width, 'big') + value)


def uint(eid, value):
    return elem(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big'))


def track(number, codec, mapping=b''):
    kind = 1 if codec.startswith('V_') else 2 if codec.startswith('A_') else 17
    return elem(0xAE, uint(0xD7, number) + uint(0x73C5, number * 100) +
                uint(0x83, kind) + elem(0x86, codec.encode()) + elem(0x63A2, b'private') + mapping)


def document(dv=None, *, audio='A_TRUEHD', chapters=b'', attachments=b'', extra_audio=False):
    mapping = b'' if dv is None else elem(0x41E4,
        uint(0x41E7, int.from_bytes(b'dvcC', 'big')) + elem(0x41ED, dv) + uint(0x41F0, 1))
    tracks = track(1, 'V_MPEGH/ISO/HEVC', mapping) + track(2, audio) + track(3, 'S_HDMV/PGS')
    if extra_audio:
        tracks += track(4, 'A_DTS')
    return elem(0x1A45DFA3, elem(0x4282, b'matroska')) + elem(
        fast.SEGMENT, elem(fast.TRACKS, tracks) + chapters + attachments + elem(fast.CLUSTER, b''))


def media(audio='A_TRUEHD', extra_audio=False):
    result = {'tracks': [{'id': i, 'type': kind, 'properties': {
        'number': i + 1, 'codec_id': codec, 'language': 'eng', 'default_track': True,
        'forced_track': False}}
        for i, (kind, codec) in enumerate((('video', 'V_MPEGH/ISO/HEVC'),
                                        ('audio', audio), ('subtitles', 'S_HDMV/PGS')))],
        'chapters': [], 'attachments': []}
    if extra_audio:
        result['tracks'].append({'id': 3, 'type': 'audio',
                                 'properties': {'number': 4, 'codec_id': 'A_DTS'}})
    return result


class FastMuxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.output, self.tool = (self.root / name for name in ('source.mkv', 'out.mkv', 'mkvmerge.exe'))
        self.source.write_bytes(document())
        self.tool.write_bytes(b'test-tool')
        self.metadata = media()

    def assess(self, **kw):
        with patch.object(fast, '_tool_version', return_value=True):
            return fast.assess(self.source, self.output, self.metadata, kw.pop('audio_ids', [1]),
                               kw.pop('sub_ids', [2]), kw.pop('generated', []), self.tool, **kw)

    def test_plain_truehd_and_verified_dv81_are_eligible(self):
        self.assertTrue(self.assess().enabled)
        self.source.write_bytes(document(bytes.fromhex('0100103510') + b'\0' * 19))
        self.assertTrue(self.assess().enabled)

    def test_dv7_el_unknown_version_and_compatibility_are_kept_on_old_route(self):
        for config in ('01000e3730', '0100103730', '0200103510', '0100103560'):
            with self.subTest(config=config):
                self.source.write_bytes(document(bytes.fromhex(config) + b'\0' * 19))
                self.assertFalse(self.assess().enabled)

    def test_mixed_dts_is_allowed_only_when_not_retained(self):
        self.source.write_bytes(document(extra_audio=True))
        self.metadata = media(extra_audio=True)
        self.assertTrue(self.assess().enabled)
        self.assertFalse(self.assess(audio_ids=[1, 3]).enabled)

    def test_nontruehd_unverified_video_and_subtitle_structures_do_not_start_inspection(self):
        for category, value in (('audio', 'A_AC3'), ('video', 'V_MPEG4/ISO/AVC'), ('subtitles', 'S_TEXT/ASS')):
            with self.subTest(category=category), patch.object(fast, 'read_headers') as read:
                self.metadata = media()
                next(t for t in self.metadata['tracks'] if t['type'] == category)['properties']['codec_id'] = value
                self.assertFalse(self.assess().enabled)
                read.assert_not_called()
        self.assertFalse(self.assess(generated=[(Path('new.ass'), 'en', 'name', True)]).enabled)
        self.assertFalse(self.assess(allowed=False).enabled)

    def test_unverified_tool_or_unreadable_header_uses_original_mux(self):
        with patch.object(fast, '_tool_version', return_value=False):
            self.assertFalse(fast.assess(self.source, self.output, self.metadata, [1], [2], [], self.tool).enabled)
        self.source.write_bytes(b'not-a-mkv')
        self.assertFalse(self.assess().enabled)

    def test_retained_headers_follow_mkvmerge_type_order(self):
        self.metadata['tracks'] = list(reversed(self.metadata['tracks']))
        self.assertEqual([t['type'] for t in self.assess().retained], ['video', 'audio', 'subtitles'])

    def test_ordered_or_linked_chapters_fail_closed(self):
        for child in (uint(0x45DD, 1), elem(0xB6, elem(0x6E67, b'uid'))):
            self.source.write_bytes(document(chapters=elem(fast.CHAPTERS, elem(0x45B9, child))))
            self.assertFalse(self.assess().enabled)

    def test_equivalent_chapter_defaults_and_ietf_language_are_preserved(self):
        a = elem(0x45B9, elem(0xB6, uint(0x73C4, 1) + uint(0x91, 0) +
                             elem(0x80, elem(0x85, b'Opening') + elem(0x437C, b'eng'))))
        b = elem(0x45B9, uint(0x45DD, 0) + elem(0xB6, uint(0x73C4, 1) + uint(0x91, 0) +
                             uint(0x4598, 1) + elem(0x80, elem(0x85, b'Opening') +
                                 elem(0x437C, b'eng') + elem(0x437D, b'en'))))
        self.assertEqual(fast._canonical(a), fast._canonical(b))

    def test_header_verifier_detects_private_delay_mapping_and_attachment_changes(self):
        plan = self.assess()
        headers = copy.deepcopy(plan.headers)
        out = copy.deepcopy(self.metadata)
        with patch.object(fast, 'read_headers', return_value=headers):
            fast.verify(plan, self.output, out, {}, {}, [])
        for field, value in (('private', b'changed'), ('codec_delay', 100), ('default_duration', 200),
                             ('uid', 500), ('seek_preroll', 600), ('mappings', ((1, b'data', 9, b'name'),))):
            with self.subTest(field=field):
                changed = copy.deepcopy(headers)
                changed['tracks'][0][field] = value
                with patch.object(fast, 'read_headers', return_value=changed):
                    with self.assertRaises(fast.HeaderCheckError):
                        fast.verify(plan, self.output, out, {}, {}, [])
        changed = {**headers, 'attachments': (('changed',),)}
        with patch.object(fast, 'read_headers', return_value=changed):
            with self.assertRaises(fast.HeaderCheckError):
                fast.verify(plan, self.output, out, {}, {}, [])

    def test_metadata_flags_and_generated_language_are_checked(self):
        plan = self.assess()
        headers = copy.deepcopy(plan.headers)
        out = copy.deepcopy(self.metadata)
        out['tracks'][2]['properties']['default_track'] = False
        with patch.object(fast, 'read_headers', return_value=headers):
            fast.verify(plan, self.output, out, {}, {2: False}, [])
            out['tracks'][1]['properties']['language'] = 'jpn'
            with self.assertRaises(fast.HeaderCheckError):
                fast.verify(plan, self.output, out, {}, {2: False}, [])
        headers['tracks'].append({**headers['tracks'][2], 'codec': 'S_TEXT/UTF8'})
        out = copy.deepcopy(self.metadata)
        out['tracks'].append({'type': 'subtitles', 'properties': {
            'codec_id': 'S_TEXT/UTF8', 'language': 'chi', 'track_name': 'Chinese',
            'default_track': False, 'forced_track': False}})
        with patch.object(fast, 'read_headers', return_value=headers):
            fast.verify(plan, self.output, out, {}, {}, [('chi', 'Chinese', False)])
            out['tracks'][-1]['properties']['language'] = 'eng'
            with self.assertRaises(fast.HeaderCheckError):
                fast.verify(plan, self.output, out, {}, {}, [('chi', 'Chinese', False)])

    def test_tracks_size_bound_rejects_before_large_read(self):
        head = elem(0x1A45DFA3, elem(0x4282, b'matroska'))
        # Unknown-sized Segment is valid; Tracks is too large without a body.
        payload = head + fast.SEGMENT.to_bytes(4, 'big') + b'\xff' + elem(fast.TRACKS, b'x' * (1024 * 1024 + 1))
        self.source.write_bytes(payload)
        self.assertFalse(self.assess().enabled)

    def test_chapters_after_cluster_use_seekhead_without_reading_media(self):
        chapter = elem(fast.CHAPTERS, elem(0x45B9, elem(0xB6, uint(0x91, 0))))
        track_block = elem(fast.TRACKS, track(1, 'V_MPEGH/ISO/HEVC'))
        # Fixed-width seek position means offset doesn't change its own length.
        seek = lambda pos: elem(fast.SEEK_HEAD, elem(0x4DBB,
                            elem(0x53AB, fast.CHAPTERS.to_bytes(4, 'big')) + elem(0x53AC, pos.to_bytes(8, 'big'))))
        cluster = elem(fast.CLUSTER, b'x' * (2 * 1024 * 1024))
        position = len(seek(0)) + len(track_block) + len(cluster)
        content = elem(0x1A45DFA3, elem(0x4282, b'matroska')) + elem(
            fast.SEGMENT, seek(position) + track_block + cluster + chapter)
        class CountReads(io.BytesIO):
            bytes_read = 0
            def read(self, size=-1):
                value = super().read(size)
                self.bytes_read += len(value)
                return value
        reader = CountReads(content)
        with patch.object(Path, 'open', return_value=reader):
            result = fast.read_headers(self.source)
        self.assertTrue(result['chapters'])
        self.assertLess(reader.bytes_read, 1024)


class FastMuxExecutionTests(unittest.TestCase):
    def invoke(self, folder, run, *, verify=None, cancel=None):
        output = Path(folder) / 'out.mkv'
        plan = fast.FastMuxPlan(True, 'fixture', {}, ())
        patches = [patch.object(fast, 'assess', return_value=plan),
                   patch.object(core, 'run_command', side_effect=run),
                   patch.object(core, 'inspect_media', return_value={}),
                   patch.object(fast, 'verify', side_effect=verify)]
        with patches[0], patches[1] as process, patches[2], patches[3]:
            core.mux_video('input.mkv', str(output), [1], [2],
                           [(Path('new.srt'), 'en', 'English', True)], lambda _: None,
                           input_media=media(), subtitle_sync_offsets={2: -150}, cancel_event=cancel)
            return process.call_args_list

    def test_success_keeps_existing_arguments_and_applies_global_flag_once(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = self.invoke(folder, lambda *a, **k: None)
        self.assertEqual(len(calls), 1)
        args = calls[0].args[0]
        self.assertEqual(args[3:5], ['--engage', 'force_passthrough_packetizer'])
        self.assertIn('2:-150', args)
        self.assertIn('1:yes', args)
        self.assertIn('2:no', args)
        self.assertIn('0:eng', args)

    def test_failed_header_verification_cleans_partial_before_single_original_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'out.mkv'
            runs = []
            def run(args, **kwargs):
                self.assertFalse(output.exists())
                runs.append(args)
                output.write_bytes(b'partial')
            calls = self.invoke(folder, run, verify=fast.HeaderCheckError('changed header'))
            self.assertEqual(len(calls), 2)
            self.assertNotIn('--engage', calls[1].args[0])
            self.assertEqual(runs[0][:3] + runs[0][5:], runs[1])

    def test_tool_failure_retries_once_but_disk_full_does_not(self):
        for error, expected in (('unsupported experimental flag', 2), ('No space left on device', 1)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    raise RuntimeError(error)
                with self.assertRaises(RuntimeError):
                    self.invoke(folder, run)
                self.assertEqual(len(calls), expected)

    def test_user_cancel_never_retries_or_verifies(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                raise core.CancelledError('stop')
            with self.assertRaises(core.CancelledError):
                self.invoke(folder, run)
            self.assertEqual(len(calls), 1)
        event = threading.Event(); event.set()
        with tempfile.TemporaryDirectory() as folder, patch.object(fast, 'assess') as assess:
            with self.assertRaises(core.CancelledError):
                core.mux_video('source.mkv', str(Path(folder)/'out.mkv'), [1], [2], [],
                               lambda _: None, cancel_event=event)
            assess.assert_not_called()

    def test_mp4_tracks_only_route_disables_fast_mux_for_intermediate_mkv(self):
        import pro_core
        with tempfile.TemporaryDirectory() as folder:
            def write_output(*args, **kwargs):
                Path(args[1]).write_bytes(b'output')
            with patch.object(core, 'ensure_output_disk_space'), \
                 patch.object(core, 'inspect_media', return_value=media()), \
                 patch.object(core, 'validate_media_output'), \
                 patch.object(core, 'mux_video', side_effect=write_output) as mux, \
                 patch.object(core, 'remux_mkv_to_mp4', side_effect=write_output):
                pro_core.process_tracks_only('source.mkv', str(Path(folder)/'out.mp4'), [1], [2],
                                             folder, lambda _: None, None)
            self.assertFalse(mux.call_args.kwargs['allow_fast_passthrough'])
            self.assertEqual(Path(mux.call_args.args[1]).suffix, '.mkv')


if __name__ == '__main__':
    unittest.main()
