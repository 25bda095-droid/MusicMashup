"""YouTube search + metadata lookups.

Search uses the official YouTube Data API when YOUTUBE_API_KEY is set in
Streamlit secrets (most reliable), and falls back to yt-dlp (no key needed).
"""

from __future__ import annotations

import re

import requests
import streamlit as st

TIMEOUT = 8


def _secret(name: str) -> str | None:
    try:
        return st.secrets.get(name)  # type: ignore[no-any-return]
    except Exception:
        return None


def thumb(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def video_info(video_id: str) -> dict:
    """Return {'title', 'channel', 'duration'} (duration may be None)."""
    info = {"title": None, "channel": None, "duration": None}
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=TIMEOUT,
        )
        if r.ok:
            j = r.json()
            info["title"] = j.get("title")
            info["channel"] = j.get("author_name")
    except Exception:
        pass

    key = _secret("YOUTUBE_API_KEY")
    if key:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails,snippet", "id": video_id, "key": key},
                timeout=TIMEOUT,
            )
            items = r.json().get("items", []) if r.ok else []
            if items:
                info["duration"] = _iso_duration(items[0]["contentDetails"]["duration"])
                info["title"] = info["title"] or items[0]["snippet"]["title"]
                info["channel"] = info["channel"] or items[0]["snippet"]["channelTitle"]
        except Exception:
            pass

    if info["duration"] is None:
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL(
                {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": TIMEOUT}
            ) as ydl:
                j = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
            info["duration"] = j.get("duration")
            info["title"] = info["title"] or j.get("title")
            info["channel"] = info["channel"] or j.get("uploader")
        except Exception:
            pass
    return info


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def search(query: str, limit: int = 10) -> tuple[list[dict], str | None]:
    """Return (results, error). Each result: id, title, channel, duration."""
    query = query.strip()
    if not query:
        return [], None
    key = _secret("YOUTUBE_API_KEY")
    if key:
        res, err = _search_api(query, limit, key)
        if res:
            return res, None
    res, err2 = _search_ytdlp(query, limit)
    if res:
        return res, None
    return [], (err2 or "No results")


def _search_api(query: str, limit: int, key: str) -> tuple[list[dict], str | None]:
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": limit,
                "videoEmbeddable": "true",
                "key": key,
            },
            timeout=TIMEOUT,
        )
        if not r.ok:
            return [], f"YouTube API error {r.status_code}"
        items = r.json().get("items", [])
        ids = [i["id"]["videoId"] for i in items]
        durations: dict[str, float] = {}
        if ids:
            d = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails", "id": ",".join(ids), "key": key},
                timeout=TIMEOUT,
            )
            if d.ok:
                for v in d.json().get("items", []):
                    durations[v["id"]] = _iso_duration(v["contentDetails"]["duration"])
        return [
            {
                "id": i["id"]["videoId"],
                "title": _unescape(i["snippet"]["title"]),
                "channel": i["snippet"]["channelTitle"],
                "duration": durations.get(i["id"]["videoId"]),
            }
            for i in items
        ], None
    except Exception as e:
        return [], str(e)


def _search_ytdlp(query: str, limit: int) -> tuple[list[dict], str | None]:
    try:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
                "socket_timeout": TIMEOUT}
        with yt_dlp.YoutubeDL(opts) as ydl:
            r = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        out = []
        for e in r.get("entries") or []:
            if not e or not e.get("id") or len(e["id"]) != 11:
                continue
            out.append({
                "id": e["id"],
                "title": e.get("title") or e["id"],
                "channel": e.get("channel") or e.get("uploader") or "",
                "duration": e.get("duration"),
            })
        return out, None
    except Exception as e:
        return [], f"Search failed: {e}"


def _iso_duration(s: str) -> float | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + se)


def _unescape(s: str) -> str:
    import html

    return html.unescape(s)


def playlist(url: str) -> tuple[str | None, list[dict], int, str | None]:
    """Fetch every video of a YouTube playlist: (title, items, skipped, error)."""
    from urllib.parse import parse_qs, urlparse

    try:
        lid = parse_qs(urlparse(url.strip()).query).get("list", [None])[0]
    except ValueError:
        lid = None
    if not lid or not re.fullmatch(r"[\w-]{10,64}", lid):
        return None, [], 0, "That link has no playlist id (list=...)"
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                               "skip_download": True, "socket_timeout": 20, "playlistend": 500}) as ydl:
            r = ydl.extract_info(f"https://www.youtube.com/playlist?list={lid}", download=False)
    except Exception as e:
        return None, [], 0, f"Couldn't read that playlist (is it public or unlisted?): {str(e)[:150]}"
    items, skipped = [], 0
    for e in r.get("entries") or []:
        if not e or len(e.get("id") or "") != 11 or not e.get("duration"):
            skipped += 1  # deleted / private / live videos
            continue
        items.append({"id": e["id"], "title": e.get("title") or e["id"],
                      "channel": e.get("channel") or e.get("uploader") or "", "duration": float(e["duration"])})
    return r.get("title"), items, skipped, None


# ---------------------------------------------------------------- auto timing
from functools import lru_cache

_HOOK = re.compile(r"chorus|hook|drop|refrain|main part|best part", re.I)


@lru_cache(maxsize=4096)
def _meta(video_id: str) -> tuple:
    """(duration, heatmap, chapters) from YouTube via yt-dlp; empty on failure."""
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               "socket_timeout": 15}) as ydl:
            r = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
        heat = tuple((float(h["start_time"]), float(h["end_time"]), float(h["value"]))
                     for h in (r.get("heatmap") or []))
        chap = tuple((float(c.get("start_time") or 0), float(c.get("end_time") or 0), str(c.get("title") or ""))
                     for c in (r.get("chapters") or []))
        return (r.get("duration"), heat, chap)
    except Exception:
        return (None, (), ())


def auto_cut(video_id: str, duration: float | None, length: float = 60, use_replayed: bool = True) -> dict:
    """Pick the best part of a song: most-replayed peak → hook chapter → estimate."""
    length = max(20.0, min(240.0, float(length or 75)))
    dur, heat, chap = _meta(video_id) if use_replayed or not duration else (None, (), ())
    dur = float(dur or duration or 0)
    out = {"duration": dur or None}
    if dur <= 0:
        return dict(out, start=0.0, end=length, source="estimate")
    if dur <= length + 15:  # short song: play almost all of it
        st_ = round(min(8.0, dur * 0.05))
        return dict(out, start=st_, end=round(min(dur - 2, st_ + length)), source="estimate")

    if use_replayed and heat:
        # Score every 1 s window of `length` by summed replay value, ignoring the
        # first 10% (people restart songs) and the last 5%.
        def val(t):
            for s, e, v in heat:
                if s <= t < e:
                    return v
            return 0.0
        per_sec = [val(t + 0.5) for t in range(int(dur))]
        lo, hi = int(dur * 0.10), int(dur * 0.95 - length)
        if hi > lo:
            win = sum(per_sec[lo:lo + int(length)])
            best, best_s = win, lo
            for s in range(lo + 1, hi + 1):
                win += per_sec[s + int(length) - 1] - per_sec[s - 1]
                if win > best:
                    best, best_s = win, s
            start = max(0, best_s - 4)  # a little build-up before the hook
            return dict(out, start=float(start), end=float(min(dur - 1, start + length)), source="replayed")

    for s, e, title in chap:
        if _HOOK.search(title):
            start = max(0.0, s - 3)
            return dict(out, start=round(start), end=round(min(dur - 1, start + length)), source="chapter")

    start = round(dur * 0.22)
    return dict(out, start=float(start), end=float(min(dur - 3, start + length)), source="estimate")
