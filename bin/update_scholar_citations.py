#!/usr/bin/env python

import argparse
import json
import logging
import os
import sys
import yaml
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from scholarly import scholarly


def load_scholar_user_id() -> str:
    """Load the Google Scholar user ID from the configuration file."""
    config_file = "_data/socials.yml"
    if not os.path.exists(config_file):
        print(
            f"Configuration file {config_file} not found. Please ensure the file exists and contains your Google Scholar user ID."
        )
        sys.exit(1)
    try:
        with open(config_file, "r") as f:
            config = yaml.safe_load(f)
        scholar_user_id = config.get("scholar_userid")
        if not scholar_user_id:
            print(
                "No 'scholar_userid' found in the configuration file. Please add 'scholar_userid' to _data/socials.yml."
            )
            sys.exit(1)
        return scholar_user_id
    except yaml.YAMLError as e:
        print(
            f"Error parsing YAML file {config_file}: {e}. Please check the file for correct YAML syntax."
        )
        sys.exit(1)


SCHOLAR_USER_ID: str = load_scholar_user_id()
OUTPUT_FILE: str = "_data/citations.yml"


def fetch_serpapi_publications(api_key: str) -> dict:
    """Read the public author profile in batches, without logging credential URLs."""
    if not api_key:
        raise ValueError("Set the SERPAPI_API_KEY repository secret to use SerpApi.")

    publications = []
    seen_ids = set()
    # Bound pagination so a malformed response cannot consume the monthly quota.
    for start in range(0, 1000, 100):
        params = urlencode({
            "engine": "google_scholar_author",
            "author_id": SCHOLAR_USER_ID,
            "hl": "en",
            "num": 100,
            "start": start,
            "api_key": api_key,
        })
        print(f"Fetching SerpApi publication batch at offset {start}.")
        try:
            with urlopen(f"https://serpapi.com/search.json?{params}", timeout=45) as response:
                data = json.load(response)
        except HTTPError as error:
            raise RuntimeError(
                f"SerpApi returned HTTP {error.code}. Check the API key and account quota."
            ) from None
        except (URLError, TimeoutError):
            raise RuntimeError("SerpApi request failed: network error or timeout.") from None
        except (ValueError, UnicodeError):
            raise RuntimeError("SerpApi returned invalid JSON.") from None

        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError("SerpApi reported an error. Check the API key and account quota.")
        if data.get("search_metadata", {}).get("status") != "Success":
            raise RuntimeError("SerpApi did not return a completed search.")
        articles = data.get("articles")
        if not isinstance(articles, list) or not articles:
            raise RuntimeError("SerpApi returned an empty or invalid publication page.")

        for article in articles:
            pub_id = article.get("citation_id", "")
            title = article.get("title")
            citations = (article.get("cited_by") or {}).get("value", 0)
            # SerpApi uses JSON null for papers without any citations.
            if citations is None:
                citations = 0
            if not isinstance(pub_id, str) or not pub_id.startswith(f"{SCHOLAR_USER_ID}:"):
                raise RuntimeError("SerpApi returned an unexpected publication identifier.")
            if pub_id in seen_ids:
                raise RuntimeError("SerpApi returned a repeated publication identifier.")
            if not isinstance(title, str) or not title.strip():
                raise RuntimeError("SerpApi returned a publication without a title.")
            if type(citations) is not int or citations < 0:
                raise RuntimeError(
                    f"SerpApi returned an invalid citation count (type: {type(citations).__name__})."
                )
            seen_ids.add(pub_id)
            publications.append({
                "author_pub_id": pub_id,
                "bib": {"title": title, "pub_year": str(article.get("year") or "Unknown Year")},
                "num_citations": citations,
            })

        if not data.get("serpapi_pagination", {}).get("next"):
            return {"publications": publications}

    raise RuntimeError("SerpApi exceeded the ten-page limit. Keeping the existing cache.")


def get_scholar_citations(force: bool = False, provider: str = "auto") -> None:
    """Fetch and update Google Scholar citation data."""
    print(f"Fetching citations for Google Scholar ID: {SCHOLAR_USER_ID}")
    today = datetime.now().strftime("%Y-%m-%d")
    existing_data = {}

    # Check if the output file was already updated today
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r") as f:
                existing_data = yaml.safe_load(f) or {}
            if not isinstance(existing_data, dict):
                raise ValueError("Citation data must be a YAML mapping.")
            if (
                existing_data
                and "metadata" in existing_data
                and "last_updated" in existing_data["metadata"]
            ):
                print(f"Last updated on: {existing_data['metadata']['last_updated']}")
                if not force and existing_data["metadata"]["last_updated"] == today:
                    print("Citations data is already up-to-date. Skipping fetch.")
                    return
        except Exception as e:
            existing_data = {}
            print(
                f"Warning: Could not read existing citation data from {OUTPUT_FILE}: {e}. The file may be missing or corrupted."
            )

    citation_data = {"metadata": {"last_updated": today}, "papers": {}}

    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if provider == "auto":
        provider = "serpapi" if api_key else "direct"
    print(f"Citation provider: {provider}")
    try:
        if provider == "serpapi":
            author_data = fetch_serpapi_publications(api_key)
        elif provider == "direct":
            scholarly.set_timeout(15)
            scholarly.set_retries(3)
            author = scholarly.search_author_id(SCHOLAR_USER_ID)
            # Only request the publication list needed by the badges.
            author_data = scholarly.fill(author, sections=["publications"])
        else:
            raise ValueError(f"Unknown citation provider: {provider}")
    except Exception as e:
        print(
            f"Error fetching Google Scholar data via {provider}: {e}. Keeping the existing citation data."
        )
        sys.exit(1)

    if not author_data:
        print(
            f"Could not fetch author data for user ID '{SCHOLAR_USER_ID}'. Please verify the Scholar user ID and try again."
        )
        sys.exit(1)

    if not author_data.get("publications"):
        print(f"No publications found in author data for user ID '{SCHOLAR_USER_ID}'.")
        sys.exit(1)

    for pub in author_data["publications"]:
        try:
            pub_id = pub.get("pub_id") or pub.get("author_pub_id")
            if not pub_id:
                print(
                    f"Warning: No ID found for publication: {pub.get('bib', {}).get('title', 'Unknown')}. This publication will be skipped."
                )
                continue

            title = pub.get("bib", {}).get("title", "Unknown Title")
            year = pub.get("bib", {}).get("pub_year", "Unknown Year")
            citations = pub.get("num_citations", 0)

            print(f"Found: {title} ({year}) - Citations: {citations}")

            citation_data["papers"][pub_id] = {
                "title": title,
                "year": year,
                "citations": citations,
            }
        except Exception as e:
            print(
                f"Error processing publication '{pub.get('bib', {}).get('title', 'Unknown')}': {e}. This publication will be skipped."
            )

    if not citation_data["papers"]:
        print("No valid publications fetched. Keeping the existing citation data.")
        sys.exit(1)

    # Compare new data with existing data. last_updated is the last data change.
    if existing_data and existing_data.get("papers") == citation_data["papers"]:
        print("No changes in citation data. Skipping file update.")
        return

    try:
        with open(OUTPUT_FILE, "w") as f:
            yaml.dump(citation_data, f, width=1000, sort_keys=True)
        print(f"Citation data saved to {OUTPUT_FILE}")
    except Exception as e:
        print(
            f"Error writing citation data to {OUTPUT_FILE}: {e}. Please check file permissions and disk space."
        )
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh cached Scholar citations.")
    parser.add_argument("--force", action="store_true", help="Fetch even if the cache was updated today.")
    parser.add_argument(
        "--provider", choices=["auto", "serpapi", "direct"], default="auto",
        help="Use SerpApi when a key is available, or choose a provider explicitly.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    scholarly.set_logger(True)
    try:
        get_scholar_citations(force=args.force, provider=args.provider)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
