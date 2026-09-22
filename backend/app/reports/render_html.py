"""HTML report rendering.

The output is self-contained: CSS, the chart data and the logo are inlined, so the file can
be emailed as a single attachment. The same page is what Playwright prints to PDF, so the
two formats never diverge.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.analysis.engine import AnalysisResult
from app.analysis.rules.base import Finding, Severity
from app.api.health import VERSION
from app.jobs.manager import _mask
from app.reports.email_block import build_blocks

TEMPLATE_DIR = Path(__file__).parent / "templates"
ASSET_DIR = Path(__file__).parent / "assets"

# The reports embed the chart library so the HTML renders offline, in an email client, and
# in the headless browser that prints the PDF.
ECHARTS_CANDIDATES = (
    Path(__file__).parent.parent.parent.parent
    / "frontend"
    / "node_modules"
    / "echarts"
    / "dist"
    / "echarts.min.js",
    ASSET_DIR / "echarts.min.js",
)


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["tojson"] = lambda value, indent=None: json.dumps(value, indent=indent, default=str)
    return env


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    encoded = ASSET_DIR / "tvplus-logo.b64.txt"
    if encoded.exists():
        return "data:image/png;base64," + encoded.read_text().strip()
    png = ASSET_DIR / "tvplus-logo.png"
    if png.exists():
        return "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    return ""


@lru_cache(maxsize=1)
def _echarts_source() -> str:
    for candidate in ECHARTS_CANDIDATES:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return ""


def _human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    return f"{minutes / 60:.1f} hours"


def _severity_overrides() -> list[dict[str, str]]:
    """Every rule this deployment files under a severity other than the declared one."""
    from app.analysis.rules import catalogue  # noqa: F401 — declares the rules.
    from app.analysis.rules.base import registry, severity_overrides

    rows = []
    for rule_id, severity in sorted(severity_overrides().items()):
        if rule_id not in registry:
            continue
        rule = registry.get(rule_id)
        rows.append(
            {
                "id": rule.id,
                "title": rule.title,
                "declared": rule.severity.value,
                "effective": severity.value,
            }
        )
    return rows


def _verdict_tone(status: str) -> str:
    if status.startswith("REBUFFERING —"):
        return "critical"
    if status.startswith("REBUFFERING RISK"):
        return "warn"
    return "pass"


def _chart_specs(result: AnalysisResult) -> list[dict[str, Any]]:
    """ECharts options rendered from the stored samples, matching the Realtime charts."""
    charts: list[dict[str, Any]] = []
    base = {
        "animation": False,
        "grid": {"left": 48, "right": 16, "top": 26, "bottom": 30, "containLabel": True},
        "textStyle": {"fontFamily": "JetBrains Mono, Roboto Mono, monospace", "fontSize": 10},
        "tooltip": {"trigger": "axis"},
        "legend": {"top": 0, "itemWidth": 10, "itemHeight": 7, "textStyle": {"fontSize": 9}},
        "color": ["#1428A0", "#7B2CBF", "#FF2D55", "#12864C", "#4B63D6", "#A66CE0"],
    }

    for key, vpb_result in result.vpb_results.items():
        series = [
            {
                "name": f"VPB {vpb_result.variant}",
                "type": "line",
                "showSymbol": False,
                "areaStyle": {"opacity": 0.08},
                "data": [[p.at.isoformat(), p.level_s] for p in vpb_result.points],
                "markArea": {
                    "silent": True,
                    "itemStyle": {"color": "rgba(180,21,27,0.12)"},
                    "data": [
                        [
                            {"xAxis": stall.started_at.isoformat()},
                            {"xAxis": (stall.ended_at or stall.started_at).isoformat()},
                        ]
                        for stall in vpb_result.counted_stalls
                    ],
                },
            }
        ]
        charts.append(
            {
                "title": f"Virtual Player Buffer — {vpb_result.variant}",
                "note": (
                    f"Rebuffering ratio {vpb_result.rebuffer_ratio:.3f}; "
                    f"{len(vpb_result.counted_stalls)} stall(s). Shaded bands are stalls."
                ),
                "option": {
                    **base,
                    "xAxis": {"type": "time"},
                    "yAxis": {"type": "value", "name": "seconds buffered", "min": 0},
                    "series": series,
                },
            }
        )
        del key

    if result.ladder:
        variants = [row["variant"] for row in result.ladder]
        declared = [(row["declared"].get("bandwidth") or 0) / 1000 for row in result.ladder]
        measured = [row["measured"].get("peak_kbps") or 0 for row in result.ladder]
        charts.append(
            {
                "title": "Declared vs measured bitrate",
                "note": "Declared BANDWIDTH against the peak measured on each rung.",
                "option": {
                    **base,
                    "xAxis": {"type": "category", "data": variants, "axisLabel": {"rotate": 20}},
                    "yAxis": {"type": "value", "name": "kbit/s", "min": 0},
                    "series": [
                        {"name": "Declared BANDWIDTH", "type": "bar", "data": declared},
                        {
                            "name": "Measured peak",
                            "type": "scatter",
                            "symbol": "triangle",
                            "symbolSize": 12,
                            "data": measured,
                        },
                    ],
                },
            }
        )

    severity_counts = result.verdict.counts
    charts.append(
        {
            "title": "Findings by severity",
            "note": "Every check that ran, grouped by severity.",
            "option": {
                **base,
                "tooltip": {"trigger": "item"},
                "xAxis": {"type": "category", "data": list(severity_counts.keys())},
                "yAxis": {"type": "value", "name": "findings"},
                "series": [
                    {
                        "type": "bar",
                        "data": [
                            {
                                "value": count,
                                "itemStyle": {
                                    "color": {
                                        "CRITICAL": "#FF2D55",
                                        "ERROR": "#E01142",
                                        "WARN": "#7B2CBF",
                                        "INFO": "#1428A0",
                                        "PASS": "#12864C",
                                    }[severity]
                                },
                            }
                            for severity, count in severity_counts.items()
                        ],
                    }
                ],
            },
        }
    )
    return charts


def render_report(
    result: AnalysisResult,
    *,
    channel_name: str,
    job_type: str,
    urls: dict[str, str | None],
    channel_ref: str | None = None,
    country: str | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    player_ratio: float | None = None,
) -> str:
    """Render the channel report to a self-contained HTML string."""
    verdict = result.verdict.as_dict()
    findings = [f.as_dict() for f in result.findings]

    actionable: list[Finding] = [
        f for f in result.findings if f.severity not in (Severity.PASS, Severity.INFO)
    ]
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for finding in sorted(actionable, key=lambda f: (-f.severity.rank, -f.count, f.rule.id)):
        by_owner.setdefault(finding.owner.label, []).append(finding.as_dict())

    escalations = build_blocks(
        result.findings,
        channel_name=channel_name,
        window_start=result.started_at,
        window_end=result.ended_at,
        playback_url=urls.get("playback_url") or "",
        rebuffer_ratio=verdict.get("measured_rebuffer_ratio"),
        threshold=result.thresholds.rebuffer_ratio_threshold,
    )

    ref_frames = {
        row["measured"].get("max_num_ref_frames")
        for row in result.ladder
        if row["measured"].get("max_num_ref_frames") is not None
    }

    context = {
        "version": VERSION,
        "channel_name": channel_name,
        "channel_ref": channel_ref,
        "country": country,
        "job_type": job_type.title(),
        "started_at": result.started_at.isoformat(timespec="seconds"),
        "ended_at": result.ended_at.isoformat(timespec="seconds"),
        "window_human": _human_duration(verdict["window_seconds"]),
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "urls": urls,
        "masked_urls": {name: _mask(url) for name, url in urls.items()},
        "layers": sorted({f.stream_layer.value for f in result.findings}) or ["PLAYBACK"],
        "verdict": verdict,
        "verdict_tone": _verdict_tone(verdict["status"]),
        "ratio_over": (verdict.get("measured_rebuffer_ratio") or 0)
        > result.thresholds.rebuffer_ratio_threshold,
        "player_ratio": player_ratio,
        "findings": findings,
        "findings_by_owner": by_owner,
        "passes": [f for f in findings if f["severity"] == "PASS"],
        "incidents": [i.as_dict() for i in result.incidents],
        "chains": [c.as_dict() for c in result.chains],
        "layer_diffs": [d.as_dict() for d in result.layer_diffs],
        "ladder": result.ladder,
        "ref_frames_differ": len(ref_frames) > 1,
        "redirect_chains": result.redirect_chains,
        "thresholds": result.thresholds.model_dump(mode="json"),
        # A CDN and a packager can both serve differently per User-Agent, so the string the
        # run went out as is part of what the run measured, and an escalation has to carry it.
        "ua_profile": result.options.get("ua_profile", ""),
        "user_agent": result.options.get("user_agent", ""),
        # A reader of an escalation has to be able to see that a severity was reassigned on
        # this deployment, the same way the thresholds a finding was measured against are
        # stated. An unreassigned analyzer renders nothing here.
        "severity_overrides": _severity_overrides(),
        # What the channel's protection amounted to, per rendition: which system, which key
        # identifier, whether a key was obtained and how many segments were decrypted. A
        # reader has to be able to tell a rendition that passed every bitstream check from
        # one whose payload was never read. Empty for a clear channel, which renders nothing.
        "drm": result.drm,
        "escalations": [block.as_dict() for block in escalations],
        "vpb_summary": [
            {
                "variant": r.variant,
                "rebuffer_ratio": r.rebuffer_ratio,
                "stall_count": len(r.counted_stalls),
                "stall_s": r.stall_s,
                "playing_s": r.playing_s,
                "mode": r.mode.value,
            }
            for r in result.vpb_results.values()
        ],
        "snapshots": snapshots or [],
        "charts": _chart_specs(result),
        "echarts_js": _echarts_source(),
        "logo_data_uri": _logo_data_uri(),
    }

    return _environment().get_template("report.html.j2").render(**context)


def render_html_for_handle(handle: Any, result: AnalysisResult) -> str:
    """Render a report for a job handle, taking the channel identity from the job."""
    extra = handle.extra or {}
    return render_report(
        result,
        channel_name=handle.channel_name,
        job_type=handle.type,
        urls=dict(handle.urls),
        channel_ref=extra.get("channel_id"),
        country=extra.get("country"),
        player_ratio=extra.get("player_ratio"),
    )


def render_bulk_report(
    rows: list[dict[str, Any]],
    *,
    job_id: str,
    started_at: dt.datetime,
    ended_at: dt.datetime,
) -> str:
    """The consolidated ranked report for a bulk job."""
    template = _environment().get_template("bulk.html.j2")
    owner_summary: dict[str, int] = {}
    for row in rows:
        owner = row.get("owner")
        if owner:
            owner_summary[owner] = owner_summary.get(owner, 0) + 1

    ranked = sorted(
        rows,
        key=lambda row: (-(row.get("risk_score") or 0), row.get("channel_name") or ""),
    )
    return template.render(
        version=VERSION,
        job_id=job_id,
        started_at=started_at.isoformat(timespec="seconds"),
        ended_at=ended_at.isoformat(timespec="seconds"),
        generated_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        rows=ranked,
        owner_summary=dict(sorted(owner_summary.items(), key=lambda item: -item[1])),
        total=len(rows),
        logo_data_uri=_logo_data_uri(),
    )
