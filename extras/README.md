# extras/

Files here are **not loaded** at startup. Neither `core/action_loader.py`
(which scans `actions/`) nor `core/plugin_loader.py` (which scans `plugins/`)
looks in this folder.

The five skills below are the ones the Mark LV release notes say were taken off
the bundled list. They were still sitting in `actions/`, so every session
loaded them anyway, along with their tool declarations. They are also the
riskiest skills in the project:

| File | What it does | Why it is parked |
|---|---|---|
| `dev_agent.py` | Builds a whole project from a description | Runs commands the model picks and pip-installs packages the model names, with no confirmation |
| `code_helper.py` | Writes, edits, runs and debugs code | Runs and overwrites files with no confirmation or backup |
| `game_updater.py` | Steam/Epic updates, scheduled updates | Can shut the PC down without going through the confirm gate; Epic "update" launches games |
| `flight_finder.py` | Google Flights search | Scrapes the page fragilely |
| `youtube_video.py` | YouTube search, trending and transcript summaries | `summarize` opens a tkinter dialog from a worker thread (unsafe next to Qt) and breaks on youtube-transcript-api 1.x. `video_player` covers playback. |
| `llm_client.py` | Ollama / LM Studio client left over from Mark XL | Nothing imports it |

To turn one back on, move it into `actions/` and restart. `code_helper` and
`flight_finder` already have their argument and URL bugs fixed. Their safety
problems are not fixed yet.
