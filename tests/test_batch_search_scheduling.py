from __future__ import annotations

import concurrent.futures
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core as batch
from profile_model import PreferenceProfile
import pro_core
import smart_subtitles as smart
import subtitle_tool_core as core


class BatchSearchSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.settings_gate = patch.object(smart, "_ONLINE_SERVICE_GATE", threading.Lock())
        self.settings_gate.start()
        self.addCleanup(self.settings_gate.stop)

    def test_batch_does_not_lock_two_films_around_the_entire_smart_pipeline(self):
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True,
                             False, False, False, False)]
        rendezvous = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
            plans = []
            with patch.object(batch, "_inspect_media_cached", return_value=({}, False)), \
                 patch.object(core, "tracks_from_media", return_value=tracks):
                for name in ("A", "B"):
                    movie = root / f"{name}.mkv"
                    movie.write_bytes(b"movie")
                    plans.append(batch.analyze_video(str(movie), profile))

            def find(movie, *_args, **_kwargs):
                # A long local VAD/matching phase must not block the other film
                # from entering its own smart pipeline.
                rendezvous.wait(timeout=2)
                subtitle = root / (Path(movie).stem + ".srt")
                subtitle.write_text("verified", encoding="utf-8")
                return smart.SmartSubtitleResult(
                    str(subtitle), "en", "pass", "Test", object())

            stack.enter_context(patch.object(core, "inspect_tracks", return_value=tracks))
            stack.enter_context(patch.object(smart, "find_verified_english", side_effect=find))
            stack.enter_context(patch.object(core, "begin_ollama_lease"))
            stack.enter_context(patch.object(core, "end_ollama_lease", return_value=False))
            stack.enter_context(patch.object(pro_core, "process_pro"))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(batch.process_plan, plan, profile,
                                       lambda _message: None, None)
                           for plan in plans]
                for future in futures:
                    future.result(timeout=3)

    def pipeline_patches(self, stack, service, prepare, verify):
        stack.enter_context(patch.object(smart, "PROVIDERS", (("Test", service, "key"),)))
        stack.enter_context(patch.object(smart, "filename_container_identity_conflict", return_value=""))
        stack.enter_context(patch.object(smart, "_downloaded_candidate_structure_conflict", return_value=""))
        stack.enter_context(patch.object(pro_core, "persistent_subtitle_logger",
                                        side_effect=lambda _movie, log: log))
        stack.enter_context(patch.object(pro_core, "prepare_shared_subtitle_content_audio",
                                        side_effect=prepare))
        stack.enter_context(patch.object(pro_core, "preflight_online_subtitle", side_effect=verify))
        stack.enter_context(patch.object(smart, "verification_seal", return_value="test-seal"))

    def test_local_vad_preparation_and_acceptance_do_not_hold_online_gate(self):
        preparation_rendezvous = threading.Barrier(2)
        first_acceptance = threading.Event()
        second_verified = threading.Event()
        release_first = threading.Event()
        search_calls = []

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "test-key"}

            @staticmethod
            def search(_key, movie, _language):
                search_calls.append(Path(movie).stem)
                candidate = SimpleNamespace(
                    file_id=Path(movie).stem, release="Movie", file_name="movie.srt", language="en")
                return None, [candidate], None

            @staticmethod
            def download(_key, candidate, destination):
                output = Path(destination) / "movie.srt"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(candidate.file_id, encoding="utf-8")
                return output

        def prepare(*_args):
            preparation_rendezvous.wait(timeout=2)
            return object()

        def verify(movie, subtitle, *_args, **_kwargs):
            if Path(movie).stem == "B":
                # B finishes matching even though A is still locally checking
                # its retained PGS subtitles.
                self.assertTrue(first_acceptance.wait(2))
                second_verified.set()
            return Path(subtitle), "pass"

        def accept_first(_subtitle, _cancel):
            first_acceptance.set()
            self.assertTrue(release_first.wait(2))

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            self.pipeline_patches(stack, Service, prepare, verify)
            movies = [str(Path(folder) / f"{name}.mkv") for name in ("A", "B")]
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(smart.find_verified_english, movies[0], 0,
                                    lambda _message: None, candidate_acceptance=accept_first)
                second = pool.submit(smart.find_verified_english, movies[1], 0,
                                     lambda _message: None)
                try:
                    self.assertTrue(second_verified.wait(2))
                    second.result(timeout=2)
                finally:
                    release_first.set()
                first.result(timeout=2)
        self.assertCountEqual(search_calls, ["A", "B"])

    def test_online_phases_are_serialized_and_release_after_failure(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first():
            with smart._online_service_phase(None):
                entered.set()
                self.assertTrue(release.wait(2))
                raise RuntimeError("provider failure")

        def second():
            self.assertTrue(entered.wait(2))
            with smart._online_service_phase(None):
                second_entered.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(first)
            self.assertTrue(entered.wait(2))
            b = pool.submit(second)
            try:
                self.assertFalse(second_entered.wait(0.15))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "provider failure"):
                a.result(timeout=2)
            b.result(timeout=2)
        self.assertTrue(second_entered.is_set())

    def test_cancel_while_waiting_online_does_not_call_service_or_release_owner(self):
        stop = threading.Event()
        waiting = threading.Event()
        called = threading.Event()
        smart._ONLINE_SERVICE_GATE.acquire()

        def run():
            waiting.set()
            with smart._online_service_phase(stop):
                called.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            self.assertTrue(waiting.wait(2))
            stop.set()
            try:
                with self.assertRaises(core.CancelledError):
                    future.result(timeout=2)
                self.assertFalse(called.is_set())
                self.assertTrue(smart._ONLINE_SERVICE_GATE.locked())
            finally:
                smart._ONLINE_SERVICE_GATE.release()

    def test_download_wave_keeps_prefetch_parallelism_but_blocks_other_network_phases(self):
        rendezvous = threading.Barrier(smart.SMART_DOWNLOAD_WORKERS + 1)
        release = threading.Event()
        other_entered = threading.Event()
        count_lock = threading.Lock()
        active = 0
        peak = 0

        class Service:
            @staticmethod
            def download(_key, candidate, _directory):
                nonlocal active, peak
                with count_lock:
                    active += 1
                    peak = max(peak, active)
                rendezvous.wait(timeout=2)
                if not release.wait(2):
                    raise RuntimeError("test release timed out")
                with count_lock:
                    active -= 1
                return Path(str(candidate))

        wave = [{"service": Service, "key": "test", "candidate": str(i),
                 "directory": Path("unused")} for i in range(smart.SMART_DOWNLOAD_WORKERS)]

        def another_phase():
            with smart._online_service_phase(None):
                other_entered.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=smart.SMART_DOWNLOAD_WORKERS) as downloads, \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as films:
            first = films.submit(smart._download_wave, downloads, wave, None)
            rendezvous.wait(timeout=2)
            second = films.submit(another_phase)
            try:
                self.assertFalse(other_entered.wait(0.15))
            finally:
                release.set()
            outcomes = first.result(timeout=2)
            second.result(timeout=2)
        self.assertEqual(peak, smart.SMART_DOWNLOAD_WORKERS)
        self.assertEqual(len(outcomes), smart.SMART_DOWNLOAD_WORKERS)
        self.assertTrue(all(error is None for _path, error in outcomes.values()))

    def test_cancelled_active_download_retains_gate_until_request_exits(self):
        stop = threading.Event()
        download_entered = threading.Event()
        release_download = threading.Event()
        other_entered = threading.Event()

        class Service:
            @staticmethod
            def download(*_args):
                download_entered.set()
                if not release_download.wait(2):
                    raise RuntimeError("test release timed out")
                return Path("subtitle.srt")

        wave = [{"service": Service, "key": "test", "candidate": object(),
                 "directory": Path("unused")}]

        def another_phase():
            with smart._online_service_phase(None):
                other_entered.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as downloads, \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as films:
            first = films.submit(smart._download_wave, downloads, wave, stop)
            self.assertTrue(download_entered.wait(2))
            stop.set()
            second = films.submit(another_phase)
            try:
                self.assertFalse(other_entered.wait(0.15))
            finally:
                release_download.set()
            with self.assertRaises(core.CancelledError):
                first.result(timeout=2)
            second.result(timeout=2)
        self.assertTrue(other_entered.is_set())

    def test_download_failure_is_reported_per_candidate_and_does_not_leak_gate(self):
        class Service:
            @staticmethod
            def download(*_args):
                raise RuntimeError("429 quota")

        wave = [{"service": Service, "key": "test", "candidate": object(),
                 "directory": Path("unused")}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            outcomes = smart._download_wave(executor, wave, None)
        self.assertIsNone(outcomes[id(wave[0])][0])
        self.assertRegex(str(outcomes[id(wave[0])][1]), "quota")
        with smart._online_service_phase(None):
            pass


if __name__ == "__main__":
    unittest.main()
