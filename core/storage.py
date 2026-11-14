"""Playlist persistence.

Backends:
  * GistStorage  - permanent; used when GITHUB_TOKEN + GIST_ID are in secrets.
  * LocalStorage - data/playlists.json (Streamlit Cloud wipes it on restart).

SharedStore sits in front of the backend and is shared by every visitor:
  * all reads come from memory; the Gist is re-checked at most once a minute with
    a conditional request (a "304 Not Modified" reply doesn't count toward GitHub's limit)
  * writes go to memory immediately and are flushed to the Gist by a background
    thread at most once every FLUSH_EVERY seconds, so 100 quick edits = 1 write
  * if GitHub rate-limits us, changes stay in memory (and a local mirror file)
    and the flush is retried with backoff; nothing is lost
Data shape: {"version": 1, "playlists": {pid: playlist}}
"""

from __future__ import annotations

import atexit
import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import requests
import streamlit as st

FILENAME = "playlists.json"
EMPTY = {"version": 1, "playlists": {}}
FLUSH_EVERY = 20      # seconds between Gist writes (max ~180 writes/hour)
REFRESH_EVERY = 60    # seconds between checks for changes made elsewhere


class StorageError(RuntimeError):
    def __init__(self, msg, wait=None):
        super().__init__(msg)
        self.wait = wait  # seconds GitHub asked us to wait, if it said


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class LocalStorage:
    name = "local file"
    permanent = False

    def __init__(self, path: Path):
        self.path = path

    def load(self, etag=None):
        if not self.path.exists():
            return copy.deepcopy(EMPTY), None
        try:
            return _normalise(json.loads(self.path.read_text("utf-8"))), None
        except (OSError, json.JSONDecodeError) as e:
            raise StorageError(f"Could not read {self.path}: {e}") from e

    def save(self, data: dict) -> None:
        _atomic_write(self.path, data)


class GistStorage:
    name = "GitHub Gist"
    permanent = True

    def __init__(self, token: str, gist_id: str):
        self.url = f"https://api.github.com/gists/{gist_id}"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def load(self, etag=None):
        """Returns (data, etag), or (None, etag) when unchanged since `etag`."""
        h = dict(self.headers)
        if etag:
            h["If-None-Match"] = etag
        try:
            r = requests.get(self.url, headers=h, timeout=15)
        except requests.RequestException as e:
            raise StorageError(f"GitHub unreachable: {e}") from e
        if r.status_code == 304:
            return None, etag
        if not r.ok:
            raise StorageError(f"GitHub Gist error {r.status_code}: {r.text[:160]}")
        f = r.json().get("files", {}).get(FILENAME)
        if not f:
            return copy.deepcopy(EMPTY), r.headers.get("ETag")
        content = f.get("content", "")
        if f.get("truncated"):
            content = requests.get(f["raw_url"], headers=self.headers, timeout=15).text
        try:
            return _normalise(json.loads(content or "{}")), r.headers.get("ETag")
        except json.JSONDecodeError as e:
            raise StorageError(f"Gist content is not valid JSON: {e}") from e

    def save(self, data: dict) -> None:
        body = {"files": {FILENAME: {"content": json.dumps(data, ensure_ascii=False, indent=1)}}}
        try:
            r = requests.patch(self.url, headers=self.headers, json=body, timeout=20)
        except requests.RequestException as e:
            raise StorageError(str(e)) from e
        if not r.ok:
            wait = None
            if r.headers.get("Retry-After", "").isdigit():
                wait = int(r.headers["Retry-After"])
            elif r.headers.get("x-ratelimit-remaining") == "0" and r.headers.get("x-ratelimit-reset", "").isdigit():
                wait = max(30, int(r.headers["x-ratelimit-reset"]) - int(time.time()))
            msg = "GitHub is limiting writes for a while" if r.status_code in (403, 429) else f"GitHub error {r.status_code}"
            raise StorageError(msg, wait)


def _normalise(data: dict) -> dict:
    if not isinstance(data, dict):
        return copy.deepcopy(EMPTY)
    data.setdefault("version", 1)
    pls = data.setdefault("playlists", {})
    for pid, p in list(pls.items()):
        p.setdefault("id", pid)
        p.setdefault("name", "Untitled")
        p.setdefault("crossfade", 4.0)
        p.setdefault("items", [])
        p.setdefault("created", time.time())
        p.setdefault("updated", time.time())
    return data


class SharedStore:
    def __init__(self, backend, mirror: Path):
        self.b = backend
        self.name, self.permanent = backend.name, backend.permanent
        self.mirror = mirror
        self.lock = threading.RLock()
        self.data = None
        self.etag = None
        self.checked = 0.0
        self.dirty = False
        self.last_flush = 0.0
        self.retry_at = 0.0
        self.error = None          # last load/save problem, shown in the UI
        self.fails = 0
        self._load(initial=True)
        self._recover_mirror()
        threading.Thread(target=self._flusher, daemon=True).start()
        atexit.register(self.flush)

    # -- reading
    def _load(self, initial=False):
        try:
            data, etag = self.b.load(None if initial else self.etag)
            self.checked = time.time()
            if data is not None and not self.dirty:
                self.data, self.etag = data, etag
            self.error = None if not self.dirty else self.error
        except StorageError as e:
            self.checked = time.time()
            self.error = str(e)
            if self.data is None:
                # GitHub unavailable at start: use the local mirror if we have one
                if self.mirror.exists():
                    self.data = _normalise(json.loads(self.mirror.read_text("utf-8")))
                else:
                    raise

    def _recover_mirror(self):
        """After a restart, unsaved changes from before are still in the mirror file: keep them."""
        try:
            m = _normalise(json.loads(self.mirror.read_text("utf-8")))
        except Exception:
            return
        newest = lambda d: max((p.get("updated", 0) for p in d["playlists"].values()), default=0)
        if self.data is None or newest(m) > newest(self.data) + 1:
            self.data, self.dirty = m, True

    def get(self, force=False) -> dict:
        with self.lock:
            if not self.dirty and (force or time.time() - self.checked > REFRESH_EVERY):
                self._load()
            return copy.deepcopy(self.data)

    # -- writing
    def apply(self, changes: dict) -> dict:
        with self.lock:
            for pid, pl in changes.items():
                if pl is None:
                    self.data["playlists"].pop(pid, None)
                else:
                    pl["updated"] = time.time()
                    self.data["playlists"][pid] = pl
            self.dirty = True
            try:
                _atomic_write(self.mirror, self.data)
            except OSError:
                pass
            if not self.permanent:  # local file: write straight away
                self.flush()
            return copy.deepcopy(self.data)

    def flush(self) -> bool:
        with self.lock:
            if not self.dirty:
                return True
            snapshot = copy.deepcopy(self.data)
        try:
            self.b.save(snapshot)
        except StorageError as e:
            with self.lock:
                self.fails += 1
                backoff = e.wait or min(900, 60 * 2 ** (self.fails - 1))  # 1, 2, 4, 8, 15 min
                self.retry_at = time.time() + backoff
                self.error = (f"{e}. Your changes are kept here and will be saved automatically "
                              f"(next try in about {max(1, round(backoff / 60))} min). Use Export for an extra backup.")
            return False
        with self.lock:
            if self.data == snapshot:
                self.dirty = False
            self.last_flush = time.time()
            self.etag = None  # our own write changed the gist; next check fetches it once
            self.checked = time.time()
            self.error = None
            self.fails = 0
        return True

    def _flusher(self):
        while True:
            time.sleep(2)
            now = time.time()
            if self.dirty and now - self.last_flush >= FLUSH_EVERY and now >= self.retry_at:
                self.flush()

    def status(self) -> dict:
        with self.lock:
            return {"pending": self.dirty, "error": self.error}


@st.cache_resource
def get_storage() -> SharedStore:
    try:
        token = st.secrets.get("GITHUB_TOKEN")
        gist_id = st.secrets.get("GIST_ID")
    except Exception:
        token = gist_id = None
    root = Path(__file__).resolve().parent.parent / "data"
    if token and gist_id:
        return SharedStore(GistStorage(token, gist_id), root / "gist_mirror.json")
    custom = os.environ.get("MUSICMASHUP_DATA")
    path = Path(custom) if custom else root / FILENAME
    return SharedStore(LocalStorage(path), path.with_suffix(".mirror.json"))
