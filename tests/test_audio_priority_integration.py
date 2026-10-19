"""Scheduling integration with fake processes and tiny PCM/NPZ fixtures only."""
import io
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import continuous_vad as continuous
import pro_core as product
import subtitle_tool_core as core
import audio_priority as priority
from tests import test_sampled_vad_integration as sampled_fixture
from tests import test_audio_reference_policy as reference_fixture


class RecordingPolicy:
    def __init__(self, promoted=False, attempted=False):
        self.promoted = promoted
        self.attempted = bool(attempted or promoted)
        self.monitors = []

    def monitor(self, progress_path, media_seconds, future_media_seconds=0):
        result = SimpleNamespace(progress_path=progress_path, media_seconds=media_seconds,
                                 future_media_seconds=future_media_seconds,
                                 policy=self)
        self.monitors.append(result)
        return result


class CommandMonitorIntegrationTests(unittest.TestCase):
    def process(self, *, returncode=0):
        process = Mock(returncode=returncode)
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        process.communicate.return_value = (b'complete stdout', b'complete stderr')
        return process

    def test_default_path_has_original_flags_pipes_and_one_clock_read(self):
        process = self.process()
        with patch.object(core.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(core.time, 'monotonic', return_value=0) as clock:
            result = core.run_command(['ffmpeg.exe', '-i', 'input'])
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(spawn.call_args.kwargs['creationflags'], core.EXTERNAL_PROCESS_CREATION_FLAGS)
        self.assertEqual(spawn.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.PIPE)
        self.assertEqual((result.stdout, result.stderr), (b'complete stdout', b'complete stderr'))

    def test_monitor_observes_same_process_without_consuming_either_pipe(self):
        process = self.process()
        process.communicate.side_effect = [subprocess.TimeoutExpired('ffmpeg', .2),
                                          (b'all stdout', b'all stderr')]
        calls = []
        monitor = SimpleNamespace(
            creation_flags=lambda flags: flags,
            started=lambda child: calls.append(('started', child)),
            poll=lambda child: calls.append(('poll', child)),
            finished=lambda child: calls.append(('finished', child)))
        with patch.object(core.subprocess, 'Popen', return_value=process):
            result = core.run_command(['ffmpeg.exe'], process_monitor=monitor)
        self.assertEqual([kind for kind, _ in calls], ['started', 'poll', 'poll', 'finished'])
        self.assertTrue(all(child is process for _, child in calls))
        self.assertEqual(result.stdout, b'all stdout')
        self.assertEqual(result.stderr, b'all stderr')
        self.assertEqual(process.stdout.tell(), 0)
        self.assertEqual(process.stderr.tell(), 0)

    def test_cancel_precedes_poll_and_finished_cannot_replace_cancel(self):
        process = self.process()
        cancel = threading.Event()
        cancel.set()
        monitor = Mock()
        monitor.creation_flags.return_value = core.EXTERNAL_PROCESS_CREATION_FLAGS
        monitor.finished.side_effect = OSError('diagnostic unavailable')
        with patch.object(core.subprocess, 'Popen', return_value=process), \
             patch.object(core, 'terminate_process_tree') as terminate:
            with self.assertRaises(core.CancelledError):
                core.run_command(['ffmpeg.exe'], cancel_event=cancel, process_monitor=monitor)
        terminate.assert_called_once_with(process)
        monitor.poll.assert_not_called()
        monitor.finished.assert_called_once_with(process)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_observer_failures_preserve_tool_failure_and_default_creation_flags(self):
        process = self.process(returncode=2)
        process.communicate.return_value = (b'', b'actual decoder error')
        monitor = Mock()
        monitor.creation_flags.side_effect = OSError('priority unavailable')
        monitor.started.side_effect = OSError('observe unavailable')
        monitor.poll.side_effect = OSError('observe unavailable')
        monitor.finished.side_effect = OSError('observe unavailable')
        with patch.object(core.subprocess, 'Popen', return_value=process) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'actual decoder error'):
                core.run_command(['ffmpeg.exe'], process_monitor=monitor)
        self.assertEqual(spawn.call_args.kwargs['creationflags'], core.EXTERNAL_PROCESS_CREATION_FLAGS)
        monitor.finished.assert_called_once_with(process)

    def test_expired_preparation_deadline_is_cancelled_without_priority_poll(self):
        process = self.process()
        monitor = Mock()
        monitor.creation_flags.return_value = core.EXTERNAL_PROCESS_CREATION_FLAGS
        deadline = product._DeadlineCancel(None, time.monotonic() - 1)
        with patch.object(core.subprocess, 'Popen', return_value=process), \
             patch.object(core, 'terminate_process_tree') as terminate:
            with self.assertRaises(core.CancelledError):
                core.run_command(['ffmpeg.exe'], cancel_event=deadline, process_monitor=monitor)
        terminate.assert_called_once_with(process)
        monitor.poll.assert_not_called()
        monitor.finished.assert_called_once_with(process)

    def test_pro_wrapper_only_adds_optional_keyword_when_enabled(self):
        monitor = object()
        with patch.object(core, 'run_command') as run:
            product._run(['tool'], core.log_noop, None)
            self.assertNotIn('process_monitor', run.call_args.kwargs)
            product._run(['tool'], core.log_noop, None, process_monitor=monitor)
            self.assertIs(run.call_args.kwargs['process_monitor'], monitor)


class PreparationPolicyRoutingTests(unittest.TestCase):
    def test_candidate_deadline_alone_never_creates_or_selects_policy(self):
        candidate = product._DeadlineCancel(None, time.monotonic() + 20)
        candidate.candidate_match_clock = True
        self.assertIsNone(product._audio_preparation_priority(candidate))
        preparation = product._DeadlineCancel(candidate, time.monotonic() + 180)
        policy = object()
        preparation._audio_priority_policy = policy
        nested = product._DeadlineCancel(preparation, time.monotonic() + 10)
        nested.candidate_match_clock = True
        self.assertIs(product._audio_preparation_priority(nested), policy)

    def test_attached_policy_uses_actual_remaining_not_parent_candidate_twenty_seconds(self):
        candidate = product._DeadlineCancel(None, 20)
        candidate.candidate_match_clock = True
        preparation = product._DeadlineCancel(candidate, 180)
        policy = object()
        with patch.object(continuous.time, 'monotonic', return_value=50), \
             patch('audio_priority.AudioPriorityPolicy', return_value=policy) as create:
            self.assertIs(continuous._attach_audio_priority(preparation, core.log_noop), policy)
        self.assertEqual(create.call_args.args[:2], (180, 130))
        self.assertEqual(candidate.deadline, 20)
        self.assertEqual(preparation.deadline, 180)

    def test_short_budget_does_not_opt_in_but_prior_boost_can_be_inherited(self):
        preparation = product._DeadlineCancel(None, 80)
        with patch.object(continuous.time, 'monotonic', return_value=50), \
             patch('audio_priority.AudioPriorityPolicy') as create:
            self.assertIsNone(continuous._attach_audio_priority(preparation, core.log_noop))
            create.assert_not_called()
            continuous._attach_audio_priority(preparation, core.log_noop, initial_promoted=True)
        self.assertEqual(create.call_args.args[:2], (80, 30))
        self.assertTrue(create.call_args.kwargs['initial_promoted'])

    def test_other_platform_remains_background(self):
        preparation = product._DeadlineCancel(None, time.monotonic() + 180)
        with patch.object(continuous.os, 'name', 'posix'), \
             patch('audio_priority.AudioPriorityPolicy') as create:
            self.assertIsNone(continuous._attach_audio_priority(preparation, core.log_noop))
        create.assert_not_called()

    def test_unknown_or_unbounded_clock_does_not_opt_in_to_scheduling(self):
        for deadline in (SimpleNamespace(), SimpleNamespace(deadline=None),
                         SimpleNamespace(deadline=float('inf'))):
            with self.subTest(deadline=deadline), patch('audio_priority.AudioPriorityPolicy') as create:
                self.assertIsNone(continuous._attach_audio_priority(deadline, core.log_noop))
            create.assert_not_called()

    def test_only_two_boolean_flags_are_inherited_and_failed_attempt_is_not_lost(self):
        policy_object = object()
        state = {'priority_promoted': 'true', 'priority_attempted': True,
                 'limit': 180, 'spent': 65, 'references': {.5: 'cached'},
                 'policy': policy_object}
        flags = continuous._audio_priority_flags(state)
        self.assertEqual(flags, {'priority_promoted': False, 'priority_attempted': True})
        continuous._merge_audio_priority_flags(state, None)
        self.assertIs(state['priority_attempted'], True)
        self.assertIs(state['priority_promoted'], False)
        self.assertEqual((state['limit'], state['spent'], state['references']), (180, 65, {.5: 'cached'}))
        self.assertIs(state['policy'], policy_object)


class SampledPriorityPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sampled_fixture.SampledIntegrationTests('test_50_success_stops_expansion_and_shifts_full_intact_cues')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.policy = RecordingPolicy()
        self.calls = []
        self.decode_count = 0
        self.fail_first_dts = False
        self.boost_on_decode = False
        self.attempt_on_decode = False
        original_run = self.fixture.fake_run

        def run(args, log, cancel=None, **kwargs):
            monitor = kwargs.get('process_monitor')
            self.calls.append((list(args), cancel, monitor))
            if args[0] == str(self.fixture.ffmpeg):
                self.decode_count += 1
                if '-progress' in args:
                    Path(args[args.index('-progress') + 1]).write_text('out_time_us=1000000\nprogress=continue\n')
                if self.boost_on_decode:
                    monitor.policy.promoted = True
                    monitor.policy.attempted = True
                elif self.attempt_on_decode and monitor is not None:
                    monitor.policy.attempted = True
                if self.fail_first_dts and self.decode_count == 1:
                    raise RuntimeError('No valid DCA sub-stream found')
            return original_run(args, log, cancel, **kwargs)

        patcher = patch.object(product, '_run', side_effect=run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, *, cache=None, cancel=None):
        if cancel is None:
            cancel = product._DeadlineCancel(None, time.monotonic() + 180)
            cancel._audio_priority_policy = self.policy
        return product._build_sampled_alignment_audio(
            str(self.fixture.movie), 7, cache or self.fixture.root / 'policy-cache',
            self.fixture.logs.append, cancel, self.fixture.duration)

    def test_decode_progress_is_unique_side_file_and_vad_only_inherits_policy(self):
        self.boost_on_decode = True
        reference = self.build()
        decode, vad = self.calls
        args, cancel, monitor = decode
        self.assertEqual(args.count('-progress'), 1)
        self.assertEqual(args[args.index('-stats_period') + 1], '2')
        self.assertIn('-nostats', args)
        self.assertLess(args.index('-progress'), args.index('-i'))
        self.assertEqual(monitor.media_seconds, self.fixture.duration * .5)
        self.assertEqual(monitor.future_media_seconds, 0)
        self.assertIs(vad[1], cancel)
        self.assertIsNone(vad[2].progress_path)
        self.assertIs(vad[2].policy, self.policy)
        self.assertTrue(vad[2].policy.promoted)
        self.assertFalse(Path(monitor.progress_path).exists())
        self.assertIsNotNone(product._sampled_reference_info(reference))

    def test_cache_hit_starts_no_process_or_monitor(self):
        self.build()
        self.calls.clear()
        self.policy.monitors.clear()
        self.build()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.policy.monitors, [])

    def test_no_explicit_policy_preserves_decoder_command_and_optional_kwargs(self):
        self.build(cancel=product._DeadlineCancel(None, time.monotonic() + 180))
        self.assertTrue(all(monitor is None for _, _, monitor in self.calls))
        self.assertNotIn('-progress', self.calls[0][0])

    def test_shared_audio_keeps_requested_budgets_with_real_existing_media_fixture(self):
        for limit in (180, 60, .5):
            with self.subTest(limit=limit):
                shared = self.fixture.prepare(budget=limit)
                self.assertEqual(shared.vad_preparation_state['limit'], limit)
                self.assertLess(shared.vad_preparation_state['spent'], limit)
                self.assertIsInstance(shared.vad_preparation_state['priority_promoted'], bool)

    def test_dts_retry_uses_new_progress_file_and_original_deadline(self):
        self.fixture.tracks[0].codec = 'DTS-HD Master Audio'
        self.fail_first_dts = True
        deadline = product._DeadlineCancel(None, time.monotonic() + 180)
        deadline._audio_priority_policy = self.policy
        original_deadline = deadline.deadline
        self.build(cancel=deadline)
        decodes = [call for call in self.calls if call[0][0] == str(self.fixture.ffmpeg)]
        self.assertEqual(len(decodes), 2)
        self.assertNotEqual(decodes[0][2].progress_path, decodes[1][2].progress_path)
        self.assertTrue(all(cancel is deadline for _, cancel, _ in decodes))
        self.assertEqual(deadline.deadline, original_deadline)
        self.assertTrue(all(not Path(m.progress_path).exists() for _, _, m in decodes))

    def test_extension_segments_share_remaining_budget_and_include_future_decode_work(self):
        created = []

        def attach(deadline, log, initial_promoted=False, initial_attempted=False):
            self.assertGreater(deadline.deadline, continuous.time.monotonic())
            policy = RecordingPolicy(initial_promoted, initial_attempted)
            deadline._audio_priority_policy = policy
            created.append((deadline, policy))
            return policy

        self.fixture.clock = sampled_fixture.FakeClock()
        with patch.object(continuous.time, 'monotonic', side_effect=self.fixture.clock.monotonic), \
             patch.object(continuous, '_attach_audio_priority', side_effect=attach):
            shared = self.fixture.prepare()
            shared.vad_preparation_state['priority_promoted'] = True
            self.fixture.minimum_fraction = .6
            self.fixture.preflight(shared)
        self.assertEqual(len(created), 2)
        initial, extension = created
        self.assertEqual(initial[0].deadline, 180)
        self.assertEqual(extension[0].deadline, 180)
        self.assertTrue(extension[1].promoted)
        self.assertTrue(extension[1].attempted)
        decoders = [m for m in extension[1].monitors if m.progress_path is not None]
        self.assertEqual(len(decoders), 2)
        self.assertGreater(decoders[0].future_media_seconds, 0)
        self.assertEqual(decoders[1].future_media_seconds, 0)
        self.assertTrue(shared.vad_preparation_state['priority_promoted'])
        self.assertTrue(shared.vad_preparation_state['priority_attempted'])
        json.dumps(shared.vad_preparation_state, default=lambda item: str(item) if isinstance(item, Path)
                   else self.fail('policy object leaked into serializable state'))

    def test_failed_priority_attempt_is_inherited_by_extension_without_promoted_flag(self):
        created = []

        def attach(deadline, log, initial_promoted=False, initial_attempted=False):
            policy = RecordingPolicy(initial_promoted, initial_attempted)
            deadline._audio_priority_policy = policy
            created.append(policy)
            return policy

        self.attempt_on_decode = True
        with patch.object(continuous, '_attach_audio_priority', side_effect=attach):
            shared = self.fixture.prepare()
            self.fixture.minimum_fraction = .6
            self.fixture.preflight(shared)
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].attempted)
        self.assertTrue(created[1].attempted)
        self.assertFalse(created[1].promoted)
        self.assertIs(shared.vad_preparation_state['priority_attempted'], True)
        self.assertIs(shared.vad_preparation_state['priority_promoted'], False)

    def test_failed_attempt_survives_short_remaining_extension_without_new_policy(self):
        shared = self.fixture.prepare()
        shared.vad_preparation_state.update(spent=130, priority_attempted=True,
                                          priority_promoted=False)
        self.fixture.minimum_fraction = .6
        with patch.object(continuous, '_attach_audio_priority', return_value=None) as attach:
            self.fixture.preflight(shared)
        self.assertTrue(attach.call_args.kwargs['initial_attempted'])
        self.assertLess(attach.call_args.args[0].deadline - time.monotonic(), 60)
        self.assertIs(shared.vad_preparation_state['priority_attempted'], True)
        self.assertIs(shared.vad_preparation_state['priority_promoted'], False)

    def test_failed_preparation_returns_attempt_flag_through_private_state(self):
        state = {'priority_promoted': False, 'priority_attempted': False}

        def fail_build(*args, **kwargs):
            policy = product._audio_preparation_priority(args[4])
            policy._attempted = True
            raise RuntimeError('original preparation failure')

        with patch.object(product, '_build_continuous_alignment_audio', side_effect=fail_build):
            with self.assertRaisesRegex(RuntimeError, 'original preparation failure'):
                continuous.shared_audio(str(self.fixture.movie), 7, self.fixture.logs.append,
                                        _priority_state=state)
        self.assertEqual(state, {'priority_promoted': False, 'priority_attempted': True})


class NativeAttemptBoundaryTests(unittest.TestCase):
    def test_failed_native_attempt_is_not_repeated_by_a_new_preparation_policy(self):
        clock = sampled_fixture.FakeClock()
        process = SimpleNamespace(poll=lambda: None)
        with patch.object(priority, 'IS_WINDOWS', True), \
             patch.object(priority, '_priority_class', return_value=priority.BELOW_NORMAL_PRIORITY_CLASS), \
             patch.object(priority, '_normal_priority', return_value=False) as promote:
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'first.progress'
                first = priority.AudioPriorityPolicy(180, 180, core.log_noop, clock=clock.monotonic)
                monitor = first.monitor(path, 3907.26)
                monitor.started(process)
                for wall in range(2, 50, 2):
                    clock.now = wall
                    with path.open('a') as stream:
                        stream.write(f'out_time_us={wall * 10000000}\nprogress=continue\n')
                    monitor.poll(process)
                self.assertTrue(first.attempted)
                self.assertFalse(first.promoted)
                promote.assert_called_once_with(process)
                second = priority.AudioPriorityPolicy(180, 132, core.log_noop,
                    clock=clock.monotonic, initial_attempted=first.attempted)
                path = Path(root) / 'second.progress'
                monitor = second.monitor(path, 3907.26)
                monitor.started(process)
                for wall in range(50, 100, 2):
                    clock.now = wall
                    with path.open('a') as stream:
                        stream.write(f'out_time_us={(wall - 48) * 10000000}\nprogress=continue\n')
                    monitor.poll(process)
                self.assertTrue(second.attempted)
                self.assertFalse(second.promoted)
                promote.assert_called_once_with(process)


class MainReferencePriorityStateTests(unittest.TestCase):
    def fixture(self):
        fixture = reference_fixture.FallbackTests('test_two_different_clocks_switch_once_and_return_actual_audio')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_main_fallback_inherits_successful_priority_once_and_only_boolean_state(self):
        fixture = self.fixture()
        previous = fixture.session.current
        previous.vad_preparation_state.update(priority_promoted=True, priority_attempted=True)
        captured = []

        def prepare(*args, **kwargs):
            flags = kwargs['_priority_state']
            captured.append(dict(flags))
            return reference_fixture.shared(1)

        def match(*args, shared, **kwargs):
            if shared.validation_audio_id == 2:
                fixture.fail()
            return Path('accepted.srt'), 'pass'

        with patch.object(continuous, '_preflight_reference', side_effect=match), \
             patch.object(continuous, 'shared_audio', side_effect=prepare):
            with self.assertRaises(product.SubtitleContentMismatchError):
                fixture.invoke(fixture.a)
            fixture.invoke(fixture.b)
        self.assertEqual(captured, [{'priority_promoted': True, 'priority_attempted': True}])
        self.assertEqual(fixture.session.validation_audio_id, 1)
        for shared in (previous, fixture.session.current):
            self.assertIs(shared.vad_preparation_state['priority_attempted'], True)
            self.assertIs(shared.vad_preparation_state['priority_promoted'], True)
            self.assertEqual(shared.vad_preparation_state['limit'], 180)

    def test_main_fallback_failure_writes_attempt_back_to_original_state(self):
        fixture = self.fixture()
        previous = fixture.session.current
        previous.vad_preparation_state.update(priority_promoted=False, priority_attempted=False)
        original_references = previous.vad_preparation_state['references']

        def prepare(*args, **kwargs):
            flags = kwargs['_priority_state']
            self.assertEqual(flags, {'priority_promoted': False, 'priority_attempted': False})
            flags['priority_attempted'] = True
            raise RuntimeError('original main preparation failure')

        with patch.object(continuous, '_preflight_reference', side_effect=fixture.fail), \
             patch.object(continuous, 'shared_audio', side_effect=prepare):
            with self.assertRaises(product.SubtitleContentMismatchError):
                fixture.invoke(fixture.a)
            with self.assertRaisesRegex(RuntimeError, 'original main preparation failure'):
                fixture.invoke(fixture.b)
        self.assertIs(fixture.session.current, previous)
        self.assertIs(previous.vad_preparation_state['priority_attempted'], True)
        self.assertIs(previous.vad_preparation_state['priority_promoted'], False)
        self.assertEqual(previous.vad_preparation_state['limit'], 180)
        self.assertIs(previous.vad_preparation_state['references'], original_references)


if __name__ == '__main__':
    unittest.main()
