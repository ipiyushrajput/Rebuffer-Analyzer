"""Automated batches: the whole pipeline, run by the backend rather than by a browser tab.

One batch takes a country and carries it end to end — list the channels, scan them against
CASCADA, keep the ones whose average rebuffering is above the threshold, analyse each for a
fixed duration, and produce a report. A scheduled batch then leaves the worst channels aging
for the following week, so the next report can say what was captured at the moments the
rebuffering ratio was actually high.

Nothing here reimplements what the product already does. The catalogue listing, the CASCADA
scan and its averaging, the analysis engine and the job manager are called.
"""
