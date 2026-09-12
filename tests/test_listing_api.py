from __future__ import annotations

import csv
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from listing_api import ListingsIndex, make_server


def write_sample_csv(path: Path) -> None:
    rows = [
        ["listing_id", "address", "price", "beds", "url", "source_page", "scraped_at"],
        ["1001", "1 Main St", "850000", "3", "https://src/1001_zpid/", "https://src/", "2026-07-30T00:00:00Z"],
        ["1002", "2 Main St", "450000", "2", "https://src/1002_zpid/", "https://src/", "2026-07-30T00:00:01Z"],
        ["1003", "3 Oak Ave", "1250000", "4", "https://src/1003_zpid/", "https://src/", "2026-07-30T00:00:02Z"],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)


def test_index_query_filters_and_paginates(tmp_path: Path) -> None:
    csv_path = tmp_path / "listings.csv"
    write_sample_csv(csv_path)
    index = ListingsIndex(csv_path)

    result = index.query({}, limit=2, offset=0)
    assert result["total"] == 3
    assert result["count"] == 2

    result = index.query({"price_min": "600000", "price_max": "900000"})
    assert result["total"] == 1
    assert result["results"][0]["listing_id"] == "1001"

    result = index.query({"beds_min": "3"})
    assert result["total"] == 2

    result = index.query({"address": "oak"})
    assert result["total"] == 1
    assert result["results"][0]["listing_id"] == "1003"


def test_index_dedups_repeated_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "listings.csv"
    write_sample_csv(csv_path)
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(
            ["1001", "1 Main St", "850000", "3", "https://src/1001_zpid/", "https://src/", "2026-07-30T00:01:00Z"]
        )
    index = ListingsIndex(csv_path)
    assert index.query({})["total"] == 3


def test_http_server_endpoints(tmp_path: Path) -> None:
    csv_path = tmp_path / "listings.csv"
    metadata_path = tmp_path / "metadata.json"
    write_sample_csv(csv_path)
    metadata_path.write_text(
        json.dumps(
            {
                "source_type": "reso_web_api",
                "coverage_scope": "Active Toronto fixture listings",
                "retrieval": {"complete_for_configured_scope": True},
            }
        ),
        encoding="utf-8",
    )
    server = make_server(csv_path, "127.0.0.1", 0, metadata_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/health") as response:
            assert json.loads(response.read()) == {"status": "ok", "records": 3}

        with urllib.request.urlopen(f"{base}/metadata") as response:
            metadata = json.loads(response.read())
            assert metadata["source_type"] == "reso_web_api"
            assert metadata["retrieval"]["complete_for_configured_scope"] is True

        with urllib.request.urlopen(
            f"{base}/listings?price_min=600000&price_max=900000"
        ) as response:
            payload = json.loads(response.read())
            assert payload["total"] == 1
            assert payload["results"][0]["listing_id"] == "1001"

        try:
            urllib.request.urlopen(f"{base}/listings?limit=abc")
        except urllib.error.HTTPError as error:
            assert error.code == 400
        else:
            raise AssertionError("expected 400 for invalid limit")

        try:
            urllib.request.urlopen(f"{base}/nope")
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("expected 404 for unknown path")
    finally:
        server.shutdown()
        server.server_close()
