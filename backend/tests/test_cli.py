"""CLI argument handling and the generated rule catalogue."""

from __future__ import annotations

import argparse
import json

import pytest

from app.cli import build_parser, main, parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90.0), ("90s", 90.0), ("5m", 300.0), ("2h", 7200.0), ("1.5h", 5400.0)],
)
def test_durations_parse(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


def test_an_unparseable_duration_is_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration("soon")


def test_the_analyse_command_accepts_every_documented_option() -> None:
    args = build_parser().parse_args(
        [
            "analyse",
            "https://cdn/master.m3u8?token=1",
            "--duration",
            "5m",
            "--origin-url",
            "https://origin/master.m3u8",
            "--cdn-url",
            "https://cdn/master.m3u8",
            "--ssai-url",
            "https://ssai/master.m3u8",
            "--ua-profile",
            "tizen7",
            "--check-set",
            "video_quality",
            "--check-set",
            "scte35_inband",
            "--rendition",
            "v720p@1500k",
            "--vpb-mode",
            "STRICT",
            "--record-evidence",
            "--html",
            "out.html",
        ]
    )
    assert args.duration == 300.0
    assert args.check_set == ["video_quality", "scte35_inband"]
    assert args.vpb_mode == "STRICT"
    assert args.record_evidence is True
    assert args.url.endswith("?token=1")


def test_the_rules_command_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) > 100
    ids = {rule["id"] for rule in payload}
    assert {"MED-004", "SEQ-009", "VID-006", "VPB-001"} <= ids
    for rule in payload:
        assert rule["root_cause"]
        assert rule["fix"]


def test_the_rules_command_renders_markdown(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "--markdown"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("# Rule catalogue")
    assert "| `MED-004` |" in text
    assert "## Threshold defaults" in text
    assert "`rebuffer_ratio_threshold`" in text
