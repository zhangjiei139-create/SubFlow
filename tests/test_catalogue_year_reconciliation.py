"""Bounded catalogue evidence regressions; no real transport or media tasks."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import work_identity_resolver as work


def identity(title, year):
    return SimpleNamespace(title=title, original_title=title, year=year,
                           imdb_id='', tmdb_id='', raw_title=title + ' ' + year)


def movie_row(title='300', year='2007', imdb='416449', tmdb='1271', kind='movie'):
    return dict(name=title, original_name=title, year=year,
                imdb_id=imdb, tmdb_id=tmdb, type=kind)


def movie_page(title='300', year='2007', tmdb='1271'):
    data = {'@type': 'Movie', 'name': title, 'url': 'https://www.themoviedb.org/movie/' + tmdb,
            'releasedEvent': [{'startDate': year + '-03-09'}]}
    return ('<h2><a href="/movie/' + tmdb + '">' + title + '</a>'
            '<span class="release_date">(' + year + ')</span></h2>'
            '<script type="application/ld+json">' + json.dumps(data) + '</script>')


def numeric_page(alias='1984'):
    return ('<html><head><link rel="canonical" href="https://www.themoviedb.org/movie/9314/titles">'
            '<meta property="og:url" content="https://www.themoviedb.org/movie/9314">'
            '<meta property="og:title" content="Nineteen Eighty-Four"></head><body>'
            '<section class="header small"><div class="title"><h2><a href="/movie/9314">'
            'Nineteen Eighty-Four</a><span class="release_date">(1984)</span></h2></div></section>'
            '<div id="media_v4"><div class="white_column"><section class="panel">'
            '<table class="card releases titles"><tbody><tr><td>' + alias + '</td><td></td></tr>'
            '</tbody></table></section></div></div></body></html>')


class CatalogueYearReconciliationTests(unittest.TestCase):
    def run_year(self, *, response=None, profiles=None, cache=False, page=None, budget=None):
        source = profiles if profiles is not None else [work.WorkProfile(
            '300', '2006', '416449', '1271', 'opensubtitles-features')]
        seen = []
        def secondary(query, current):
            seen.append(('secondary', query, current.request_count))
            return response if response is not None else {'results': [movie_row()]}
        def public(tmdb, current):
            seen.append(('public', tmdb, current.request_count))
            return page or movie_page()
        result = work.resolve(identity('300', '2006'), source,
            fetch_secondary=secondary, fetch_public=public, cache_path=cache,
            budget=budget, allow_exact_title_lookup=True)
        return result, seen

    def run_numeric(self, *, response=None, page=None, cache=False):
        seen = []
        def secondary(query, current):
            seen.append(('secondary', query, current.request_count))
            return response if response is not None else {'results': [movie_row(
                'Nineteen Eighty-Four', '1984', '87803', '9314')]}
        def public(tmdb, current):
            seen.append(('public', tmdb, current.request_count))
            return page or numeric_page()
        result = work.resolve(identity('1984', '1984'), (),
            fetch_secondary=secondary, fetch_public=public, cache_path=cache,
            allow_exact_title_lookup=True)
        return result, seen

    def test_year_correction_does_not_rewrite_physical_cache_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'proof.json'
            proof, seen = self.run_year(cache=path)
            self.assertEqual(proof.state, 'confirmed')
            self.assertEqual(proof.year, '2007')
            self.assertEqual([x[0] for x in seen], ['secondary', 'public'])
            record = next(iter(json.loads(path.read_text(encoding='utf-8'))['entries'].values()))
            self.assertEqual(record['input']['year'], '2006')
            self.assertEqual(record['input']['title'], '300')
            self.assertEqual(record['input']['raw_title'], '300 2006')
            cached = work.read_cached_resolution(identity('300', '2006'), cache_path=path)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.year, '2007')
            self.assertIsNone(work.read_cached_resolution(identity('300', '2007'), cache_path=path))

    def test_year_correction_never_covers_unobserved_years_or_other_sources(self):
        proof, _ = self.run_year()
        for source, year in [('subdl-v2', '2006'), ('opensubtitles-features', '2007'),
                             ('input', '2006'), ('other', '2007'), ('subdl-v2', '2008')]:
            with self.subTest(source=source, year=year):
                self.assertFalse(work.catalogue_year_matches(proof, source, year,
                    title='300', imdb_id='416449', tmdb_id='1271'))

    def test_year_reconciliation_requires_complete_secondary_envelope(self):
        for meta in [{'total_pages': 2}, {'has_more': True}, {'truncated': True},
                     {'total_count': 2}, {'total': 0}, {'count': True}]:
            with self.subTest(meta=meta):
                proof, calls = self.run_year(response={'results': [movie_row()], **meta})
                self.assertEqual(proof.state, 'pending')
                self.assertEqual([x[0] for x in calls], ['secondary'])

    def test_year_second_evidence_cannot_receive_a_third_request(self):
        budget = work.RequestBudget()
        budget.request_count = 1
        proof, calls = self.run_year(budget=budget)
        self.assertEqual(proof.state, 'pending')
        self.assertEqual(proof.request_count, 2)
        self.assertEqual([x[0] for x in calls], ['secondary'])

    def test_same_id_cross_year_tv_row_blocks_year_reconciliation(self):
        for row in [movie_row(kind='tv'), movie_row(year='2006', kind='tv'),
                    movie_row(year='2007', imdb='999')]:
            with self.subTest(row=row):
                proof, _ = self.run_year(response={'results': [movie_row(), row]})
                self.assertEqual(proof.state, 'pending')

    def test_numeric_proposal_has_exact_two_step_sequence(self):
        proof, calls = self.run_numeric()
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual(calls, [('secondary', 'nineteen eighty four', 1), ('public', '9314', 2)])
        self.assertTrue(work.validate_resolution(identity('1984', '1984'), proof))

    def test_numeric_cache_binds_original_number_and_all_catalogue_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'proof.json'
            proof, _ = self.run_numeric(cache=path)
            self.assertEqual(proof.state, 'confirmed')
            record = next(iter(json.loads(path.read_text(encoding='utf-8'))['entries'].values()))
            self.assertEqual(record['input']['title'], '1984')
            self.assertEqual(record['input']['raw_title'], '1984 1984')
            cached = work.read_cached_resolution(identity('1984', '1984'), cache_path=path)
            self.assertIsNotNone(cached)
            self.assertFalse(work.validate_resolution(identity('1985', '1984'), cached))

    def test_numeric_receipt_cannot_be_removed_duplicated_or_changed(self):
        proof, _ = self.run_numeric()
        record = next(item for item in proof.evidence if item.get('acquisition') == 'numeric-title-alias-proposal')
        rest = tuple(item for item in proof.evidence if item is not record)
        variants = [rest, (*rest, record, record)]
        for field, value in [('query', 'one thousand nine hundred eighty four'),
                             ('original_title', '1985'), ('input_year', '1983'),
                             ('catalogue_metadata', {'total_count': 2})]:
            variants.append((*rest, {**record, field: value}))
        for evidence in variants:
            with self.subTest(evidence=evidence):
                self.assertFalse(work.validate_resolution(identity('1984', '1984'), replace(proof, evidence=evidence)))

    def test_numeric_receipt_cannot_hide_same_title_partial_id_competition(self):
        proof, _ = self.run_numeric()
        record = next(item for item in proof.evidence if item.get('acquisition') == 'numeric-title-alias-proposal')
        rows = [*record['catalogue_profiles'], {'title': 'Nineteen Eighty-Four', 'year': '1984',
            'imdb_id': '999', 'tmdb_id': '', 'source': 'subdl-v2', 'feature_type': 'movie', 'aliases': ()}]
        mutated = tuple({**item, 'catalogue_profiles': rows} if item is record else item for item in proof.evidence)
        self.assertFalse(work.validate_resolution(identity('1984', '1984'), replace(proof, evidence=mutated)))

    def test_numeric_complete_response_rejects_count_flags_and_malformed_rows(self):
        for metadata in [{'has_more': True}, {'total_pages': 2}, {'total_count': 2},
                         {'count': True}, {'count': 0}, {'next': 'p2'}]:
            with self.subTest(metadata=metadata):
                result, calls = self.run_numeric(response={'results': [movie_row(
                    'Nineteen Eighty-Four', '1984', '87803', '9314')], **metadata})
                self.assertEqual(result.state, 'pending')
                self.assertEqual(len(calls), 1)
        self.assertFalse(work.secondary_response_complete({'results': ['bad']}))

    def test_numeric_same_year_unidentified_row_does_not_invent_competing_ids(self):
        response = {'results': [movie_row('Nineteen Eighty-Four', '1984', '87803', '9314'),
                                movie_row('Nineteen Eighty Four', '1984', '', '')]}
        proof, _ = self.run_numeric(response=response)
        self.assertEqual(proof.state, 'confirmed')

    def test_numeric_official_main_title_alone_never_proves_original_number(self):
        result, calls = self.run_numeric(page=numeric_page('Nineteen Eighty-Four'))
        self.assertEqual(result.state, 'pending')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
