"""Read-only JSON API over collected property listings.

Serves whatever the collector has written to CSV. No database, no web
framework — stdlib only, so ``property-api`` runs anywhere the scraper runs.
Data is loaded into memory at startup; restart the server to pick up a
newly completed run (or send SIGHUP for an in-place reload).

Endpoints:
    GET /health          -> {"status": "ok", "records": N}
    GET /metadata        -> source, license, scope, and completeness evidence
    GET /listings        -> filtered, paginated listing records
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from field_utils import normalize_record_key

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# Filters map a query-string key to (record field, value converter, inclusive-min,
# inclusive-max flag). Keys are descriptive API names, not CSV column names, so
# the surface stays stable even if columns are renamed.
FILTER_SPECS = {
    "listing_id": ("listing_id", str, False, False),
    "address": ("address", str, False, False),
    "source": ("source_page", str, False, False),
    "price_min": ("price", float, True, False),
    "price_max": ("price", float, False, True),
    "beds_min": ("beds", float, True, False),
    "beds_max": ("beds", float, False, True),
    "sqft_min": ("sqft", float, True, False),
    "sqft_max": ("sqft", float, False, True),
}


class ListingsIndex:
    """In-memory index over the listings CSV, rebuilt on reload."""

    def __init__(self, csv_path: Path, metadata_path: Path | None = None) -> None:
        self.csv_path = csv_path
        self.metadata_path = metadata_path
        self.records: list[dict[str, str]] = []
        self.seen_keys: set[str] = set()
        self.generated_at = ""
        self.metadata: dict[str, object] = {}
        self.lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self.lock:
            self.records, self.seen_keys, self.generated_at = self._read_file()
            self.metadata = self._read_metadata()

    def _read_metadata(self) -> dict[str, object]:
        if self.metadata_path and self.metadata_path.exists():
            try:
                payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except (OSError, json.JSONDecodeError):
                logging.warning("Ignoring unreadable metadata file: %s", self.metadata_path)
        return {
            "schema_version": 1,
            "source_type": "unknown_csv",
            "coverage_scope": "unknown",
            "retrieval": {
                "records": len(self.records),
                "complete_for_configured_scope": False,
                "published": self.csv_path.exists(),
                "stop_reason": "no_metadata_file",
            },
            "warning": "No provenance metadata was supplied; completeness is unverified.",
        }

    def _read_file(self) -> tuple[list[dict[str, str]], set[str], str]:
        records: list[dict[str, str]] = []
        keys: set[str] = set()
        if not self.csv_path.exists():
            return records, keys, datetime.now(UTC).isoformat()
        with self.csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                return records, keys, datetime.now(UTC).isoformat()
            for row in reader:
                key = normalize_record_key(row.get("listing_id", ""), row.get("url", ""))
                if key and key not in keys:
                    keys.add(key)
                    records.append(row)
        generated_at = datetime.now(UTC).isoformat()
        return records, keys, generated_at

    def query(
        self,
        filters: dict[str, str],
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, object]:
        with self.lock:
            records = self.records

        def matches(record: dict[str, str]) -> bool:
            for key, raw_value in filters.items():
                spec = FILTER_SPECS.get(key)
                if spec is None:
                    return False
                field, converter, is_min, is_max = spec
                actual = record.get(field, "")
                if converter is str:
                    if raw_value.lower() not in actual.lower():
                        return False
                    continue
                if actual == "":
                    return False
                try:
                    actual_num = converter(actual)
                    wanted = converter(raw_value)
                except ValueError:
                    return False
                if is_min and actual_num < wanted:
                    return False
                if is_max and actual_num > wanted:
                    return False
            return True

        filtered = [r for r in records if matches(r)]
        page = filtered[offset : offset + limit]
        return {
            "count": len(page),
            "total": len(filtered),
            "offset": offset,
            "limit": limit,
            "generated_at": self.generated_at,
            "results": page,
        }


class ApiHandler(BaseHTTPRequestHandler):
    index: ListingsIndex = None  # type: ignore[assignment]  # set by make_server

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            with self.index.lock:
                total = len(self.index.records)
            self._send_json(200, {"status": "ok", "records": total})
            return
        if parsed.path == "/metadata":
            with self.index.lock:
                metadata = self.index.metadata
            self._send_json(200, metadata)
            return
        if parsed.path == "/listings":
            query = parse_qs(parsed.query)
            filters = {key: values[-1] for key, values in query.items() if key in FILTER_SPECS}
            if "limit" in query:
                try:
                    limit = int(query["limit"][-1])
                except ValueError:
                    self._send_json(400, {"error": "limit must be an integer"})
                    return
                limit = max(1, min(limit, MAX_LIMIT))
            else:
                limit = DEFAULT_LIMIT
            if "offset" in query:
                try:
                    offset = int(query["offset"][-1])
                except ValueError:
                    self._send_json(400, {"error": "offset must be an integer"})
                    return
                offset = max(0, offset)
            else:
                offset = 0
            self._send_json(200, self.index.query(filters, limit, offset))
            return
        self._send_json(404, {"error": "not found"})


def make_server(
    csv_path: Path,
    host: str,
    port: int,
    metadata_path: Path | None = None,
) -> ThreadingHTTPServer:
    index = ListingsIndex(csv_path, metadata_path)
    handler = type(
        "BoundApiHandler",
        (ApiHandler,),
        {"index": index},
    )
    server = ThreadingHTTPServer((host, port), handler)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve collected property listings as a JSON API.")
    parser.add_argument(
        "--csv",
        type=Path,
        help="Listings CSV (default: data/listings.csv relative to --config's directory).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Scraper config.json; its output_csv path is used when --csv is absent.",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        help="Optional provenance/completeness JSON exposed at GET /metadata.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    configured_metadata: Path | None = None
    if args.csv:
        csv_path = args.csv.resolve()
    elif args.config:
        import json as _json

        raw = _json.loads(args.config.read_text(encoding="utf-8"))
        base = args.config.parent
        csv_path = (base / raw["output_csv"]).resolve()
        if raw.get("metadata_file"):
            configured_metadata = (base / raw["metadata_file"]).resolve()
    else:
        parser.error("provide --csv or --config")

    metadata_path = args.metadata.resolve() if args.metadata else configured_metadata
    server = make_server(csv_path, args.host, args.port, metadata_path)

    def reload_index(signum: int, _frame: object) -> None:
        logging.info("SIGHUP received; reloading %s", csv_path)
        server.RequestHandlerClass.index.reload()

    signal.signal(signal.SIGHUP, reload_index)
    logging.info(
        "Serving %s on http://%s:%d (GET /health, GET /metadata, GET /listings)",
        csv_path,
        args.host,
        args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
