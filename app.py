"""MusicMashup: DJ sets from the best parts of YouTube songs, with auto crossfades.

The UI is one full-screen web app (core/ui/index.html). Python is the backend:
search, titles, saving, password. The page sends events (save/delete/search/
titles/import/sync); each triggers one rerun and the reply returns via args.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import requests
import streamlit as st

from core import youtube
from core.storage import StorageError, get_storage
from core.ui import mashup_ui
from core.utils import new_uid, parse_video_id

st.set_page_config(page_title="MusicMashup", page_icon="🎧", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""<style>
header[data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],
[data-testid="stStatusWidget"],footer,#MainMenu{display:none!important}
.stApp,[data-testid="stAppViewContainer"]{background:#070707!important}
[data-testid="stMainBlockContainer"],.block-container{padding:0!important;max-width:100%!important}
[data-testid="stVerticalBlock"]{gap:0!important} iframe{display:block;border:0}
</style>""", unsafe_allow_html=True)


def _secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


pw = _secret("APP_PASSWORD")
if pw and not st.session_state.get("authed"):
    st.markdown("<style>.block-container{max-width:420px!important;padding:12vh 20px 0!important}</style>",
                unsafe_allow_html=True)
    st.title("🎧 MusicMashup")
    with st.form("login"):
        given = st.text_input("Password", type="password")
        if st.form_submit_button("Enter", type="primary"):
            if given == pw:
                st.session_state.authed = True
                st.rerun()
            st.error("Wrong password")
    st.stop()

storage = get_storage()


def load_data(force=False):
    fresh = time.time() - st.session_state.get("loaded_at", 0) < 10
    if "data" in st.session_state and not force and fresh:
        return
    try:
        st.session_state.data = storage.load()
        st.session_state.loaded_at = time.time()
        st.session_state.load_error = None
    except StorageError as e:
        st.session_state.load_error = str(e)
        st.session_state.setdefault("data", {"version": 1, "playlists": {}})


def commit(changes):
    """Apply {pid: playlist|None} on top of the LATEST saved data (multi-device safe)."""
    try:
        data = storage.load()
    except StorageError:
        data = st.session_state.data
    for pid, pl in changes.items():
        if pl is None:
            data["playlists"].pop(pid, None)
        else:
            pl["updated"] = time.time()
            data["playlists"][pid] = pl
    st.session_state.data, st.session_state.loaded_at = data, time.time()
    try:
        storage.save(data)
    except StorageError as e:
        return str(e)


def _num(v, d=0.0):
    try:
        f = float(v)
        return f if f == f else d
    except (TypeError, ValueError):
        return d


def clean_playlist(p):
    items = []
    for i in (p.get("items") or [])[:1000]:
        vid = parse_video_id(str(i.get("video_id", "")))
        s, e = _num(i.get("start"), -1), _num(i.get("end"), -1)
        if not vid or s < 0 or e <= s:
            continue
        dur = _num(i.get("duration"), 0) or None
        items.append({"uid": str(i.get("uid") or new_uid())[:24], "video_id": vid,
                      "title": str(i.get("title") or vid)[:200], "channel": str(i.get("channel") or "")[:120],
                      "start": round(s, 2), "end": round(e, 2), "duration": round(dur, 2) if dur else None,
                      "auto": i.get("auto") if i.get("auto") in ("replayed", "chapter", "estimate") else None})
    return {"id": str(p.get("id") or new_uid())[:24],
            "name": (str(p.get("name") or "").strip() or "Untitled set")[:80],
            "crossfade": max(0.0, min(12.0, _num(p.get("crossfade"), 4.0))),
            "clip_len": max(20.0, min(240.0, _num(p.get("clip_len"), 75.0))),
            "replayed": p.get("replayed") is not False,
            "items": items, "created": _num(p.get("created"), time.time())}


def _oembed(v):
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": f"https://www.youtube.com/watch?v={v}", "format": "json"}, timeout=8)
        if r.ok:
            j = r.json()
            return {"title": j.get("title"), "channel": j.get("author_name")}
    except Exception:
        pass
    return {"title": None, "channel": None}


def handle(ev):
    kind, pl_ = ev.get("type"), ev.get("payload") or {}
    r = {"id": ev.get("id"), "ok": True}
    if kind == "save":
        p = clean_playlist(pl_.get("playlist") or {})
        err = commit({p["id"]: p})
    elif kind == "delete":
        err = commit({str(pl_.get("pid")): None})
    elif kind == "import":
        new = {}
        for src in (pl_.get("playlists") or [])[:200]:
            p = clean_playlist(dict(src, id=new_uid()))
            for it in p["items"]:
                it["uid"] = new_uid()
            new[p["id"]] = p
        err = commit(new) if new else None
        r["count"] = len(new)
    elif kind == "search":
        r["results"], err = youtube.search(str(pl_.get("q", ""))[:200], 15)
    elif kind == "titles":
        ids = list(dict.fromkeys(v for v in (parse_video_id(str(x)) for x in (pl_.get("ids") or [])[:300]) if v))
        with ThreadPoolExecutor(max_workers=12) as ex:
            r["titles"] = dict(zip(ids, ex.map(_oembed, ids)))
        err = None
    elif kind == "ytplaylist":
        r["title"], r["items"], r["skipped"], err = youtube.playlist(str(pl_.get("url", ""))[:500])
    elif kind == "autocut":
        vids = [v for v in (pl_.get("videos") or [])[:24] if parse_video_id(str(v.get("id", "")))]
        ln, rep = _num(pl_.get("length"), 75), bool(pl_.get("replayed", True))
        with ThreadPoolExecutor(max_workers=8) as ex:
            cuts = ex.map(lambda v: youtube.auto_cut(v["id"], _num(v.get("duration"), 0) or None, ln, rep), vids)
        r["cuts"] = {v["id"]: c for v, c in zip(vids, cuts)}
        err = None
    elif kind == "sync":
        load_data(force=True)
        err = None
    else:
        err = f"unknown event {kind!r}"
    if err:
        r.update(ok=False, error=err)
    return r


load_data()
ev = mashup_ui(data=st.session_state.data, resp=st.session_state.get("resp"),
               info={"permanent": storage.permanent, "load_error": st.session_state.get("load_error")})

if isinstance(ev, dict) and ev.get("id") and ev["id"] != st.session_state.get("last_ev"):
    st.session_state.last_ev = ev["id"]
    try:
        st.session_state.resp = handle(ev)
    except Exception as e:
        st.session_state.resp = {"id": ev["id"], "ok": False, "error": f"Server error: {e}"}
    st.rerun()
