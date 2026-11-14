"""Small helpers: YouTube ID parsing and time formatting/parsing."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def new_uid() -> str:
    return uuid.uuid4().hex[:10]


def parse_video_id(text: str) -> str | None:
    """Extract an 11-char YouTube video id from a URL or a bare id."""
    if not text:
        return None
    text = text.strip()
    if _ID_RE.match(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        u = urlparse(text)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path_parts = [p for p in u.path.split("/") if p]

    if host == "youtu.be" and path_parts:
        cand = path_parts[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(u.query)
        if "v" in qs:
            cand = qs["v"][0]
        elif len(path_parts) >= 2 and path_parts[0] in ("embed", "shorts", "live", "v", "e"):
            cand = path_parts[1]
        else:
            return None
    else:
        return None
    cand = cand[:11]
    return cand if _ID_RE.match(cand) else None


def parse_url_start(text: str) -> float | None:
    """Read a ?t= / &start= timestamp from a YouTube URL, if present."""
    try:
        qs = parse_qs(urlparse(text.strip()).query)
    except ValueError:
        return None
    for key in ("t", "start"):
        if key in qs:
            try:
                return parse_time(qs[key][0])
            except ValueError:
                return None
    return None


def parse_time(value) -> float:
    """Parse '83', '1:23', '01:02:03', '1m23s', '83.5' into seconds."""
    if value is None:
        raise ValueError("empty time")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("negative time")
        return float(value)
    s = str(value).strip().lower()
    if not s:
        raise ValueError("empty time")
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    parts = s.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad time: {value}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"bad time: {value}") from e
    if any(n < 0 for n in nums):
        raise ValueError("negative time")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_time(seconds: float | None, always_hours: bool = False) -> str:
    if seconds is None:
        return "–"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h or always_hours:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def segment_length(item: dict) -> float:
    return max(0.0, float(item["end"]) - float(item["start"]))


def effective_crossfade(seg_a: float, seg_b: float, crossfade: float) -> float:
    """Crossfade can't be longer than a third of either neighbouring clip."""
    return max(0.0, min(crossfade, seg_a / 3, seg_b / 3))


def playlist_total(items: list[dict], crossfade: float) -> float:
    """Real playing time of a playlist, accounting for crossfade overlap."""
    if not items:
        return 0.0
    segs = [segment_length(i) for i in items]
    total = sum(segs)
    for a, b in zip(segs, segs[1:]):
        total -= effective_crossfade(a, b, crossfade)
    return total
