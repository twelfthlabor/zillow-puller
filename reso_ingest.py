"""Ingest authorized RESO Web API listings without browser automation.

The software is open source; the listing data is governed by the provider's
license. Credentials are read from environment variables and are never written
to checkpoints, metadata, logs, or CSV output.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from field_utils import backoff_seconds, normalize_record_key


CSV_FIELDS = [
    "listing_id",
    "address",
    "price",
    "beds",
    "baths",
    "sqft",
    "agent",
    "url",
    "source_page",
    "scraped_at",
]

DEFAULT_SELECT = (
    "ListingKey",
    "ListingId",
    "UnparsedAddress",
    "StreetNumber",
    "StreetDirPrefix",
    "StreetName",
    "StreetSuffix",
    "UnitNumber",
    "City",
    "StateOrProvince",
    "PostalCode",
    "ListPrice",
    "BedroomsTotal",
    "BathroomsTotalInteger",
    "BathroomsTotalDecimal",
    "LivingArea",
    "ListAgentFullName",
    "ListOfficeName",
    "ListingURL",
    "VirtualTourURLUnbranded",
    "StandardStatus",
    "PropertyType",
    "ModificationTimestamp",
)

DEFAULT_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "listing_id": ("ListingKey", "ListingId"),
    "address": ("UnparsedAddress",),
    "price": ("ListPrice",),
    "beds": ("BedroomsTotal",),
    "baths": ("BathroomsTotalInteger", "BathroomsTotalDecimal"),
    "sqft": ("LivingArea",),
    "agent": ("ListAgentFullName", "ListOfficeName"),
    "url": ("ListingURL", "VirtualTourURLUnbranded"),
}


class ResoError(RuntimeError):
    """Base error for configuration, authentication, and transport failures."""


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    bearer_token_env: str
    token_url: str | None
    client_id_env: str
    client_secret_env: str
    scope: str | None


@dataclass(frozen=True)
class Settings:
    service_root: str
    resource: str
    output_csv: Path
    checkpoint_file: Path
    metadata_file: Path
    source_name: str
    coverage_scope: str
    license_name: str
    license_url: str
    filter_expression: str
    select: tuple[str, ...]
    field_map: dict[str, tuple[str, ...]]
    page_size: int
    max_pages: int
    timeout_seconds: float
    max_attempts_per_request: int
    backoff_base_seconds: float
    pagination_mode: str
    auth: AuthSettings

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        auth = raw.get("auth", {})
        configured_map = raw.get("field_map", {})
        field_map = dict(DEFAULT_FIELD_MAP)
        for canonical, source_fields in configured_map.items():
            if isinstance(source_fields, str):
                field_map[canonical] = (source_fields,)
            else:
                field_map[canonical] = tuple(str(item) for item in source_fields)
        return cls(
            service_root=str(raw["service_root"]).rstrip("/"),
            resource=str(raw.get("resource", "Property")).strip("/"),
            output_csv=(base / raw.get("output_csv", "data/reso-listings.csv")).resolve(),
            checkpoint_file=(
                base / raw.get("checkpoint_file", "data/reso-checkpoint.json")
            ).resolve(),
            metadata_file=(
                base / raw.get("metadata_file", "data/reso-metadata.json")
            ).resolve(),
            source_name=str(raw.get("source_name", "Authorized RESO provider")),
            coverage_scope=str(raw.get("coverage_scope", "")).strip(),
            license_name=str(raw.get("license", {}).get("name", "")).strip(),
            license_url=str(raw.get("license", {}).get("url", "")).strip(),
            filter_expression=str(raw.get("filter", "")).strip(),
            select=tuple(str(item) for item in raw.get("select", DEFAULT_SELECT)),
            field_map=field_map,
            page_size=int(raw.get("page_size", 500)),
            max_pages=int(raw.get("max_pages", 0)),
            timeout_seconds=float(raw.get("timeout_seconds", 30)),
            max_attempts_per_request=int(raw.get("max_attempts_per_request", 3)),
            backoff_base_seconds=float(raw.get("backoff_base_seconds", 2)),
            pagination_mode=str(raw.get("pagination_mode", "auto")),
            auth=AuthSettings(
                mode=str(auth.get("mode", "bearer")),
                bearer_token_env=str(auth.get("bearer_token_env", "RESO_BEARER_TOKEN")),
                token_url=(str(auth["token_url"]) if auth.get("token_url") else None),
                client_id_env=str(auth.get("client_id_env", "RESO_CLIENT_ID")),
                client_secret_env=str(
                    auth.get("client_secret_env", "RESO_CLIENT_SECRET")
                ),
                scope=(str(auth["scope"]) if auth.get("scope") else None),
            ),
        )

    def validate(self) -> None:
        errors: list[str] = []
        parsed = urllib.parse.urlparse(self.service_root)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("service_root must be an absolute http(s) URL")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            errors.append("service_root must use HTTPS except for localhost tests")
        if not self.resource:
            errors.append("resource is required")
        if not self.source_name:
            errors.append("source_name is required")
        if not self.coverage_scope:
            errors.append("coverage_scope is required so completeness is not overstated")
        if not self.license_name or not self.license_url:
            errors.append("license.name and license.url are required")
        if self.page_size < 1 or self.page_size > 10000:
            errors.append("page_size must be between 1 and 10000")
        if self.max_pages < 0:
            errors.append("max_pages cannot be negative; use 0 for unlimited")
        if self.timeout_seconds <= 0:
            errors.append("timeout_seconds must be positive")
        if self.max_attempts_per_request < 1:
            errors.append("max_attempts_per_request must be at least 1")
        if self.backoff_base_seconds < 0:
            errors.append("backoff_base_seconds cannot be negative")
        if self.pagination_mode not in {"auto", "next_link", "skip"}:
            errors.append("pagination_mode must be auto, next_link, or skip")
        if self.auth.mode not in {"bearer", "client_credentials", "none"}:
            errors.append("auth.mode must be bearer, client_credentials, or none")
        if self.auth.mode == "client_credentials" and not self.auth.token_url:
            errors.append("auth.token_url is required for client_credentials")
        missing_fields = set(CSV_FIELDS[:8]) - set(self.field_map)
        if missing_fields:
            errors.append("field_map is missing: " + ", ".join(sorted(missing_fields)))
        if errors:
            raise ValueError("; ".join(errors))


class ResoIngester:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.token: str | None = None
        self.started_at = datetime.now(UTC)

    @property
    def staging_csv(self) -> Path:
        return self.settings.output_csv.with_suffix(
            self.settings.output_csv.suffix + ".partial"
        )

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "property-scraper-reso/0.1",
            **(headers or {}),
        }
        data: bytes | None = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method,
        )
        attempts = self.settings.max_attempts_per_request
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.settings.timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ResoError("provider returned a non-object JSON payload")
                    return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == attempts:
                    raise ResoError(f"HTTP {exc.code} from RESO provider") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == attempts:
                    raise ResoError(f"RESO request failed: {exc}") from exc
            delay = backoff_seconds(attempt, self.settings.backoff_base_seconds)
            logging.warning(
                "RESO request attempt %d/%d failed; retrying in %.1fs.",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
        assert last_error is not None
        raise ResoError(str(last_error))

    def _access_token(self) -> str | None:
        auth = self.settings.auth
        if auth.mode == "none":
            return None
        if self.token:
            return self.token
        if auth.mode == "bearer":
            token = os.environ.get(auth.bearer_token_env, "").strip()
            if not token:
                raise ResoError(
                    f"set {auth.bearer_token_env} to the provider-issued bearer token"
                )
            self.token = token
            return token

        client_id = os.environ.get(auth.client_id_env, "").strip()
        client_secret = os.environ.get(auth.client_secret_env, "").strip()
        if not client_id or not client_secret:
            raise ResoError(
                f"set {auth.client_id_env} and {auth.client_secret_env} to "
                "provider-issued credentials"
            )
        assert auth.token_url is not None
        form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if auth.scope:
            form["scope"] = auth.scope
        payload = self._request_json(auth.token_url, method="POST", form=form)
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise ResoError("token endpoint did not return access_token")
        self.token = token
        return token

    def _authorized_json(self, url: str) -> dict[str, Any]:
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self._request_json(url, headers=headers)

    def _initial_url(self, *, skip: int = 0) -> str:
        endpoint = f"{self.settings.service_root}/{self.settings.resource}"
        query: list[tuple[str, str]] = [("$top", str(self.settings.page_size))]
        if self.settings.select:
            query.append(("$select", ",".join(self.settings.select)))
        if self.settings.filter_expression:
            query.append(("$filter", self.settings.filter_expression))
        if skip:
            query.append(("$skip", str(skip)))
        return endpoint + "?" + urllib.parse.urlencode(query)

    @staticmethod
    def _first(record: dict[str, Any], fields: tuple[str, ...]) -> Any:
        for field in fields:
            value = record.get(field)
            if value is not None and str(value).strip() != "":
                return value
        return ""

    @staticmethod
    def _scalar(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _address(self, record: dict[str, Any]) -> str:
        direct = self._scalar(self._first(record, self.settings.field_map["address"]))
        if direct:
            return direct
        street = " ".join(
            self._scalar(record.get(field))
            for field in (
                "StreetNumber",
                "StreetDirPrefix",
                "StreetName",
                "StreetSuffix",
            )
            if self._scalar(record.get(field))
        )
        unit = self._scalar(record.get("UnitNumber"))
        if unit:
            street = f"{street} #{unit}".strip()
        locality = ", ".join(
            part
            for part in (
                self._scalar(record.get("City")),
                self._scalar(record.get("StateOrProvince")),
                self._scalar(record.get("PostalCode")),
            )
            if part
        )
        return ", ".join(part for part in (street, locality) if part)

    def _map_record(self, source: dict[str, Any]) -> dict[str, str]:
        mapped = {
            field: self._scalar(self._first(source, self.settings.field_map[field]))
            for field in ("listing_id", "price", "beds", "baths", "sqft", "agent", "url")
        }
        mapped["address"] = self._address(source)
        if mapped["url"]:
            parsed = urllib.parse.urlparse(mapped["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                mapped["url"] = ""
        mapped["source_page"] = (
            f"{self.settings.service_root}/{self.settings.resource}"
        )
        mapped["scraped_at"] = datetime.now(UTC).isoformat()
        return {field: mapped.get(field, "") for field in CSV_FIELDS}

    @staticmethod
    def _record_key(record: dict[str, str]) -> str:
        return normalize_record_key(record.get("listing_id", ""), record.get("url", ""))

    def _load_staging_keys(self) -> set[str]:
        if not self.staging_csv.exists():
            return set()
        with self.staging_csv.open(newline="", encoding="utf-8") as handle:
            return {
                self._record_key(row)
                for row in csv.DictReader(handle)
                if self._record_key(row)
            }

    def _append_staging(
        self, records: list[dict[str, str]], seen: set[str]
    ) -> int:
        self.staging_csv.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.staging_csv.exists() or self.staging_csv.stat().st_size == 0
        added = 0
        with self.staging_csv.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if new_file:
                writer.writeheader()
            for record in records:
                key = self._record_key(record)
                if not key or key in seen:
                    continue
                writer.writerow(record)
                seen.add(key)
                added += 1
        return added

    def _read_checkpoint(self) -> dict[str, Any]:
        if not self.settings.checkpoint_file.exists():
            return {}
        try:
            return json.loads(
                self.settings.checkpoint_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            logging.warning("Ignoring unreadable RESO checkpoint.")
            return {}

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _write_checkpoint(
        self,
        *,
        next_url: str | None,
        pages_completed: int,
        records_staged: int,
        complete: bool,
    ) -> None:
        self._write_json_atomic(
            self.settings.checkpoint_file,
            {
                "next_url": next_url,
                "pages_completed": pages_completed,
                "records_staged": records_staged,
                "complete": complete,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def _write_metadata(
        self,
        *,
        pages_completed: int,
        records: int,
        complete: bool,
        published: bool,
        stop_reason: str,
    ) -> None:
        self._write_json_atomic(
            self.settings.metadata_file,
            {
                "schema_version": 1,
                "source_type": "reso_web_api",
                "source_name": self.settings.source_name,
                "service_root": self.settings.service_root,
                "resource": self.settings.resource,
                "coverage_scope": self.settings.coverage_scope,
                "filter": self.settings.filter_expression,
                "license": {
                    "name": self.settings.license_name,
                    "url": self.settings.license_url,
                },
                "retrieval": {
                    "started_at": self.started_at.isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "pages": pages_completed,
                    "records": records,
                    "complete_for_configured_scope": complete,
                    "published": published,
                    "stop_reason": stop_reason,
                },
            },
        )

    @staticmethod
    def _values(payload: dict[str, Any]) -> list[dict[str, Any]]:
        values = payload.get("value", [])
        if not isinstance(values, list) or not all(
            isinstance(item, dict) for item in values
        ):
            raise ResoError("provider payload must contain a list named value")
        return values

    @staticmethod
    def _next_link(payload: dict[str, Any]) -> str | None:
        for key in ("@odata.nextLink", "odata.nextLink", "nextLink"):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    def run(self) -> dict[str, Any]:
        checkpoint = self._read_checkpoint()
        resume = bool(
            checkpoint
            and not checkpoint.get("complete", False)
            and checkpoint.get("next_url")
            and self.staging_csv.exists()
        )
        if resume:
            next_url = str(checkpoint["next_url"])
            pages_completed = int(checkpoint.get("pages_completed", 0))
            logging.info("Resuming RESO ingestion at page %d.", pages_completed + 1)
        else:
            if self.staging_csv.exists():
                self.staging_csv.unlink()
            next_url = self._initial_url()
            pages_completed = 0

        seen = self._load_staging_keys()
        visited: set[str] = set()
        complete = False
        stop_reason = "unknown"

        while next_url:
            if self.settings.max_pages and pages_completed >= self.settings.max_pages:
                stop_reason = "max_pages_reached"
                break
            if next_url in visited:
                raise ResoError("provider pagination loop detected")
            visited.add(next_url)
            logging.info("Fetching RESO page %d: %s", pages_completed + 1, next_url)
            payload = self._authorized_json(next_url)
            source_records = self._values(payload)
            records = [self._map_record(item) for item in source_records]
            added = self._append_staging(records, seen)
            pages_completed += 1

            advertised_next = self._next_link(payload)
            if advertised_next:
                next_url = urllib.parse.urljoin(next_url, advertised_next)
            elif (
                self.settings.pagination_mode in {"auto", "skip"}
                and len(source_records) == self.settings.page_size
            ):
                next_url = self._initial_url(skip=pages_completed * self.settings.page_size)
            else:
                next_url = None
                complete = True
                stop_reason = "provider_exhausted"

            self._write_checkpoint(
                next_url=next_url,
                pages_completed=pages_completed,
                records_staged=len(seen),
                complete=complete,
            )
            logging.info(
                "RESO page complete: %d records, %d newly staged.",
                len(source_records),
                added,
            )

        published = False
        if complete:
            self.settings.output_csv.parent.mkdir(parents=True, exist_ok=True)
            if not self.staging_csv.exists():
                self._append_staging([], seen)
            self.staging_csv.replace(self.settings.output_csv)
            published = True
        self._write_metadata(
            pages_completed=pages_completed,
            records=len(seen),
            complete=complete,
            published=published,
            stop_reason=stop_reason,
        )
        return {
            "complete": complete,
            "published": published,
            "pages": pages_completed,
            "records": len(seen),
            "stop_reason": stop_reason,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest listings from an authorized RESO Web API feed."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        settings = Settings.load(args.config.resolve())
        result = ResoIngester(settings).run()
    except (OSError, ValueError, json.JSONDecodeError, ResoError) as exc:
        logging.error("RESO ingestion failed: %s", exc)
        return 2
    logging.info(
        "RESO ingestion finished: %d records; complete=%s; published=%s.",
        result["records"],
        result["complete"],
        result["published"],
    )
    return 0 if result["complete"] else 4


if __name__ == "__main__":
    sys.exit(main())
