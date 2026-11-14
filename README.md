# MusicMashup

DJ sets from the best parts of YouTube songs. Pick an IN and OUT point for each song,
and the player blends every clip into the next with an automatic crossfade.

- Two decks: the next clip preloads silently, so there are no gaps
- Add songs by search, link, many links at once, or a whole YouTube playlist
- Visual IN/OUT picker with preview; the same song can be used several times
- Live screen: Skip, Play full, Skip next, Play next, set timer; drag to reorder
- No login: everyone who opens the site sees and plays the saved sets

Run locally: `pip install -r requirements.txt && streamlit run app.py`

Streamlit secrets (Settings → Secrets):
```toml
GITHUB_TOKEN = "ghp_..."  # classic token with only the "gist" scope = permanent database
GIST_ID = "..."           # secret gist containing playlists.json
YOUTUBE_API_KEY = "..."   # optional: faster search
APP_PASSWORD = "..."      # optional: lock the site
```
Without GITHUB_TOKEN/GIST_ID, sets are kept in data/playlists.json, which Streamlit Cloud wipes on restart.
