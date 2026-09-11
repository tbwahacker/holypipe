"""A single UTC "now" used for every timestamp that is stored in or compared
against the metadata database.

SQLite (HolyPipe's own metadata store) round-trips `DateTime` columns as
naive values — timezone info doesn't survive a write/read cycle. Comparing
a naive value loaded from the DB against a timezone-aware `datetime.now()`
raises `TypeError: can't compare offset-naive and offset-aware datetimes`.
Keeping every stored/compared timestamp naive-but-UTC sidesteps that
entirely. (Values sent to the frontend over the websocket/JSON API are
still ISO-formatted with an explicit UTC offset — see `bus.py` — since
those never round-trip through the DB.)
"""
from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
