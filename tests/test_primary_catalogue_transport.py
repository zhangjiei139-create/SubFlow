"""Canonical primary URL and terminal errors; no real HTTP is sent."""
import unittest
import urllib.error
from unittest.mock import patch

import online_subtitles as online
import work_identity_resolver as resolver


class PrimaryCatalogueTransportTests(unittest.TestCase):
    def request(self, title='1984', year='1984', *, response='{"data": []}', error=None):
        budget = resolver.RequestBudget(2, 15.0)
        with patch.object(resolver, '_read_response', return_value=response,
                          side_effect=error) as read:
            result = budget.request('primary', lambda: resolver.fetch_primary_catalogue(
                title, year, budget, api_key='transport-fixture-key'))
        return result, budget, read

    def test_numeric_query_uses_official_sorted_url_once(self):
        response, budget, read = self.request()
        self.assertEqual(response, {'data': []})
        read.assert_called_once()
        request, actual_budget = read.call_args.args
        self.assertIs(actual_budget, budget)
        self.assertEqual(request.full_url,
            'https://api.opensubtitles.com/api/v1/features?query=1984&query_match=exact&type=movie&year=1984')
        self.assertEqual(request.get_header('Api-key'), 'transport-fixture-key')
        self.assertEqual(budget.request_count, 1)
        self.assertEqual((budget.max_requests, budget.time_budget_seconds), (2, 15.0))

    def test_normalization_matches_existing_primary_request_rules(self):
        _response, budget, read = self.request('A MIXED Title', '')
        self.assertEqual(read.call_args.args[0].full_url,
            'https://api.opensubtitles.com/api/v1/features?query=a+mixed+title&query_match=exact&type=movie')
        self.assertEqual(budget.request_count, 1)

    def test_unauthorized_errors_keep_existing_authentication_class_without_retry(self):
        for status in (401, 403):
            budget = resolver.RequestBudget(2, 15.0)
            failure = urllib.error.HTTPError('https://api.opensubtitles.com/api/v1/features', status,
                                              'authentication rejected', None, None)
            with self.subTest(status=status), \
                 patch.object(resolver, '_read_response', side_effect=failure) as read, \
                 self.assertRaises(online.SubtitleAuthenticationError) as caught:
                budget.request('primary', lambda: resolver.fetch_primary_catalogue(
                    '1984', '1984', budget, api_key='transport-fixture-key'))
            read.assert_called_once()
            self.assertEqual(budget.request_count, 1)
            self.assertNotIn('transport-fixture-key', str(caught.exception))

    def test_quota_errors_keep_existing_quota_class_without_retry(self):
        budget = resolver.RequestBudget(2, 15.0)
        failure = urllib.error.HTTPError('https://api.opensubtitles.com/api/v1/features', 429,
                                          'quota exhausted', None, None)
        with patch.object(resolver, '_read_response', side_effect=failure) as read, \
             self.assertRaises(online.SubtitleQuotaError) as caught:
            budget.request('primary', lambda: resolver.fetch_primary_catalogue(
                '1984', '1984', budget, api_key='transport-fixture-key'))
        read.assert_called_once()
        self.assertEqual(budget.request_count, 1)
        self.assertNotIn('transport-fixture-key', str(caught.exception))

    def test_redirect_is_not_followed_or_retried(self):
        budget = resolver.RequestBudget(2, 15.0)
        failure = urllib.error.HTTPError('https://api.opensubtitles.com/api/v1/features', 301,
            'redirect', {'Location': 'https://different.invalid/features'}, None)
        with patch.object(resolver, '_read_response', side_effect=failure) as read, \
             self.assertRaises(urllib.error.HTTPError) as caught:
            budget.request('primary', lambda: resolver.fetch_primary_catalogue(
                '1984', '1984', budget, api_key='transport-fixture-key'))
        self.assertIs(caught.exception, failure)
        read.assert_called_once()
        self.assertEqual(budget.request_count, 1)
        handler = resolver._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 301, 'redirect', {},
                                                  'https://different.invalid/features'))


if __name__ == '__main__':
    unittest.main()
