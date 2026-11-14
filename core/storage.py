"""Playlist persistence.

Two backends:
  * GistStorage  - permanent; used when GITHUB_TOKEN + GIST_ID are in secrets.
  * LocalStorage - data/playlists.json; fine locally, but wiped whenever a
                   Streamlit Cloud app restarts.
Data shape: {"version": 1, "playlists": {pid: playlist}}
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import requests
import streamlit as st

FILENAME = "playlists.json"
EMPTY = {"version": 1, "playlists": {}}


class StorageError(RuntimeError):
    pass


class LocalStorage:
    name = "local file"
    permanent = False

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(EMPTY))
        try:
            return _normalise(json.loads(self.path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise StorageError(f"Could not read {self.path}: {e}") from e

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written file


class GistStorage:
    name = "GitHub Gist"
    permanent = True

    def __init__(self, token: str, gist_id: str):
        self.url = f"https://api.github.com/gists/{gist_id}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def load(self) -> dict:
        try:
            r = requests.get(self.url, headers=self.headers, timeout=15)
        except requests.RequestException as e:
            raise StorageError(f"GitHub unreachable: {e}") from e
        if not r.ok:
            raise StorageError(f"GitHub Gist error {r.status_code}: {r.text[:200]}")
        f = r.json().get("files", {}).get(FILENAME)
        if not f:
            return json.loads(json.dumps(EMPTY))
        content = f.get("content", "")
        if f.get("truncated"):
            content = requests.get(f["raw_url"], headers=self.headers, timeout=15).text
        try:
            return _normalise(json.loads(content or "{}"))
        except json.JSONDecodeError as e:
            raise StorageError(f"Gist content is not valid JSON: {e}") from e

    def save(self, data: dict) -> None:
        body = {"files": {FILENAME: {"content": json.dumps(data, ensure_ascii=False, indent=1)}}}
        last = None
        for attempt in range(3):
            try:
                r = requests.patch(self.url, headers=self.headers, json=body, timeout=15)
                if r.ok:
                    return
                last = f"GitHub Gist error {r.status_code}: {r.text[:200]}"
                if r.status_code < 500:
                    break
            except requests.RequestException as e:
                last = str(e)
            time.sleep(1 + attempt)
        raise StorageError(last or "save failed")


def _normalise(data: dict) -> dict:
    if not isinstance(data, dict):
        return json.loads(json.dumps(EMPTY))
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


@st.cache_resource
def get_storage():
    try:
        token = st.secrets.get("GITHUB_TOKEN")
        gist_id = st.secrets.get("GIST_ID")
    except Exception:
        token = gist_id = None
    if token and gist_id:
        return GistStorage(token, gist_id)
    custom = os.environ.get("MUSICMASHUP_DATA")
    return LocalStorage(Path(custom) if custom else Path(__file__).resolve().parent.parent / "data" / FILENAME)
