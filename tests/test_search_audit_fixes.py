from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import online_subtitles as online
import pro_core
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_identity_guard as guard


class MediaRevisionSealTests(unittest.TestCase):
    def test_video_size_or_timestamp_change_invalidates_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie, subtitle = root / "Film.2020.mkv", root / "verified.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8")
            for change in ("size", "mtime"):
                with self.subTest(change=change):
                    movie.write_bytes(b"original")
                    before = movie.stat()
                    args = (str(movie), str(subtitle), "OpenSubtitles", "Film.2020", "imdb:1")
                    seal = guard.verification_seal(*args)
                    smart.verify_before_processing(*args, seal)
                    if change == "size":
                        movie.write_bytes(b"a different edition")
                        os.utime(movie, ns=(before.st_atime_ns, before.st_mtime_ns))
                    else:
                        os.utime(movie, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                    with self.assertRaisesRegex(RuntimeError, "影片.*发生变化"):
                        smart.verify_before_processing(*args, seal)

    def test_missing_movie_cannot_receive_a_verification_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "subtitle.srt"
            subtitle.write_text("subtitle", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                guard.verification_seal(str(root / "missing.mkv"), str(subtitle), "SubDL", "Film", "1")


class FilenameIdentityTests(unittest.TestCase):
    def test_both_providers_parse_underscore_years_and_preserve_title_numbers(self):
        for filename, expected in (
            ("Film_2020_1080p_BluRay.mkv", ("Film", "2020")),
            ("Film.2020.1080p.BluRay.mkv", ("Film", "2020")),
            ("The_Legend_of_1900_1998_1080p.mkv", ("The Legend of 1900", "1998")),
            ("2001_A_Space_Odyssey_1968_BluRay.mkv", ("2001 A Space Odyssey", "1968")),
            ("Film_1080p_BluRay.mkv", ("Film", "")),
        ):
            for service in (online, subdl):
                with self.subTest(filename=filename, service=service.__name__):
                    self.assertEqual(service.clean_video_title(filename), expected)

    def test_subdl_accepts_exact_movie_for_underscore_filename(self):
        title, year = subdl.clean_video_title("Film_2020_1080p_BluRay.mkv")
        movie = {"sd_id": 99, "name": "Film", "original_name": "Film", "year": 2020}
        self.assertIs(subdl._choose_movie_without_score(subdl.MediaIdentity(title, title, year), [movie]), movie)
        self.assertIsNone(subdl._choose_movie_without_score(subdl.MediaIdentity(title, title, year),
                                                          [dict(movie, year=1990)]))


class ReleaseLabelTests(unittest.TestCase):
    def test_cam_title_is_allowed_but_actual_source_label_is_rejected(self):
        movie = "Cam.2018.1080p.BluRay.mkv"
        self.assertEqual(guard.source_conflict(movie, "Cam.2018.1080p.BluRay.srt"), "")
        for label in ("CAM", "HDCAM", "TS", "HDTS", "TC", "HDTC"):
            with self.subTest(label=label):
                self.assertIn("影院片源", guard.source_conflict(movie, f"Cam.2018.1080p.{label}.srt"))
        self.assertIn("影院片源", guard.source_conflict(movie, "CAM" + ".2018.CAM.srt"))

    def test_role_title_prefix_does_not_hide_real_trailing_role_labels(self):
        for title, bad_tag in (("Trailer Park Boys", "trailer"), ("Forced Entry", "forced"),
                               ("Sample People", "sample"), ("Commentary", "commentary")):
            identity = online.MediaIdentity(title, title, "2006")
            release = title.replace(" ", ".") + ".2006.BluRay"
            candidate = SimpleNamespace(release=release, file_name=release + ".srt")
            with self.subTest(title=title):
                self.assertEqual(smart._candidate_role_conflict(candidate, identity), "")
                for suffix in ("." + bad_tag, "_" + bad_tag):
                    # The bad marker may exist only in the download's filename.
                    candidate.file_name = release + suffix + ".srt"
                    self.assertTrue(smart._candidate_role_conflict(candidate, identity))

    def test_unknown_identity_cannot_whitelist_a_role_and_substrings_are_not_titles(self):
        candidate = SimpleNamespace(release="Trailer.Park.Boys.2006", file_name="film.srt")
        self.assertTrue(smart._candidate_role_conflict(candidate))
        self.assertEqual(guard.release_metadata("Forcedly.2020.forced", ("Forced",)), "Forcedly.2020.forced")


class UnpackedSubdlTests(unittest.TestCase):
    @staticmethod
    def candidates(count=2, child_ids=True):
        children = [{"name": f"version-{index}.srt", "url": f"/unpack/{index}.srt",
                     **({"file_n_id": 100 + index} if child_ids else {})}
                    for index in range(count)]
        return subdl._candidates_from_response({"subtitles": [{
            "n_id": 42, "language": "EN", "release_name": "Film.2020.BluRay", "unpack_files": children,
        }]}, "en")

    def test_child_ids_override_parent_id_and_urls_disambiguate_missing_child_ids(self):
        with_ids = self.candidates()
        self.assertEqual([item.file_id for item in with_ids], ["100", "101"])
        without_ids = self.candidates(child_ids=False)
        self.assertEqual([item.file_id for item in without_ids], ["42", "42"])
        self.assertEqual(len({smart.candidate_key("SubDL", item) for item in without_ids}), 2)
        self.assertEqual(smart.candidate_key("SubDL", without_ids[0]),
                         smart.candidate_key("SubDL", without_ids[0]))
        different_case = SimpleNamespace(file_id="42", download_url=without_ids[0].download_url.upper())
        self.assertNotEqual(smart.candidate_key("SubDL", without_ids[0]),
                            smart.candidate_key("SubDL", different_case))

    def run_search(self, *, candidates, accept_index=None, download_fail=False, max_candidates=5,
                   title="Film", year="2020"):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            movie = Path(directory) / f"{title}.{year}.BluRay.mkv"
            movie.write_bytes(b"test media")
            downloaded, checked = [], []
            class Service:
                @staticmethod
                def load_settings():
                    return {"key": "fixture-only"}
                @staticmethod
                def search(*_args, **_kwargs):
                    return online.MediaIdentity(title, title, year), candidates, online.SearchMeta()
                @staticmethod
                def download(_key, candidate, destination):
                    index = int(Path(candidate.download_url).stem)
                    downloaded.append(index)
                    if download_fail:
                        raise RuntimeError("fixture download failure")
                    subtitle = Path(destination) / candidate.file_name
                    subtitle.parent.mkdir(parents=True, exist_ok=True)
                    subtitle.write_text(f"1\n00:00:01,000 --> 00:00:02,000\nVersion {index}.\n", encoding="utf-8")
                    return subtitle
            def preflight(_video, path, *_args, **_kwargs):
                index = int(Path(path).stem.split("-")[-1])
                checked.append(index)
                if index == accept_index:
                    return Path(path), "accepted"
                raise RuntimeError("fixture candidate mismatch")
            stack.enter_context(patch.object(smart, "PROVIDERS", (("SubDL", Service, "key"),)))
            stack.enter_context(patch.object(smart, "filename_container_identity_conflict", return_value=""))
            stack.enter_context(patch.object(pro_core, "persistent_subtitle_logger", side_effect=lambda _movie, log: log))
            stack.enter_context(patch.object(pro_core, "prepare_shared_subtitle_content_audio", return_value=object()))
            stack.enter_context(patch.object(pro_core, "preflight_online_subtitle", side_effect=preflight))
            try:
                result = smart.find_verified_english(str(movie), 1, lambda _: None, max_candidates=max_candidates)
            except RuntimeError:
                result = None
            return result, downloaded, checked

    def test_second_subfile_is_reachable_after_first_fails_and_duplicates_stay_filtered(self):
        candidates = self.candidates(child_ids=False)
        candidates.insert(1, candidates[0])
        result, downloaded, checked = self.run_search(candidates=candidates, accept_index=1)
        self.assertIsNotNone(result)
        self.assertEqual(downloaded, [0, 1])
        self.assertEqual(checked, [0, 1])

    def test_film_title_words_reach_formal_verification(self):
        for title in ("Cam", "Trailer Park Boys", "Forced Entry"):
            with self.subTest(title=title):
                candidates = self.candidates(1)
                candidates[0].release = title.replace(" ", ".") + ".2020.BluRay"
                candidates[0].file_name = candidates[0].release + "-0.srt"
                result, downloaded, checked = self.run_search(candidates=candidates, accept_index=0, title=title)
                self.assertIsNotNone(result)
                self.assertEqual(downloaded, [0])
                self.assertEqual(checked, [0])

    def test_unpacked_variants_still_obey_formal_candidate_limit(self):
        result, downloaded, checked = self.run_search(candidates=self.candidates(20), max_candidates=3)
        self.assertIsNone(result)
        self.assertEqual(checked, [0, 1, 2])
        self.assertEqual(len(downloaded), 3)

    def test_failed_unpacked_downloads_still_obey_download_attempt_limit(self):
        result, downloaded, checked = self.run_search(candidates=self.candidates(20), max_candidates=3, download_fail=True)
        self.assertIsNone(result)
        self.assertEqual(len(downloaded), 3 * smart.SMART_DOWNLOAD_ATTEMPT_MULTIPLIER)
        self.assertEqual(checked, [])


if __name__ == "__main__":
    unittest.main()
