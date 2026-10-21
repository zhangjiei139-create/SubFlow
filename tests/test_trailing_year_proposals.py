"""Filename proposals require positive catalogue evidence; transport is mocked."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import json
import tempfile
import threading
import unittest

import online_subtitles as online
import subdl_subtitles as subdl
import subtitle_identity_guard as guard
import work_identity_resolver as resolver


NAME = '2010 The Year We Make Contact 1984.mkv'
FULL = '2010 The Year We Make Contact'


def row(title='2010', year='1984', imdb='86837', tmdb='4437', **extra):
    return dict(name=title, original_name=title, year=year, imdb_id=imdb,
                tmdb_id=tmdb, type='movie', sd_id='17', **extra)


def titles_page(alias='2010: The Year We Make Contact', *, title='2010',
                year='1984', canonical_id='4437', og_id='4437', table_class='card releases titles', extra=''):
    return (f'<html><head><link rel="canonical" href="https://www.themoviedb.org/movie/{canonical_id}-2010/titles">'
            f'<meta property="og:url" content="https://www.themoviedb.org/movie/{og_id}-2010">'
            f'<meta property="og:title" content="{title}"></head><body>'
            f'<div class="header small first"><section class="header small"><div class="title">'
            f'<h2><a href="/movie/4437-2010?language=en-US">{title}</a>'
            f'<span class="tag release_date">({year})</span></h2></div></section></div>'
            f'<div id="media_v4"><div class="white_column"><section class="panel">'
            f'<table class="{table_class}"><thead><tr><th>Title</th></tr></thead>'
            f'<tbody><tr><td>{alias}</td><td></td></tr></tbody></table></section></div></div>{extra}</body></html>')


def unresolved(name=NAME):
    title, year = guard.clean_video_title(name)
    return online.MediaIdentity(title, title, year)


class FilenameProposalTests(unittest.TestCase):
    def test_proposal_retains_complete_raw_title_and_does_not_change_parser(self):
        proposal = guard.trailing_filename_year_proposal(NAME)
        self.assertEqual((proposal.title, proposal.year), (FULL, '1984'))
        self.assertEqual((proposal.year_confidence, proposal.year_source),
                         ('inferred', 'filename-space-tail-proposal'))
        self.assertEqual(proposal.raw_title, Path(NAME).stem)
        self.assertEqual(guard.clean_video_title(NAME), (Path(NAME).stem, ''))

    def test_numeric_titles_and_ordinary_space_names_get_one_proposal(self):
        for name, title, year in (
            ('1917 2019.mkv', '1917', '2019'), ('1984 1956.mkv', '1984', '1956'),
            ('2012 2009.mkv', '2012', '2009'), ('1408 2007.mkv', '1408', '2007'),
            ('2001 A Space Odyssey 1968.mkv', '2001 A Space Odyssey', '1968'),
            ('Blade Runner 2049 2017.mkv', 'Blade Runner 2049', '2017'),
            ('Class of 1984 1982.mkv', 'Class of 1984', '1982'),
            ('Fury 2014.mkv', 'Fury', '2014'), ('X 2022.mkv', 'X', '2022'),
        ):
            with self.subTest(name=name):
                proposal = guard.trailing_filename_year_proposal(name)
                self.assertEqual((proposal.title, proposal.year), (title, year))

    def test_catalogue_text_and_existing_dated_formats_do_not_get_new_proposals(self):
        for name in ('1917.mkv', '1984.mkv', '2012.mkv', FULL,
                     '1917.2019.BluRay.mkv', 'Fury_2014_1080p.mkv',
                     'Movie (2020).mkv', 'Blade Runner 2049.mkv', 'Film 2014 final.mkv'):
            with self.subTest(name=name):
                self.assertIsNone(guard.trailing_filename_year_proposal(name))


class EvidenceTests(unittest.TestCase):
    def recover(self, rows=None, page=None, session=None, name=NAME):
        session = session or resolver.RecoverySession(cache_path=False)
        with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': rows if rows is not None else [row()]}) as secondary, \
             patch.object(resolver, 'fetch_public_movie', return_value=page or titles_page()) as public:
            result = resolver.recover_filename_year(name, unresolved(name), session)
        return result, session, secondary, public

    def test_complete_prefix_and_year_can_confirm_literal_catalogue_title_in_one_request(self):
        result, session, secondary, public = self.recover(rows=[row(FULL)])
        self.assertEqual(result.state, 'confirmed')
        self.assertEqual(result.title, FULL)
        self.assertEqual(session.budget.request_count, 1)
        self.assertEqual(secondary.call_args.args[0], FULL)
        public.assert_not_called()

    def test_single_numeric_catalogue_title_requires_complete_formal_alias(self):
        result, session, secondary, public = self.recover()
        self.assertEqual((result.state, result.title, result.year), ('confirmed', '2010', '1984'))
        self.assertEqual(session.budget.request_count, 2)
        self.assertTrue(public.call_args.args[1].public_titles)
        self.assertTrue(resolver.validate_filename_resolution(NAME, unresolved(), result))
        self.assertIs(session.confirmed_resolution, result)

    def test_numeric_prefix_alone_or_unrelated_formal_alias_cannot_confirm(self):
        result, _, _, _ = self.recover(page=titles_page(alias='2010: A Different Subtitle'))
        self.assertEqual(result.state, 'pending')

    def test_ordinary_one_word_prefix_cannot_replace_full_local_title(self):
        result, _, _, public = self.recover(name='Contact Some Other Words 1997.mkv',
                                          rows=[row('Contact', '1997')])
        self.assertEqual(result.state, 'pending')
        public.assert_not_called()

    def test_wrong_year_competing_double_id_and_single_id_competitor_reject(self):
        for rows in ([row(year='1985')], [row(), row(imdb='999', tmdb='998')],
                     [row(), row(imdb='999', tmdb='')], [row(), row(imdb='', tmdb='998')]):
            with self.subTest(rows=rows):
                result, _, _, _ = self.recover(rows=rows)
                self.assertEqual(result.state, 'pending')

    def test_raw_full_title_record_protects_numbers_even_without_ids(self):
        for title in ('Class of 1984', 'The Legend of 1900'):
            name = title + '.mkv'
            session = resolver.RecoverySession(cache_path=False)
            profiles = [resolver.WorkProfile(title, '', '', '', 'fixture')]
            with self.subTest(title=title), patch.object(resolver, 'fetch_secondary_catalogue') as fetch:
                result = resolver.recover_filename_year(name, unresolved(name), session, profiles)
                self.assertEqual(result.state, 'pending')
                fetch.assert_not_called()
                self.assertIsNone(session.budget)

    def test_full_title_protection_survives_provider_change(self):
        session = resolver.RecoverySession(cache_path=False)
        base = unresolved('Class of 1984.mkv')
        session.protect_full_title(base, [resolver.WorkProfile('Class of 1984', '', '', '')])
        with patch.object(resolver, 'fetch_secondary_catalogue') as fetch:
            result = resolver.recover_filename_year('Class of 1984.mkv', base, session, ())
        self.assertEqual(result.state, 'pending')
        fetch.assert_not_called()

    def test_confirmed_result_reuse_makes_no_third_request(self):
        result, session, _, _ = self.recover()
        with patch.object(resolver, 'fetch_secondary_catalogue') as secondary, \
             patch.object(resolver, 'fetch_public_movie') as public:
            repeated = resolver.recover_filename_year(NAME, unresolved(), session)
        self.assertIs(repeated, result)
        self.assertEqual(session.budget.request_count, 2)
        secondary.assert_not_called()
        public.assert_not_called()

    def test_one_request_limit_and_zero_time_do_not_restart_budgets(self):
        for session, expected in ((resolver.RecoverySession(cache_path=False, max_requests=1), 1),
                                  (resolver.RecoverySession(cache_path=False, time_budget_seconds=0), 0)):
            with self.subTest(expected=expected):
                result, session, _, _ = self.recover(session=session)
                self.assertEqual(result.state, 'pending')
                self.assertEqual(session.budget.request_count, expected)

    def test_user_cancel_remains_core_cancellation(self):
        import subtitle_tool_core as core
        event = threading.Event()
        event.set()
        with self.assertRaises(core.CancelledError), patch.object(resolver, 'fetch_secondary_catalogue') as fetch:
            resolver.recover_filename_year(NAME, unresolved(), resolver.RecoverySession(cancel_event=event, cache_path=False))
        fetch.assert_not_called()

    def test_proof_cannot_be_rebound_to_different_raw_title_or_year(self):
        result, _, _, _ = self.recover()
        for name in ('2010 Something Else 1984.mkv', '2010 The Year We Make Contact 1985.mkv'):
            with self.subTest(name=name):
                self.assertFalse(resolver.validate_filename_resolution(name, unresolved(name), result))

    def test_existing_resolver_does_not_start_without_profiles(self):
        with patch.object(resolver, 'fetch_secondary_catalogue') as fetch:
            result = resolver.resolve(SimpleNamespace(title=FULL, original_title=FULL, year='1984'), (), cache_path=False)
        self.assertEqual(result.state, 'pending')
        fetch.assert_not_called()

    def test_confirmed_filename_year_keeps_original_release_year_conflict_rules(self):
        result, _, _, _ = self.recover()
        for wrong in ('2010.2014.BluRay', '2010.BluRay.2014'):
            with self.subTest(wrong=wrong):
                self.assertTrue(guard.release_year_conflict(NAME, wrong, '1984',
                                                          exact_hash=True, confirmed_work=result))
        for correct in ('2010', '2010.1984.BluRay', '2010.The.Year.We.Make.Contact.1984.BluRay'):
            with self.subTest(correct=correct):
                self.assertEqual(guard.release_year_conflict(NAME, correct, '1984',
                                                            confirmed_work=result), '')

    def test_wrong_filename_proof_cannot_supply_a_release_year(self):
        result, _, _, _ = self.recover()
        unrelated = 'Some Other Title 1984.mkv'
        self.assertEqual(guard.release_year_conflict(unrelated, 'Other.2014.BluRay', '2014',
                                                     confirmed_work=result), '')

    def test_disjoint_primary_imdb_conflict_never_publishes_or_caches_confirmation(self):
        profile = resolver.WorkProfile(FULL, '1984', '999', '', 'opensubtitles-features')
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'evidence.json'
            session = resolver.RecoverySession(cache_path=cache)
            with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [row(FULL)]}), \
                 patch.object(resolver, '_write_cache') as write:
                result = resolver.recover_filename_year(NAME, unresolved(), session, (profile,))
            self.assertEqual(result.state, 'pending')
            self.assertEqual((result.imdb_id, result.tmdb_id, result.evidence), ('', '', ()))
            self.assertIsNone(session.confirmed_resolution)
            write.assert_not_called()
            self.assertFalse(cache.exists())

    def test_disjoint_primary_tmdb_conflict_never_publishes_or_caches_confirmation(self):
        profile = resolver.WorkProfile(FULL, '1984', '', '999', 'opensubtitles-features')
        session = resolver.RecoverySession(cache_path=False)
        with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [row(FULL)]}), \
             patch.object(resolver, '_write_cache') as write:
            result = resolver.recover_filename_year(NAME, unresolved(), session, (profile,))
        self.assertEqual(result.state, 'pending')
        self.assertIsNone(session.confirmed_resolution)
        self.assertEqual(session.budget.request_count, 1)
        write.assert_not_called()

    def test_matching_primary_single_id_can_confirm_and_cache_after_validation(self):
        for imdb, tmdb in (('86837', ''), ('', '4437')):
            profile = resolver.WorkProfile(FULL, '1984', imdb, tmdb, 'opensubtitles-features')
            with self.subTest(imdb=imdb, tmdb=tmdb), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / 'evidence.json'
                session = resolver.RecoverySession(cache_path=cache)
                with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [row(FULL)]}):
                    result = resolver.recover_filename_year(NAME, unresolved(), session, (profile,))
                self.assertEqual(result.state, 'confirmed')
                self.assertIs(session.confirmed_resolution, result)
                self.assertTrue(resolver.validate_filename_resolution(NAME, unresolved(), result, (profile,)))
                stored = json.loads(cache.read_text(encoding='utf-8'))
                self.assertEqual(next(iter(stored['entries'].values()))['resolution']['state'], 'confirmed')

    def test_absent_primary_ids_are_insufficient_evidence_rather_than_conflicting_ids(self):
        profile = resolver.WorkProfile(FULL, '1984', '', '', 'opensubtitles-features')
        session = resolver.RecoverySession(cache_path=False)
        with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [row(FULL)]}):
            result = resolver.recover_filename_year(NAME, unresolved(), session, (profile,))
        self.assertEqual(result.state, 'confirmed')
        self.assertTrue(resolver.validate_filename_resolution(NAME, unresolved(), result, (profile,)))
        self.assertIs(session.confirmed_resolution, result)


class PublicAliasTableTests(unittest.TestCase):
    def test_official_alias_table_is_bound_to_id_title_and_year(self):
        public = resolver.parse_public_movie_titles(titles_page(), '4437')
        self.assertEqual((public['title'], public['year'], public['tmdb_id']), ('2010', '1984', '4437'))
        self.assertEqual(public['aliases'], ('2010: The Year We Make Contact',))

    def test_cross_movie_canonical_or_og_is_rejected(self):
        for kwargs in ({'canonical_id': '999'}, {'og_id': '999'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                resolver.parse_public_movie_titles(titles_page(**kwargs), '4437')

    def test_recommendations_and_script_table_are_not_official_aliases(self):
        official = '<table class="card releases titles"><tbody><tr><td>' + FULL + '</td></tr></tbody></table>'
        for extra in ('<script>const value = ' + repr(official) + '</script>', '<!--' + official + '-->'):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                resolver.parse_public_movie_titles(titles_page(table_class='recommendations', extra=extra), '4437')

    def test_wrong_public_year_or_main_title_cannot_confirm(self):
        for kwargs in ({'year': '1985'}, {'title': 'Something Else'}):
            with self.subTest(kwargs=kwargs):
                session = resolver.RecoverySession(cache_path=False)
                with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [row()]}), \
                     patch.object(resolver, 'fetch_public_movie', return_value=titles_page(**kwargs)):
                    result = resolver.recover_filename_year(NAME, unresolved(), session)
                self.assertEqual(result.state, 'pending')


if __name__ == '__main__':
    unittest.main()
