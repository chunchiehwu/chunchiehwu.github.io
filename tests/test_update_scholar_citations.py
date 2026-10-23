"""Regression checks for citation cache creation and failed refreshes.

Run from the repository root: python -m unittest discover -s tests -v
"""

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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
        patch("builtins.print").start()
        self.scholar = patch.object(updater, "scholarly").start()
        self.scholar.fill.return_value = {"publications": [self.publication]}

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


if __name__ == "__main__":
    unittest.main()
