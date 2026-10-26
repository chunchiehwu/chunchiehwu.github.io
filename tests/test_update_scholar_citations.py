"""Regression checks for citation cache creation and failed refreshes.

Run from the repository root: python -m unittest discover -s tests -v
"""

import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "bin/update_scholar_citations.py"
spec = importlib.util.spec_from_file_location("update_scholar_citations", SCRIPT)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class CitationCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name) / "citations.yml"
        self.old_data = {
            "metadata": {"last_updated": "2000-01-01"},
            "papers": {
                "author:paper": {"title": "Example paper", "year": "2024", "citations": 3}
            },
        }
        self.publication = {
            "author_pub_id": "author:paper",
            "bib": {"title": "Example paper", "pub_year": "2024"},
            "num_citations": 4,
        }
        self.addCleanup(patch.stopall)
        patch.object(updater, "OUTPUT_FILE", str(self.cache)).start()
        patch.object(updater, "SCHOLAR_USER_ID", "author").start()
        patch.dict(updater.os.environ, {"SERPAPI_API_KEY": ""}).start()
        self.print = patch("builtins.print").start()
        self.scholar = patch.object(updater, "scholarly").start()
        self.scholar.fill.return_value = {"publications": [self.publication]}
        self.urlopen = patch.object(updater, "urlopen").start()

    def write_old_cache(self):
        self.cache.write_text(yaml.safe_dump(self.old_data))
        return self.cache.read_bytes()

    def test_first_refresh_creates_missing_cache(self):
        updater.get_scholar_citations()
        self.assertEqual(yaml.safe_load(self.cache.read_text())["papers"]["author:paper"]["citations"], 4)

    def test_successful_refresh_updates_citations(self):
        self.write_old_cache()
        updater.get_scholar_citations()
        data = yaml.safe_load(self.cache.read_text())
        self.assertEqual(data["papers"]["author:paper"]["citations"], 4)
        self.assertEqual(data["metadata"]["last_updated"], datetime.now().strftime("%Y-%m-%d"))

    def test_unchanged_counts_preserve_cache(self):
        original = self.write_old_cache()
        self.publication["num_citations"] = 3
        updater.get_scholar_citations()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_blocked_request_fails_and_preserves_cache(self):
        original = self.write_old_cache()
        self.scholar.search_author_id.side_effect = RuntimeError("Scholar blocked the request")
        with self.assertRaises(SystemExit) as error:
            updater.get_scholar_citations()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.cache.read_bytes(), original)

    def test_empty_profile_fails_and_preserves_cache(self):
        original = self.write_old_cache()
        self.scholar.fill.return_value = {"publications": []}
        with self.assertRaises(SystemExit):
            updater.get_scholar_citations()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_missing_publication_ids_preserve_cache(self):
        original = self.write_old_cache()
        self.scholar.fill.return_value = {"publications": [{"bib": {"title": "No ID"}}]}
        with self.assertRaises(SystemExit):
            updater.get_scholar_citations()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_non_mapping_cache_can_be_recreated(self):
        self.cache.write_text("- invalid cache\n")
        updater.get_scholar_citations()
        self.assertEqual(yaml.safe_load(self.cache.read_text())["papers"]["author:paper"]["citations"], 4)

    def test_same_day_refresh_skips_network(self):
        self.old_data["metadata"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        original = self.write_old_cache()
        updater.get_scholar_citations()
        self.scholar.search_author_id.assert_not_called()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_forced_same_day_refresh_fetches_publications(self):
        self.old_data["metadata"]["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        self.write_old_cache()
        updater.get_scholar_citations(force=True)
        self.scholar.search_author_id.assert_called_once_with(updater.SCHOLAR_USER_ID)
        self.scholar.fill.assert_called_once_with(
            self.scholar.search_author_id.return_value, sections=["publications"]
        )
        self.assertEqual(yaml.safe_load(self.cache.read_text())["papers"]["author:paper"]["citations"], 4)

    def api_page(self, paper_id="author:paper", next_page=False):
        data = {
            "search_metadata": {"status": "Success"},
            "articles": [{
                "citation_id": paper_id,
                "title": "Example paper",
                "year": "2024",
                "cited_by": {"value": 4},
            }],
        }
        if next_page:
            data["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?cstart=100"}
        return data

    def api_responses(self, *pages):
        self.urlopen.side_effect = [io.BytesIO(json.dumps(page).encode()) for page in pages]

    def test_api_key_automatically_selects_serpapi_and_batches_results(self):
        self.write_old_cache()
        self.api_responses(self.api_page())
        with patch.dict(updater.os.environ, {"SERPAPI_API_KEY": "test-secret"}):
            updater.get_scholar_citations()
        self.scholar.search_author_id.assert_not_called()
        self.assertEqual(self.urlopen.call_count, 1)
        url = self.urlopen.call_args.args[0]
        self.assertEqual(urlsplit(url).netloc, "serpapi.com")
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["engine"], ["google_scholar_author"])
        self.assertEqual(params["author_id"], ["author"])
        self.assertEqual(params["num"], ["100"])
        self.assertEqual(params["api_key"], ["test-secret"])
        self.assertEqual(yaml.safe_load(self.cache.read_text())["papers"]["author:paper"]["citations"], 4)
        self.assertNotIn("test-secret", str(self.print.call_args_list))

    def test_serpapi_required_key_does_not_fall_back_to_direct_fetching(self):
        original = self.write_old_cache()
        with self.assertRaises(SystemExit):
            updater.get_scholar_citations(provider="serpapi")
        self.urlopen.assert_not_called()
        self.scholar.search_author_id.assert_not_called()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_explicit_direct_fetch_does_not_use_available_api_key(self):
        with patch.dict(updater.os.environ, {"SERPAPI_API_KEY": "test-secret"}):
            updater.get_scholar_citations(provider="direct")
        self.urlopen.assert_not_called()
        self.scholar.search_author_id.assert_called_once_with("author")

    def test_serpapi_fetches_all_pages_from_the_fixed_api_endpoint(self):
        first_page = self.api_page(next_page=True)
        # Only use the presence of pagination; never send the key to a supplied URL.
        first_page["serpapi_pagination"]["next"] = "https://example.invalid/next"
        self.api_responses(first_page, self.api_page(paper_id="author:second"))
        data = updater.fetch_serpapi_publications("test-secret")
        self.assertEqual(len(data["publications"]), 2)
        urls = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertTrue(all(urlsplit(url).netloc == "serpapi.com" for url in urls))
        self.assertEqual([parse_qs(urlsplit(url).query)["start"] for url in urls], [["0"], ["100"]])

    def test_serpapi_errors_preserve_cache_without_logging_the_key(self):
        original = self.write_old_cache()
        secret_url = "https://serpapi.com/search.json?api_key=test-secret"
        failures = [
            HTTPError(secret_url, 401, secret_url, {}, None),
            HTTPError(secret_url, 429, secret_url, {}, None),
            URLError(secret_url),
            TimeoutError(secret_url),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.print.reset_mock()
                self.urlopen.side_effect = failure
                with patch.dict(updater.os.environ, {"SERPAPI_API_KEY": "test-secret"}):
                    with self.assertRaises(SystemExit):
                        updater.get_scholar_citations()
                self.assertEqual(self.cache.read_bytes(), original)
                self.assertNotIn("test-secret", str(self.print.call_args_list))
        self.scholar.search_author_id.assert_not_called()

    def test_serpapi_invalid_responses_preserve_cache(self):
        original = self.write_old_cache()
        invalid_article = self.api_page(paper_id="another-author:paper")
        repeated = self.api_page()
        repeated["articles"] *= 2
        invalid_count = self.api_page()
        invalid_count["articles"][0]["cited_by"]["value"] = -1
        failures = [
            {"error": "test-secret"},
            {"search_metadata": {"status": "Processing"}},
            {"search_metadata": {"status": "Success"}, "articles": []},
            invalid_article,
            repeated,
            invalid_count,
        ]
        for page in failures:
            self.api_responses(page)
            self.print.reset_mock()
            with patch.dict(updater.os.environ, {"SERPAPI_API_KEY": "test-secret"}):
                with self.assertRaises(SystemExit):
                    updater.get_scholar_citations()
            self.assertEqual(self.cache.read_bytes(), original)
            self.assertNotIn("test-secret", str(self.print.call_args_list))

    def test_serpapi_failed_later_page_does_not_save_partial_results(self):
        original = self.write_old_cache()
        self.api_responses(self.api_page(next_page=True), {"error": "Quota exhausted"})
        with patch.dict(updater.os.environ, {"SERPAPI_API_KEY": "test-secret"}):
            with self.assertRaises(SystemExit):
                updater.get_scholar_citations()
        self.assertEqual(self.cache.read_bytes(), original)

    def test_serpapi_pagination_is_bounded(self):
        self.api_responses(*[
            self.api_page(paper_id=f"author:paper{index}", next_page=True)
            for index in range(10)
        ])
        with self.assertRaisesRegex(RuntimeError, "ten-page limit"):
            updater.fetch_serpapi_publications("test-secret")
        self.assertEqual(self.urlopen.call_count, 10)


if __name__ == "__main__":
    unittest.main()
