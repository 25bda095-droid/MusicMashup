"""The MusicMashup interface: a bidirectional Streamlit component in plain HTML/JS.

It's a declared component (not components.html), so Streamlit passes new
arguments to the same iframe on every rerun. Music never stops when the
backend answers a search or saves a playlist.
"""

from pathlib import Path

import streamlit.components.v1 as components

_ui = components.declare_component("musicmashup_ui", path=str(Path(__file__).parent))


def mashup_ui(data: dict, resp: dict | None, info: dict, key: str = "mashup_ui"):
    return _ui(data=data, resp=resp, info=info, key=key, default=None)
