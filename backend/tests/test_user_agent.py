"""What the analyzer identifies as, and that the Settings selection actually applies.

A CDN or a packager can serve differently per User-Agent, so the profile a run went out as
is part of what the run measured. Two things have to hold for that to mean anything: the
strings have to be the ones the devices send, and the selection has to reach the requests.

Neither held. The table carried four Tizen profiles with strings that did not match the
devices, and the Settings selection was written to the preferences row and never read back —
every default was a hardcoded `"tizen5"` in five separate files, so choosing a profile in
Settings changed nothing at all.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.analysis.engine import SessionOptions
from app.api.schemas import JobOptionsIn
from app.config import (
    DEFAULT_UA_PROFILE,
    USER_AGENT_PROFILE_TABLE,
    USER_AGENT_PROFILES,
    default_ua_profile,
    set_default_ua_profile,
)
from app.main import create_app
from app.net.fetcher import Fetcher

# Copied from the device, character for character, independently of the table under test.
DEVICE_STRINGS = {
    "tizen10": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 10.0) AppleWebKit/537.36 (KHTML, like Gecko) 130.0.6723.116/10.0 TV Safari/537.36",
    "tizen9": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 9.0) AppleWebKit/537.36 (KHTML, like Gecko) 120.0.6099.5/9.0 TV Safari/537.36",
    "tizen8": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 8.0) AppleWebKit/537.36 (KHTML, like Gecko) 108.0.5359.1/8.0 TV Safari/537.36",
    "tizen7": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 7.0) AppleWebKit/537.36 (KHTML, like Gecko) 94.0.4606.31/7.0 TV Safari/537.36",
    "tizen65": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36",
    "tizen6": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 (KHTML, like Gecko) 76.0.3809.146/6.0 TV Safari/537.36",
    "tizen55": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 5.5) AppleWebKit/537.36 (KHTML, like Gecko) 69.0.3497.106.1/5.5 TV Safari/537.36",
    "tizen5": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Version/5.0 TV Safari/537.36",
    "tizen4": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 4.0) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 TV Safari/537.36",
    "tizen3": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 3.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/3.0 TV Safari/538.1",
    "tizen24": "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/2.4.0 TV Safari/538.1",
}


@pytest.fixture(autouse=True)
def restore_default() -> Iterator[None]:
    """The default is process state, so a test that changes it puts it back."""
    before = default_ua_profile()
    try:
        yield
    finally:
        set_default_ua_profile(before)


# -- the strings themselves --------------------------------------------------


@pytest.mark.parametrize("profile", sorted(DEVICE_STRINGS))
def test_each_profile_is_the_string_the_device_sends(profile: str) -> None:
    assert USER_AGENT_PROFILES[profile] == DEVICE_STRINGS[profile]


def test_the_oldest_profile_keeps_its_own_spelling() -> None:
    """Tizen 2.4 says `Linux` where every later release says `LINUX`, and carries a third
    version component. Both are what the device sends, and neither is a typo to tidy up."""
    tizen24 = USER_AGENT_PROFILES["tizen24"]

    assert "; Linux; Tizen 2.4.0)" in tizen24
    assert "LINUX" not in tizen24
    assert all("; LINUX; " in USER_AGENT_PROFILES[p] for p in DEVICE_STRINGS if p != "tizen24")


def test_every_profile_carries_a_label_for_the_picker() -> None:
    assert all(p.label for p in USER_AGENT_PROFILE_TABLE.values())
    assert USER_AGENT_PROFILE_TABLE[DEFAULT_UA_PROFILE].label == "Tizen 10.0 (2026)"


# -- the selection actually applying -----------------------------------------


def test_a_job_that_names_no_profile_takes_the_settings_default() -> None:
    """The defect this closes: the selection was stored and every job ignored it."""
    set_default_ua_profile("tizen65")

    options = JobOptionsIn().to_session_options(duration_s=60.0, default_record=False)

    assert options.ua_profile == "tizen65"


def test_a_job_that_names_a_profile_keeps_it() -> None:
    """A per-analysis override outranks the deployment default, as the forms promise."""
    set_default_ua_profile("tizen10")

    options = JobOptionsIn(ua_profile="tizen24").to_session_options(
        duration_s=60.0, default_record=False
    )

    assert options.ua_profile == "tizen24"


def test_a_profile_that_is_not_declared_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown User-Agent profile"):
        JobOptionsIn(ua_profile="tizen99")


def test_a_stored_default_that_no_longer_exists_falls_back_rather_than_sticking() -> None:
    """A stale id must not silently become somebody else's User-Agent — the same rule the
    rule-severity overrides follow when the catalogue stops declaring an id."""
    assert set_default_ua_profile("tizen99") == DEFAULT_UA_PROFILE


# -- what goes out on the wire -----------------------------------------------


def test_the_shared_client_sends_the_selected_profile() -> None:
    """Every outbound request is built from one client, so this is the one place to check."""
    for profile, expected in DEVICE_STRINGS.items():
        fetcher = Fetcher(ua_profile=profile)
        assert fetcher.default_headers["User-Agent"] == expected


def test_a_session_carries_its_profile_into_the_client() -> None:
    options = SessionOptions(ua_profile="tizen8")

    fetcher = Fetcher(ua_profile=options.ua_profile)

    assert fetcher.default_headers["User-Agent"] == DEVICE_STRINGS["tizen8"]


@pytest.mark.asyncio()
async def test_a_subprocess_that_fetches_a_url_carries_the_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No code path pipes a URL to ffprobe today — every call is `pipe:0` — but `probe_url`
    exists and takes a User-Agent. This pins it, so if a URL path is ever added it cannot go
    out as `Lavf/…` and quietly measure a different response than the analysis did."""
    import shutil

    from app.media import ffprobe

    captured: dict[str, list[str]] = {}

    async def fake_run(argv: list[str], **_: object) -> tuple[int, bytes, bytes]:
        captured["argv"] = argv
        return 0, b"{}", b""

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(ffprobe, "_run", fake_run)

    await ffprobe.probe_url("https://example.invalid/a.m3u8", user_agent=DEVICE_STRINGS["tizen10"])

    argv = captured["argv"]
    assert "-user_agent" in argv
    assert argv[argv.index("-user_agent") + 1] == DEVICE_STRINGS["tizen10"]


# -- what the run records about itself ---------------------------------------


def test_a_run_records_the_string_it_went_out_as_not_only_the_profile_id() -> None:
    """`tizen65` means nothing to somebody replaying these URLs by hand; the string does."""
    public = SessionOptions(ua_profile="tizen65").public()

    assert public["ua_profile"] == "tizen65"
    assert public["user_agent"] == DEVICE_STRINGS["tizen65"]


def test_the_report_appendix_states_the_user_agent() -> None:
    from app.reports.render_html import TEMPLATE_DIR

    source = (TEMPLATE_DIR / "report.html.j2").read_text()

    assert "What the analyzer identified as" in source
    assert "{{ user_agent }}" in source
    assert "{{ ua_profile }}" in source


def test_the_evidence_bundle_readme_states_the_user_agent() -> None:
    """Evidence that cannot be replayed under the same conditions is not evidence."""
    from app.reports.service import _bundle_readme

    class _Handle:
        id = "job-1"
        channel_name = "Fixture HD"
        result = None
        options = SessionOptions(ua_profile="tizen9")

    readme = _bundle_readme(_Handle())  # type: ignore[arg-type]

    assert "tizen9" in readme
    assert DEVICE_STRINGS["tizen9"] in readme


# -- what a page is told -----------------------------------------------------


def test_the_settings_endpoint_serves_the_whole_table() -> None:
    """The client holds no second copy; it renders what the requests are actually made with."""
    with TestClient(create_app()) as client:
        body = client.get("/api/settings").json()

    served = {row["id"]: row for row in body["ua_profiles"]}
    assert set(DEVICE_STRINGS) <= set(served), "every Tizen profile reaches the picker"
    assert served["tizen10"]["user_agent"] == DEVICE_STRINGS["tizen10"]
    assert served["tizen10"]["label"] == "Tizen 10.0 (2026)"
