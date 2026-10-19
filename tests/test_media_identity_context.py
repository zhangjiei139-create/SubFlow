"""Context-aware title/year regressions, including real search-chain guards.

These fixtures exercise identity admission only. Catalogue-compatible metadata
must still pass the separate subtitle content and timing checks. All provider,
media and preflight operations in the search-chain fixtures are mocked.
"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import re
import tempfile
import unittest
from unittest.mock import patch

import online_subtitles as online
import smart_subtitles as smart
import subtitle_identity_guard as guard
import subdl_subtitles as subdl


def identity(title, year='', **kwargs):
    return online.MediaIdentity(title, title, year, **kwargs)

def candidate(title, year='', *, hash_match=False, release=None, **kwargs):
    release = release or '.'.join(value for value in (title, year, 'BluRay') if value)
    return online.SubtitleCandidate(1, release + '.srt', release, 'en', 0, 0,
        False, False, moviehash_match=hash_match, feature_title=title,
        feature_year=year, **kwargs)

class FilenameAndCatalogueTests(unittest.TestCase):
    def test_numeric_title_and_explicit_release_year_are_separate(self):
        fixtures = [
            ('1917.2019.1080p.BluRay.x265.mkv', '1917', '2019'),
            ('1408.2007.1080p.BluRay.mkv', '1408', '2007'),
            ('2012.2009.2160p.UHD.BluRay.mkv', '2012', '2009'),
            ('1984.1984.1080p.BluRay.mkv', '1984', '1984'),
            ('The.Legend.of.1900.1998.1080p.BluRay.mkv', 'The Legend of 1900', '1998'),
            ('Class.of.1984.1982.1080p.BluRay.mkv', 'Class of 1984', '1982'),
            ('Blade.Runner.2049.2017.2160p.BluRay.mkv', 'Blade Runner 2049', '2017'),
            ('2001.A.Space.Odyssey.1968.2160p.BluRay.mkv', '2001 A Space Odyssey', '1968'),
            ('The.39.Steps.1935.1080p.BluRay.mkv', 'The 39 Steps', '1935'),
            ('300.2006.1080p.BluRay.mkv', '300', '2006'),
        ]
        for filename, title, year in fixtures:
            with self.subTest(filename=filename):
                self.assertEqual(guard.clean_video_title(filename), (title, year))

    @patch.object(guard, "date", SimpleNamespace(today=lambda: date(2026, 10, 18)))
    def test_undated_number_is_not_invented_as_a_release_year(self):
        for filename, title in (
            ('1917.mkv', '1917'), ('1917.1080p.BluRay.mkv', '1917'),
            ('1408.2160p.WEB-DL.mkv', '1408'), ('2012.1080p.BluRay.mkv', '2012'),
            ('2046.1080p.BluRay.mkv', '2046'),
            ('Blade.Runner.2049.2160p.BluRay.mkv', 'Blade Runner 2049'),
            ('2001.A.Space.Odyssey.1080p.BluRay.mkv', '2001 A Space Odyssey'),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(guard.clean_video_title(filename), (title, ''))

    def test_catalogue_title_context_preserves_all_title_numbers(self):
        for title in ('1917', '2012', '1984', '2001: A Space Odyssey',
                      'Blade Runner 2049', 'The Legend of 1900', 'Class of 1984'):
            with self.subTest(title=title):
                result = guard.parse_media_identity(title, kind='title')
                self.assertEqual(re.findall(r'[A-Za-z0-9]+', result.title),
                                 re.findall(r'[A-Za-z0-9]+', title))
                self.assertEqual(result.year, '')
                self.assertEqual(result.year_confidence, 'unknown')

    def test_explicit_known_title_and_technical_tail_do_not_steal_title_digits(self):
        result = guard.parse_media_identity('Blade.Runner.2049.2017.2160p.BluRay',
            kind='release', known_titles=('Blade Runner 2049',))
        self.assertEqual((result.title, result.year), ('Blade Runner 2049', '2017'))
        self.assertEqual(result.year_confidence, 'explicit')

    def test_technical_remaster_tail_does_not_replace_the_film_year(self):
        for name, title, year in (
            ('Fury.2014.1080p.BluRay.2020.Remaster.mkv', 'Fury', '2014'),
            ('Once.Upon.a.Time.in.Hollywood.2019.2160p.BluRay.2024.Remaster.mkv',
             'Once Upon a Time in Hollywood', '2019'),
        ):
            with self.subTest(name=name):
                self.assertEqual(guard.clean_video_title(name), (title, year))

    def test_ambiguous_historical_title_number_is_not_declared_explicit(self):
        for name in ('The.Legend.of.1900.1080p.BluRay.mkv',
                     'Class.of.1984.1080p.BluRay.mkv'):
            with self.subTest(name=name):
                parsed = guard.parse_media_identity(name, kind='filename')
                self.assertIn(parsed.year_confidence, ('unknown', 'inferred'))

    def test_intact_container_title_recovers_an_ambiguous_historical_number(self):
        for name, title in (
            ('The.Legend.of.1900.1080p.BluRay.mkv', 'The Legend of 1900'),
            ('Class.of.1984.1080p.BluRay.mkv', 'Class of 1984'),
        ):
            with self.subTest(name=name):
                filename, internal = guard.parse_filename_and_container(name, title)
                self.assertEqual(filename.title, title)
                self.assertEqual(filename.year, '')
                self.assertEqual(internal.title, title)

    def test_non_numeric_films_keep_the_same_filename_identity(self):
        for filename, title, year in (
            ('Fury.2014.2160p.BluRay.mkv', 'Fury', '2014'),
            ('Se7en.1995.2160p.BluRay.mkv', 'Se7en', '1995'),
            ('No.Time.to.Die.2021.1080p.BluRay.mkv', 'No Time to Die', '2021'),
            ('Top.Gun.Maverick.2022.2160p.BluRay.mkv', 'Top Gun Maverick', '2022'),
            ('Movie.Part.II.1998.BluRay.mkv', 'Movie Part II', '1998'),
            ('封神第一部（2023）BluRay.mkv', '封神第一部', '2023'),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(guard.clean_video_title(filename), (title, year))

    def test_providers_share_expected_filename_and_container_identity(self):
        for filename, container, title, year in (
            ('1917.2019.1080p.BluRay.mkv', '', '1917', '2019'),
            ('1917.1080p.BluRay.mkv', '', '1917', ''),
            ('1917.mkv', '1917 (2019)', '1917', '2019'),
            ('2012.2009.2160p.BluRay.mkv', '2012', '2012', '2009'),
            ('The.Legend.of.1900.1080p.mkv', 'The Legend of 1900', 'The Legend of 1900', ''),
            ('Blade.Runner.2049.2017.2160p.mkv', 'Blade Runner 2049', 'Blade Runner 2049', '2017'),
            ('Fury.2014.1080p.BluRay.mkv', '', 'Fury', '2014'),
        ):
            for provider in (online, subdl):
                with self.subTest(provider=provider.__name__, filename=filename), \
                     patch.object(provider, 'container_title', return_value=container):
                    actual = provider.identify_media(filename)
                    self.assertEqual((actual.title, actual.year), (title, year))

    def test_known_title_prefix_does_not_swallow_a_real_next_title_word(self):
        filename, internal = guard.parse_filename_and_container(
            'Freakier.2025.Bluray.1080p.mkv', 'FREAKIER FRIDAY - BLU-RAY')
        self.assertEqual((filename.title, filename.year), ('Freakier', '2025'))
        self.assertEqual(internal.title, 'FREAKIER FRIDAY')
        self.assertEqual(internal.year, '')
        self.assertFalse(internal.title_protected)

    def test_underscore_technical_separator_does_not_become_title_content(self):
        for name, title, year in (
            ('Film_1080p_BluRay.mkv', 'Film', ''),
            ('Fury_2014_1080p_BluRay.mkv', 'Fury', '2014'),
            ('1917_2019_1080p_BluRay.mkv', '1917', '2019'),
        ):
            with self.subTest(name=name):
                self.assertEqual(guard.clean_video_title(name), (title, year))


class YearAndContainerCounterexamples(unittest.TestCase):
    def test_title_number_does_not_reject_a_correct_release(self):
        for video, release, year in (
            ('1917.2019.1080p.mkv', '1917.2019.BluRay', '2019'),
            ('1917.1080p.mkv', '1917.2019.BluRay', '2019'),
            ('2012.2009.2160p.mkv', '2012.2009.BluRay', '2009'),
            ('Class.of.1984.1982.1080p.mkv', 'Class.of.1984.1982.BluRay', '1982'),
            ('Blade.Runner.2049.2017.2160p.mkv', 'Blade.Runner.2049.2017.BluRay', '2017'),
        ):
            for exact in (False, True):
                with self.subTest(video=video, exact_hash=exact):
                    self.assertEqual(guard.release_year_conflict(video, release, year,
                                                                exact_hash=exact), '')

    def test_explicit_wrong_year_and_remakes_still_reject_with_hash(self):
        for video, release, year in (
            ('1917.2019.1080p.mkv', '1917.2016.BluRay', '2016'),
            ('Suspiria.1977.1080p.mkv', 'Suspiria.2018.BluRay', '2018'),
            ('The.Thing.1982.1080p.mkv', 'The.Thing.2011.BluRay', '2011'),
            ('Heat.1995.1080p.mkv', 'Heat.1986.BluRay', '1986'),
        ):
            with self.subTest(video=video):
                self.assertTrue(guard.release_year_conflict(video, release, year, exact_hash=True))

    def test_declared_wrong_year_without_release_year_still_rejects(self):
        self.assertTrue(guard.release_year_conflict('Suspiria.1977.1080p.mkv',
            'Suspiria.BluRay', '2018', exact_hash=True))

    def test_unqualified_conflicting_year_after_source_tag_is_not_hidden(self):
        for video, release, declared in (
            ('Fury.2014.BluRay.mkv', 'Fury.BluRay.2019', '2014'),
            ('The.Thing.1982.BluRay.mkv', 'The.Thing.BluRay.2011', '1982'),
        ):
            with self.subTest(release=release):
                self.assertTrue(guard.release_year_conflict(video, release, declared, exact_hash=True))

    def test_missing_container_release_year_does_not_turn_title_number_into_year(self):
        for filename, container in (
            ('1917.2019.1080p.mkv', '1917'),
            ('Blade.Runner.2049.2017.2160p.mkv', 'Blade Runner 2049'),
            ('The.Legend.of.1900.1998.1080p.mkv', 'The Legend of 1900'),
            ('1917.1080p.mkv', '1917 (2019)'),
        ):
            with self.subTest(filename=filename), patch.object(guard, 'container_title', return_value=container):
                self.assertEqual(guard.filename_container_identity_conflict(filename), '')

    def test_explicit_container_remake_conflict_is_preserved(self):
        with patch.object(guard, 'container_title', return_value='Suspiria (2018)'):
            self.assertTrue(guard.filename_container_identity_conflict('Suspiria.1977.BluRay.mkv'))

class SmartPerFieldAdmissionTests(unittest.TestCase):
    def exercise(self, release, filename, *, expect_accept):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / '1917.2019.1080p.BluRay.mkv'
            movie.write_bytes(b'identity-only fixture; never decoded')
            item = candidate('1917', '2019', hash_match=True, release=release)
            item.file_name = filename
            downloads = []

            class Service:
                load_settings = staticmethod(lambda: {'key': 'mock-key-never-sent'})
                search = staticmethod(lambda *args, **kwargs:
                    (identity('1917', '2019'), [item], online.SearchMeta(query_mode='hash')))

                @staticmethod
                def download(_key, row, destination):
                    downloads.append(row.file_id)
                    path = Path(destination) / row.file_name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello there.\n', encoding='utf8')
                    return path

            with patch.object(smart, 'PROVIDERS', (('Fixture', Service, 'key'),)), \
                 patch.object(smart, 'filename_container_identity_conflict', return_value=''), \
                 patch.object(smart, 'subtitle_content_conflict', return_value=''), \
                 patch.object(smart.pro_core, 'persistent_subtitle_logger', side_effect=lambda _movie, log: log), \
                 patch.object(smart.pro_core, 'prepare_shared_subtitle_content_audio', return_value=object()), \
                 patch.object(smart.pro_core, 'preflight_online_subtitle',
                     side_effect=lambda _movie, path, *args, **kwargs: (Path(path), 'verified')) as preflight:
                if expect_accept:
                    result = smart.find_verified_english(str(movie), 1, lambda _message: None)
                    self.assertTrue(Path(result.subtitle_path).is_file())
                    self.assertEqual(downloads, [1])
                    preflight.assert_called_once()
                else:
                    with self.assertRaises(RuntimeError):
                        smart.find_verified_english(str(movie), 1, lambda _message: None)
                    self.assertEqual(downloads, [])
                    preflight.assert_not_called()

    def test_bare_numeric_title_in_both_release_fields_reaches_preflight(self):
        self.exercise('1917', '1917.srt', expect_accept=True)

    def test_either_explicit_wrong_year_field_rejects_before_download(self):
        for release, filename in (
            ('1917.2017.BluRay', '1917.2019.BluRay.srt'),
            ('1917.2019.BluRay', '1917.2017.BluRay.srt'),
        ):
            with self.subTest(release=release, filename=filename):
                self.exercise(release, filename, expect_accept=False)

    def test_conflicting_year_after_source_tag_in_either_field_rejects(self):
        for release, filename in (
            ('1917.BluRay.2017', '1917.srt'),
            ('1917', '1917.BluRay.2017.srt'),
        ):
            with self.subTest(release=release, filename=filename):
                self.exercise(release, filename, expect_accept=False)


if __name__ == '__main__':
    unittest.main()
