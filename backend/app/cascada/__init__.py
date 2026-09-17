"""CASCADA — the field rebuffering metric.

RBA measures a channel from the outside: it polls the ladder, downloads segments and models
the player. CASCADA measures the same channel from the inside of real televisions, and
publishes a per-minute `rebuffering_ratio` per channel. This package reads that series so the
product can say which channels viewers are actually rebuffering on, and hand those channels
to the analysis that explains why.

Every CASCADA call goes out from the analyzer host through `app.net.fetcher`, never from the
browser: the API needs a session cookie, sends no CORS headers, and the cookie must never
reach a page.
"""
