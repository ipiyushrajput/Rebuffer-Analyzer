"""The Samsung TV Plus live channel catalogue.

One host serves every country and both environments; which data set it reads is decided by
the `dbconnect` parameter, which depends on the country's group and the environment. That
mapping, the country list and the URL it produces live here and nowhere else, so adding a
country or moving an environment is one edit in one file.

The catalogue is reached from the analyzer host, never from the browser: the URL is built
server-side and the request goes out through the shared fetcher, which already disables TLS
verification and suppresses the warning that would otherwise be logged per call.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.net.fetcher import strip_component_suffix

CATALOGUE_URL = "https://tvp-prd-us.samsungdataplus.net/api/live"
PLATFORM = "FREESIA_PLATFORM_2023_0.3"
PAGE_SIZE = 100

ENVIRONMENTS = ("PRD", "STG")


@dataclass(frozen=True, slots=True)
class Country:
    """One selectable country and the data set its channels come from."""

    code: str
    name: str
    group: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "name": self.name, "group": self.group}


# Group A and Group B read different data sets, so each has its own dbconnect pair.
GROUP_DBCONNECT: dict[str, dict[str, int]] = {
    "A": {"PRD": 1, "STG": 3},
    "B": {"PRD": 0, "STG": 2},
}

COUNTRIES: tuple[Country, ...] = (
    Country("AU", "Australia", "A"),
    Country("BR", "Brazil", "A"),
    Country("CA", "Canada", "A"),
    Country("IN", "India", "A"),
    Country("KR", "South Korea", "A"),
    Country("MX", "Mexico", "A"),
    Country("NZ", "New Zealand", "A"),
    Country("PH", "Philippines", "A"),
    Country("SG", "Singapore", "A"),
    Country("TH", "Thailand", "A"),
    Country("US", "United States", "A"),
    Country("AE", "United Arab Emirates", "B"),
    Country("AT", "Austria", "B"),
    Country("BE", "Belgium", "B"),
    Country("CH", "Switzerland", "B"),
    Country("DE", "Germany", "B"),
    Country("DK", "Denmark", "B"),
    Country("EG", "Egypt", "B"),
    Country("ES", "Spain", "B"),
    Country("FI", "Finland", "B"),
    Country("FR", "France", "B"),
    Country("GB", "United Kingdom", "B"),
    Country("IE", "Ireland", "B"),
    Country("IT", "Italy", "B"),
    Country("LU", "Luxembourg", "B"),
    Country("NL", "Netherlands", "B"),
    Country("NO", "Norway", "B"),
    Country("PT", "Portugal", "B"),
    Country("SA", "Saudi Arabia", "B"),
    Country("SE", "Sweden", "B"),
)

BY_CODE: dict[str, Country] = {c.code: c for c in COUNTRIES}


class CatalogueError(Exception):
    """A request the operator can act on: a bad selection, or an origin that did not serve."""


def country(code: str) -> Country:
    found = BY_CODE.get(code.strip().upper())
    if found is None:
        raise CatalogueError(
            f"{code!r} is not a supported country. Supported: {', '.join(sorted(BY_CODE))}."
        )
    return found


def dbconnect(code: str, environment: str) -> int:
    """The data set the catalogue reads for this country in this environment."""
    env = environment.strip().upper()
    if env not in ENVIRONMENTS:
        raise CatalogueError(f"{environment!r} is not an environment. Use PRD or STG.")
    return GROUP_DBCONNECT[country(code).group][env]


def today_stamp(now: dt.datetime | None = None) -> str:
    """The catalogue's `today`, as YYYYMMDD, taken when the operator searches."""
    return (now or dt.datetime.now(dt.UTC)).strftime("%Y%m%d")


def build_url(
    *,
    code: str,
    environment: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    today: str | None = None,
) -> str:
    """The catalogue URL for one page of one country in one environment."""
    if page < 1:
        raise CatalogueError(f"Page {page} is below the first page.")
    query = urlencode(
        {
            "dbconnect": dbconnect(code, environment),
            "country": country(code).code,
            "countryCnt": 1,
            "platform": PLATFORM,
            "today": today or today_stamp(),
            "page": page,
            "pageSize": page_size,
        }
    )
    return f"{CATALOGUE_URL}?{query}"


# -- response parsing --------------------------------------------------------

# A channel is served as a positional array. Only the first five fields are displayed; the
# rest are kept so a later column costs no parser change.
IDX_NUMBER, IDX_SERVICE_ID, IDX_COUNTRY, IDX_NAME, IDX_URL = 0, 1, 2, 3, 4
MIN_FIELDS = 5

TOTAL_KEYS = ("total", "totalCount", "totalCnt", "count", "totalRows", "totalElements")
PAGES_KEYS = ("totalPage", "totalPages", "pageCount", "lastPage")


@dataclass(slots=True)
class Channel:
    """One catalogue row, with the playback URL the analyzer can use as-is."""

    number: str
    service_id: str
    country: str
    name: str
    playback_url: str
    extra: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "service_id": self.service_id,
            "country": self.country,
            "name": self.name,
            "playback_url": self.playback_url,
            "extra": self.extra,
        }


def _looks_like_row(item: Any) -> bool:
    """A channel row is a positional array whose fifth field is the playback URL."""
    return (
        isinstance(item, list)
        and len(item) >= MIN_FIELDS
        and isinstance(item[IDX_URL], str)
        and item[IDX_URL].lower().startswith("http")
    )


def find_rows(payload: Any) -> list[list[Any]]:
    """Locate the channel rows, whether they sit at the top level or under a key.

    The catalogue is an internal service and its envelope is not published, so the rows are
    found by their shape rather than by a key name that a future version may rename. A
    payload carrying no row of that shape raises, and the caller reports what arrived.
    """
    if isinstance(payload, list):
        rows = [item for item in payload if _looks_like_row(item)]
        if rows or not payload:
            return rows
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                rows = [item for item in value if _looks_like_row(item)]
                if rows:
                    return rows
            elif isinstance(value, dict):
                # A branch that holds no rows is not an unreadable response; the next key
                # may still carry them.
                try:
                    nested = find_rows(value)
                except CatalogueError:
                    continue
                if nested:
                    return nested
        # An empty list under any key is an empty page, not an unreadable one.
        if any(isinstance(v, list) for v in payload.values()):
            return []
    raise CatalogueError(
        "The catalogue response carries no channel rows. A row is an array whose fifth "
        f"field is a playback URL; the response was {type(payload).__name__}."
    )


def _first_int(payload: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    for value in payload.values():
        if isinstance(value, dict):
            found = _first_int(value, keys)
            if found is not None:
                return found
    return None


def parse_row(row: list[Any]) -> Channel:
    """One positional array as a channel, with the routing marker removed from the URL."""

    def text(index: int) -> str:
        value = row[index] if index < len(row) else ""
        return "" if value is None else str(value)

    return Channel(
        number=text(IDX_NUMBER),
        service_id=text(IDX_SERVICE_ID),
        country=text(IDX_COUNTRY),
        name=text(IDX_NAME),
        # Every consumer — the table, the copy button, Realtime and Aging — gets the clean URL.
        playback_url=strip_component_suffix(text(IDX_URL).strip()),
        extra=[text(i) for i in range(MIN_FIELDS, len(row))],
    )


@dataclass(slots=True)
class Page:
    """One page of the catalogue, and what is known about the pages around it."""

    channels: list[Channel]
    page: int
    page_size: int
    total: int | None
    total_pages: int | None
    has_next: bool
    url: str
    today: str

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "channels": [c.as_dict() for c in self.channels],
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "total_pages": self.total_pages,
            "has_next": self.has_next,
            "has_previous": self.has_previous,
            "url": self.url,
            "today": self.today,
        }


def parse_page(body: str, *, page: int, page_size: int, url: str, today: str) -> Page:
    """Turn one catalogue response into a page of channels."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise CatalogueError(
            f"The catalogue returned {len(body)} byte(s) that do not parse as JSON: {exc}."
        ) from exc

    rows = find_rows(payload)
    channels = [parse_row(row) for row in rows]

    total = _first_int(payload, TOTAL_KEYS)
    total_pages = _first_int(payload, PAGES_KEYS)
    if total_pages is None and total is not None and page_size > 0:
        total_pages = max(1, -(-total // page_size))

    # Without a count from the origin, a short page is the last one.
    counted = page < total_pages if total_pages is not None else len(channels) >= page_size

    return Page(
        channels=channels,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_next=counted and bool(channels),
        url=url,
        today=today,
    )
