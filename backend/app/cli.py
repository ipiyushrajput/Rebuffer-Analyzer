"""Command line interface.

    python -m app.cli analyse <url> --duration 5m --html out.html
    python -m app.cli rules --markdown > ../docs/RULES.md

The `analyse` command runs the same engine the Realtime, Aging and Bulk tabs run, so a
headless run and a UI run produce the same findings for the same stream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from app.analysis.engine import AnalysisResult, AnalysisSession, SessionOptions
from app.analysis.rules import catalogue  # noqa: F401 — importing declares the catalogue.
from app.analysis.rules.base import Severity, registry
from app.config import get_thresholds

DURATION_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([smh]?)$", re.IGNORECASE)

SEVERITY_MARK = {
    Severity.CRITICAL: "CRITICAL",
    Severity.ERROR: "ERROR   ",
    Severity.WARN: "WARN    ",
    Severity.INFO: "INFO    ",
    Severity.PASS: "PASS    ",
}


def parse_duration(value: str) -> float:
    """Accept `90`, `90s`, `5m` or `2h` and return seconds."""
    match = DURATION_PATTERN.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Duration {value!r} is not a number optionally followed by s, m or h"
        )
    amount = float(match.group(1))
    unit = match.group(2).lower() or "s"
    return amount * {"s": 1, "m": 60, "h": 3600}[unit]


async def _run_analysis(args: argparse.Namespace) -> AnalysisResult:
    check_sets: list[str] = ["baseline"]
    if args.check_set:
        check_sets += list(args.check_set)

    options = SessionOptions(
        duration_s=args.duration,
        ua_profile=args.ua_profile,
        check_sets=tuple(dict.fromkeys(check_sets)),
        renditions=tuple(args.rendition) if args.rendition else None,
        record_evidence=args.record_evidence,
        vpb_mode=args.vpb_mode,
        snapshot_mode=args.duration <= 300,
    )

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        if not args.verbose:
            return
        if kind == "finding":
            data = payload["data"]
            print(
                f"  {data['severity']:<8} {data['rule_id']:<9} {data['title']}",
                file=sys.stderr,
            )

    session = AnalysisSession(
        session_id="cli",
        playback_url=args.url,
        origin_url=args.origin_url,
        cdn_url=args.cdn_url,
        ssai_url=args.ssai_url,
        channel_name=args.channel_name,
        options=options,
        on_event=on_event,
    )
    return await session.run()


def _print_summary(result: AnalysisResult) -> None:
    verdict = result.verdict
    print()
    print("=" * 78)
    print(f"  {verdict.status.value}")
    print("=" * 78)
    print(f"  Primary root cause : {verdict.headline}")
    if verdict.owner_label:
        print(f"  Responsible party  : {verdict.owner_label}")
    if verdict.required_fix:
        print(f"  Required fix       : {verdict.required_fix}")
    print(f"  Risk score         : {verdict.risk_score}/100")
    if verdict.measured_rebuffer_ratio is not None:
        print(
            f"  Rebuffer ratio     : {verdict.measured_rebuffer_ratio:.3f} "
            f"(threshold {result.thresholds.rebuffer_ratio_threshold}) "
            f"on {verdict.worst_variant or 'the sampled rung'}"
        )
    print(
        f"  Window             : {verdict.window_seconds:.0f} s, "
        f"{verdict.playlists_checked} playlists, {verdict.segments_checked} segments"
    )
    print(f"  Incidents          : {verdict.incident_count} ({verdict.incident_seconds:.0f} s)")
    counts = ", ".join(f"{k} {v}" for k, v in verdict.counts.items() if v)
    print(f"  Findings           : {counts or 'none'}")
    print()

    for finding in result.findings:
        if finding.severity is Severity.PASS:
            continue
        print(f"  {SEVERITY_MARK[finding.severity]} {finding.rule.id:<9} {finding.rule.title}")
        print(f"           owner: {finding.owner.label}  x{finding.count}")
        print(f"           {finding.detail}")
        print(f"           fix: {finding.rule.fix}")
        print()

    if result.chains:
        print("  Stall correlation")
        for chain in result.chains[:10]:
            print(f"    {chain.describe()}")
        print()


def _cmd_analyse(args: argparse.Namespace) -> int:
    result = asyncio.run(_run_analysis(args))
    _print_summary(result)

    if args.json:
        Path(args.json).write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
        print(f"  JSON written to {args.json}")

    if args.html or args.pdf:
        from app.reports.render_html import render_report

        html = render_report(
            result,
            channel_name=args.channel_name or args.url,
            job_type="cli",
            urls={
                "playback_url": args.url,
                "origin_url": args.origin_url,
                "cdn_url": args.cdn_url,
                "ssai_url": args.ssai_url,
            },
        )
        if args.html:
            Path(args.html).write_text(html, encoding="utf-8")
            print(f"  HTML report written to {args.html}")
        if args.pdf:
            from app.reports.render_pdf import render_pdf_sync

            render_pdf_sync(html, Path(args.pdf))
            print(f"  PDF report written to {args.pdf}")

    return 0 if result.verdict.status.name == "NO_STREAM_SIDE_DEFECT" else 2


def _cmd_rules(args: argparse.Namespace) -> int:
    rules = registry.all()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": r.id,
                        "layer": r.layer,
                        "severity": r.severity.value,
                        "owner": r.owner.value,
                        "title": r.title,
                        "root_cause": r.root_cause,
                        "fix": r.fix,
                        "rebuffer_impact": r.rebuffer_impact.value,
                        "reference": r.reference,
                        "thresholds": list(r.thresholds),
                    }
                    for r in rules
                ],
                indent=2,
            )
        )
        return 0

    if not args.markdown:
        for rule in rules:
            print(f"{rule.id:<9} {rule.severity.value:<8} {rule.owner.value:<18} {rule.title}")
        return 0

    print("# Rule catalogue")
    print()
    print(
        f"{len(rules)} rules, generated from `backend/app/analysis/rules/catalogue.py` by "
        "`make rules`. Do not edit this file by hand."
    )
    print()
    print(
        "Every rule states the defect, the responsible owner, a single definite root-cause "
        "sentence and a single fix. The **Reference** column names the equivalent check in the "
        "market tools studied in [REFERENCE_TOOLS.md](REFERENCE_TOOLS.md)."
    )
    print()
    print(
        "**Rebuffer impact** — `direct`: the defect stops or starves playback on its own; "
        "`indirect`: it consumes headroom a player needs; `none`: it is a conformance or "
        "information finding."
    )
    print()

    groups: dict[str, list[Any]] = {}
    for rule in rules:
        groups.setdefault(rule.layer, []).append(rule)

    thresholds = get_thresholds().model_dump(mode="json")

    for layer in sorted(groups):
        print(f"## {layer}")
        print()
        print("| ID | Severity | Owner | Title | Impact | Thresholds | Reference |")
        print("|---|---|---|---|---|---|---|")
        for rule in groups[layer]:
            names = ", ".join(f"`{n}`" for n in rule.thresholds) or "—"
            print(
                f"| `{rule.id}` | {rule.severity.value} | {rule.owner.label} | {rule.title} | "
                f"{rule.rebuffer_impact.value} | {names} | {rule.reference or '—'} |"
            )
        print()
        for rule in groups[layer]:
            print(f"#### `{rule.id}` — {rule.title}")
            print()
            print(f"**Root cause.** {rule.root_cause}")
            print()
            print(f"**Fix.** {rule.fix}")
            print()

    print("## Threshold defaults")
    print()
    print("| Threshold | Default |")
    print("|---|---|")
    for name, value in thresholds.items():
        print(f"| `{name}` | `{value}` |")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rba",
        description="TV Plus Rebuffer Analyzer — headless analysis and rule catalogue.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyse = sub.add_parser("analyse", help="Analyse a channel and print the verdict")
    analyse.add_argument("url", help="Playback URL")
    analyse.add_argument("--origin-url", default=None)
    analyse.add_argument("--cdn-url", default=None)
    analyse.add_argument("--ssai-url", default=None)
    analyse.add_argument("--channel-name", default=None)
    analyse.add_argument("--duration", type=parse_duration, default=300.0, help="e.g. 5m, 2h, 90s")
    analyse.add_argument("--ua-profile", default="tizen5")
    analyse.add_argument("--check-set", action="append", default=None)
    analyse.add_argument("--rendition", action="append", default=None)
    analyse.add_argument("--vpb-mode", default=None, choices=["STRICT", "NORMAL", "OUTAGE_ONLY"])
    analyse.add_argument("--record-evidence", action="store_true")
    analyse.add_argument("--html", default=None, help="Write the HTML report here")
    analyse.add_argument("--pdf", default=None, help="Write the PDF report here")
    analyse.add_argument("--json", default=None, help="Write the raw result JSON here")
    analyse.add_argument("--verbose", "-v", action="store_true")
    analyse.set_defaults(func=_cmd_analyse)

    rules = sub.add_parser("rules", help="Print the rule catalogue")
    rules.add_argument("--markdown", action="store_true", help="Render docs/RULES.md")
    rules.add_argument("--json", action="store_true")
    rules.set_defaults(func=_cmd_rules)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
