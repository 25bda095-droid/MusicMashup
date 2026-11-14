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
