"""OpenSubtitles credential checks must use an authenticated route and fail closed."""
from __future__ import annotations

import io
import json
import urllib.error
import unittest
from unittest.mock import patch

import online_subtitles as online


def search_response(data=None):
    data = [] if data is None else data
    return {"data": data, "page": 1, "per_page": 50,
            "total_pages": 1 if data else 0, "total_count": len(data)}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class OpenSubtitlesKeyValidationTests(unittest.TestCase):
    def test_blank_key_does_not_send_a_request(self):
        with patch.object(online, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "请先填写"):
                online.validate_api_key("  \n ")
        request.assert_not_called()

    def test_empty_search_result_proves_access_without_downloading_or_saving(self):
        with patch.object(online, "_request", return_value=search_response()) as request, \
             patch.object(online, "save_settings") as save:
            self.assertIsNone(online.validate_api_key("  fixture-key  "))
        request.assert_called_once_with("fixture-key", "/subtitles", params={"imdb_id": 1, "languages": "en"})
        save.assert_not_called()

    def test_nonempty_result_is_also_valid(self):
        with patch.object(online, "_request", return_value=search_response([{"id": "1"}])):
            self.assertIsNone(online.validate_api_key("fixture-key"))

    def test_request_uses_key_header_and_one_small_get_without_login(self):
        response = Response(json.dumps(search_response()).encode("utf-8"))
        with patch.object(online._IPV4_OPENER, "open", return_value=response) as open_request:
            online.validate_api_key(" fixture-key ")
        open_request.assert_called_once()
        request = open_request.call_args.args[0]
        self.assertEqual(request.full_url, f"{online.API_BASE}/subtitles?imdb_id=1&languages=en")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Api-key"), "fixture-key")
        self.assertEqual(request.get_header("User-agent"), online.USER_AGENT)
        self.assertIsNone(request.get_header("Authorization"))

    def test_http_authentication_and_quota_fail_without_retry_or_save(self):
        for status, error_type in ((401, online.SubtitleAuthenticationError),
                                   (403, online.SubtitleAuthenticationError),
                                   (429, online.SubtitleQuotaError)):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://api.opensubtitles.com", status,
                                               "rejected", {}, io.BytesIO(b"{}"))
                with patch.object(online._IPV4_OPENER, "open", side_effect=error) as request, \
                     patch.object(online, "save_settings") as save:
                    with self.assertRaises(error_type):
                        online.validate_api_key("fixture-key")
                request.assert_called_once()
                save.assert_not_called()

    def test_network_failure_does_not_pass_validation(self):
        with patch.object(online._IPV4_OPENER, "open", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(RuntimeError, "无法连接 OpenSubtitles"):
                online.validate_api_key("fixture-key")

    def test_timeout_does_not_pass_validation(self):
        with patch.object(online._IPV4_OPENER, "open", side_effect=TimeoutError("slow")):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                online.validate_api_key("fixture-key")

    def test_html_or_invalid_json_does_not_pass_validation(self):
        with patch.object(online._IPV4_OPENER, "open", return_value=Response(b"<html>blocked</html>")):
            with self.assertRaisesRegex(RuntimeError, "数据格式异常"):
                online.validate_api_key("fixture-key")

    def test_http_200_error_envelope_does_not_pass_validation(self):
        for payload, error_type in (({"status": 401}, online.SubtitleAuthenticationError),
                                    ({"status": "403"}, online.SubtitleAuthenticationError),
                                    ({"status": 429}, online.SubtitleQuotaError),
                                    ({"status": False}, RuntimeError),
                                    ({"success": False}, RuntimeError),
                                    ({"error": "bad credential"}, RuntimeError),
                                    ({"errors": ["bad credential"]}, RuntimeError)):
            with self.subTest(payload=payload), patch.object(online, "_request", return_value=payload):
                with self.assertRaises(error_type):
                    online.validate_api_key("fixture-key")

    def test_malformed_or_public_metadata_response_does_not_pass_validation(self):
        invalid = [[], {"data": []}, {"data": [{"language_code": "en"}]},
                   {**search_response(), "data": {}},
                   {**search_response(), "total_count": True},
                   {**search_response(), "total_pages": -1},
                   {**search_response(), "per_page": 0},
                   {**search_response(), "page": 2}]
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(online, "_request", return_value=payload):
                with self.assertRaisesRegex(RuntimeError, "数据格式异常"):
                    online.validate_api_key("fixture-key")

    def test_error_messages_do_not_disclose_the_supplied_key(self):
        for error_type in (online.SubtitleAuthenticationError, online.SubtitleQuotaError, RuntimeError):
            with self.subTest(error_type=error_type), \
                 patch.object(online, "_request", side_effect=error_type("rejected fixture-key")):
                with self.assertRaises(error_type) as raised:
                    online.validate_api_key("fixture-key")
                self.assertNotIn("fixture-key", str(raised.exception))
                self.assertIn("[已隐藏]", str(raised.exception))

    def test_other_http_failures_stay_failures_and_hide_server_echoed_key(self):
        detail = json.dumps({"message": "failure for fixture-key"}).encode("utf-8")
        error = urllib.error.HTTPError("https://api.opensubtitles.com", 503, "unavailable", {}, io.BytesIO(detail))
        with patch.object(online._IPV4_OPENER, "open", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "503") as raised:
                online.validate_api_key("fixture-key")
        self.assertNotIn("fixture-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
