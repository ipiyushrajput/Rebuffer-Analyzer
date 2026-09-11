"""Bulk input parsing.

CSV, XLSX and JSON are accepted. Column names are matched case-insensitively through an
alias table, so a file exported from any of the team's tools loads without editing. Every row
is validated before the job starts and row-level errors are reported back for correction.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

# Canonical column -> accepted aliases, all matched case-insensitively with separators
# normalised, so "Playback URL", "playback-url" and "PLAYBACK_URL" all resolve.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "channel_name": ("channel_name", "channel", "name", "channelname", "title"),
    "playback_url": ("playback_url", "url", "hls_url", "m3u8", "playbackurl", "stream_url", "link"),
    "channel_id": ("channel_id", "id", "channelid", "ref", "channel_ref"),
    "country": ("country", "country_code", "region", "cc"),
    "content_provider": ("content_provider", "provider", "cp", "contentprovider"),
    "cdn": ("cdn", "cdn_name", "delivery"),
    "origin_url": ("origin_url", "origin", "originurl"),
    "cdn_url": ("cdn_url", "cdnurl", "edge_url"),
    "ssai_url": ("ssai_url", "ssai", "mediatailor_url", "mediatailor", "ssaiurl"),
}

REQUIRED = ("channel_name", "playback_url")


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch == "_").replace(
        "__", "_"
    )


def _build_map(headers: list[str]) -> dict[str, str]:
    """Map each source header onto a canonical column name."""
    mapping: dict[str, str] = {}
    lookup: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            lookup[_normalise(alias)] = canonical
    for header in headers:
        canonical = lookup.get(_normalise(header))
        if canonical:
            mapping[header] = canonical
    return mapping


@dataclass
class BulkRow:
    row_index: int
    channel_name: str
    playback_url: str
    origin_url: str | None = None
    cdn_url: str | None = None
    ssai_url: str | None = None
    channel_id: str | None = None
    country: str | None = None
    content_provider: str | None = None
    cdn: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "channel_name": self.channel_name,
            "playback_url": self.playback_url,
            "origin_url": self.origin_url,
            "cdn_url": self.cdn_url,
            "ssai_url": self.ssai_url,
            "channel_id": self.channel_id,
            "country": self.country,
            "content_provider": self.content_provider,
            "cdn": self.cdn,
            "errors": self.errors,
            "valid": self.valid,
        }


class BulkParseError(ValueError):
    """The file itself could not be read."""


def parse(data: bytes, filename: str) -> tuple[list[BulkRow], dict[str, str]]:
    """Parse an uploaded file into rows plus the column mapping that was applied."""
    lowered = filename.lower()
    if lowered.endswith(".json"):
        return _parse_json(data)
    if lowered.endswith((".xlsx", ".xlsm", ".xls")):
        return _parse_xlsx(data)
    if lowered.endswith((".csv", ".tsv", ".txt")):
        return _parse_csv(data, delimiter="\t" if lowered.endswith(".tsv") else None)
    raise BulkParseError(
        f"{filename} is not a supported format. Upload a .csv, .tsv, .xlsx or .json file."
    )


def _records_to_rows(records: list[dict[str, Any]], mapping: dict[str, str]) -> list[BulkRow]:
    rows: list[BulkRow] = []
    for index, record in enumerate(records, start=1):
        values: dict[str, Any] = {}
        for source, canonical in mapping.items():
            raw = record.get(source)
            if raw is None:
                continue
            text = str(raw).strip()
            if text and text.lower() not in ("nan", "none", "null"):
                values[canonical] = text

        row = BulkRow(
            row_index=index,
            channel_name=values.get("channel_name", ""),
            playback_url=values.get("playback_url", ""),
            origin_url=values.get("origin_url"),
            cdn_url=values.get("cdn_url"),
            ssai_url=values.get("ssai_url"),
            channel_id=values.get("channel_id"),
            country=values.get("country"),
            content_provider=values.get("content_provider"),
            cdn=values.get("cdn"),
        )
        _validate(row)
        rows.append(row)
    return rows


def _validate(row: BulkRow) -> None:
    if not row.channel_name:
        row.errors.append("channel_name is empty")
    if not row.playback_url:
        row.errors.append("playback_url is empty")
    for field_name in ("playback_url", "origin_url", "cdn_url", "ssai_url"):
        value = getattr(row, field_name)
        if value and not value.lower().startswith(("http://", "https://")):
            row.errors.append(f"{field_name} is not an absolute http or https URL")


def _parse_csv(data: bytes, *, delimiter: str | None) -> tuple[list[BulkRow], dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = reader.fieldnames or []
    mapping = _build_map(headers)
    _require_columns(mapping, headers)
    return _records_to_rows([dict(record) for record in reader], mapping), mapping


def _parse_json(data: bytes) -> tuple[list[BulkRow], dict[str, str]]:
    try:
        payload = json.loads(data.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        raise BulkParseError(f"The JSON file does not parse: {exc}") from exc

    if isinstance(payload, dict):
        for key in ("channels", "rows", "items", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise BulkParseError(
            "The JSON file must hold a list of objects, or an object with a channels list."
        )

    records = [record for record in payload if isinstance(record, dict)]
    headers = sorted({key for record in records for key in record})
    mapping = _build_map(headers)
    _require_columns(mapping, headers)
    return _records_to_rows(records, mapping), mapping


def _parse_xlsx(data: bytes) -> tuple[list[BulkRow], dict[str, str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is a declared dependency
        raise BulkParseError("openpyxl is not installed on the analyzer host") from exc

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.active
    if sheet is None:
        raise BulkParseError("The workbook holds no sheet")

    rows = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration as exc:
        raise BulkParseError("The sheet is empty") from exc

    headers = [str(cell) if cell is not None else "" for cell in header_row]
    mapping = _build_map(headers)
    _require_columns(mapping, headers)

    records = [
        {headers[i]: value for i, value in enumerate(record) if i < len(headers)}
        for record in rows
        if any(value is not None and str(value).strip() for value in record)
    ]
    workbook.close()
    return _records_to_rows(records, mapping), mapping


def _require_columns(mapping: dict[str, str], headers: list[str]) -> None:
    resolved = set(mapping.values())
    missing = [name for name in REQUIRED if name not in resolved]
    if missing:
        raise BulkParseError(
            f"The file is missing required column(s) {missing}. Headers found: {headers}. "
            f"Accepted aliases: "
            + "; ".join(f"{name} = {', '.join(COLUMN_ALIASES[name])}" for name in missing)
        )


TEMPLATE_COLUMNS = (
    "channel_name",
    "playback_url",
    "channel_id",
    "country",
    "content_provider",
    "cdn",
    "origin_url",
    "cdn_url",
    "ssai_url",
)

TEMPLATE_EXAMPLE = {
    "channel_name": "Samsung TV Plus — Example Channel",
    "playback_url": "https://cdn.example/live/example/master.m3u8",
    "channel_id": "TVP-1001",
    "country": "IN",
    "content_provider": "Example Media",
    "cdn": "Akamai",
    "origin_url": "",
    "cdn_url": "",
    "ssai_url": "",
}


def template_csv() -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(TEMPLATE_COLUMNS))
    writer.writeheader()
    writer.writerow(TEMPLATE_EXAMPLE)
    return buffer.getvalue()


def template_json() -> str:
    return json.dumps({"channels": [TEMPLATE_EXAMPLE]}, indent=2)


def template_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "channels"
    sheet.append(list(TEMPLATE_COLUMNS))
    sheet.append([TEMPLATE_EXAMPLE[name] for name in TEMPLATE_COLUMNS])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
