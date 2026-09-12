from __future__ import annotations

import csv
import json
import urllib.parse
from pathlib import Path

import pytest

from reso_ingest import ResoIngester, Settings


def make_config(tmp_path: Path, **overrides: object) -> Path:
    raw: dict[str, object] = {
        "service_root": "http://127.0.0.1:9999/reso/odata",
        "resource": "Property",
        "source_name": "Fixture RESO service",
        "coverage_scope": "Active Toronto fixture listings",
        "license": {
            "name": "Fixture data license",
            "url": "https://example.test/license",
        },
        "output_csv": "listings.csv",
        "checkpoint_file": "checkpoint.json",
        "metadata_file": "metadata.json",
        "filter": "City eq 'Toronto' and StandardStatus eq 'Active'",
        "page_size": 2,
        "max_pages": 0,
        "auth": {"mode": "none"},
    }
    raw.update(overrides)
    path = tmp_path / "reso.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_initial_url_uses_odata_query_parameters(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    ingester = ResoIngester(settings)
    url = urllib.parse.urlparse(ingester._initial_url(skip=10))
    query = urllib.parse.parse_qs(url.query)

    assert url.path.endswith("/reso/odata/Property")
    assert query["$top"] == ["2"]
    assert query["$skip"] == ["10"]
    assert query["$filter"] == ["City eq 'Toronto' and StandardStatus eq 'Active'"]
    assert "ListingKey" in query["$select"][0]


def test_map_record_uses_reso_fields_and_address_fallback(tmp_path: Path) -> None:
    settings = Settings.load(make_config(tmp_path))
    ingester = ResoIngester(settings)
    mapped = ingester._map_record(
        {
            "ListingKey": "abc-123",
            "StreetNumber": "10",
            "StreetName": "King",
            "StreetSuffix": "St",
            "UnitNumber": "404",
            "City": "Toronto",
            "StateOrProvince": "ON",
            "PostalCode": "M5A 1A1",
            "ListPrice": 850000.0,
            "BedroomsTotal": 2,
            "BathroomsTotalInteger": 2,
            "LivingArea": 900,
            "ListAgentFullName": "Example Agent",
            "ListingURL": "https://listings.example/abc-123",
        }
    )

    assert mapped["listing_id"] == "abc-123"
    assert mapped["address"] == "10 King St #404, Toronto, ON, M5A 1A1"
    assert mapped["price"] == "850000"
    assert mapped["beds"] == "2"
    assert mapped["baths"] == "2"
    assert mapped["sqft"] == "900"
    assert mapped["agent"] == "Example Agent"


def test_complete_run_follows_next_link_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    settings = Settings.load(make_config(tmp_path))
    ingester = ResoIngester(settings)
    calls: list[str] = []
    pages = [
        {
            "value": [
                {
                    "ListingKey": "one",
                    "UnparsedAddress": "1 Main St, Toronto, ON",
                    "ListPrice": 700000,
                },
                {
                    "ListingKey": "two",
                    "UnparsedAddress": "2 Main St, Toronto, ON",
                    "ListPrice": 800000,
                },
            ],
            "@odata.nextLink": "http://127.0.0.1:9999/reso/odata/Property?page=2",
        },
        {
            "value": [
                {
                    "ListingKey": "three",
                    "UnparsedAddress": "3 Main St, Toronto, ON",
                    "ListPrice": 900000,
                }
            ]
        },
    ]

    def fake_request(url: str) -> dict[str, object]:
        calls.append(url)
        return pages[len(calls) - 1]

    ingester._authorized_json = fake_request  # type: ignore[method-assign]
    result = ingester.run()

    assert result == {
        "complete": True,
        "published": True,
        "pages": 2,
        "records": 3,
        "stop_reason": "provider_exhausted",
    }
    assert calls[1].endswith("?page=2")
    with settings.output_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["listing_id"] for row in rows] == ["one", "two", "three"]
    assert not ingester.staging_csv.exists()

    metadata = json.loads(settings.metadata_file.read_text(encoding="utf-8"))
    assert metadata["retrieval"]["complete_for_configured_scope"] is True
    assert metadata["retrieval"]["published"] is True
    assert metadata["retrieval"]["records"] == 3
    assert metadata["coverage_scope"] == "Active Toronto fixture listings"


def test_incomplete_run_does_not_replace_last_published_snapshot(
    tmp_path: Path,
) -> None:
    settings = Settings.load(make_config(tmp_path, page_size=1, max_pages=1))
    settings.output_csv.write_text("last-known-good\n", encoding="utf-8")
    ingester = ResoIngester(settings)
    ingester._authorized_json = lambda _url: {  # type: ignore[method-assign]
        "value": [{"ListingKey": "one", "UnparsedAddress": "1 Main St"}],
        "@odata.nextLink": "http://127.0.0.1:9999/reso/odata/Property?page=2",
    }

    result = ingester.run()

    assert result["complete"] is False
    assert result["published"] is False
    assert result["stop_reason"] == "max_pages_reached"
    assert settings.output_csv.read_text(encoding="utf-8") == "last-known-good\n"
    assert ingester.staging_csv.exists()
    metadata = json.loads(settings.metadata_file.read_text(encoding="utf-8"))
    assert metadata["retrieval"]["published"] is False


def test_settings_require_scope_and_license(tmp_path: Path) -> None:
    settings = Settings.load(
        make_config(tmp_path, coverage_scope="", license={"name": "", "url": ""})
    )
    with pytest.raises(ValueError, match="coverage_scope"):
        settings.validate()
