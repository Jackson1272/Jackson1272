# Command Center

A self-hosted dashboard and second brain that runs on your Windows desktop. You open it from your phone over Tailscale.

| Tab | What it does |
|---|---|
| **Today** | Overdue and due-today counts, a ranked "next up" list, today's classes and study blocks, items at risk of being late, quick capture, and recent emails from professors. |
| **Deadlines** | Every course in one list: Canvas plus deadlines found in your email, grouped overdue / today / tomorrow / this week. One-tap done, pin, hide, and edit effort. Below that is a 7-day plan. |
| **Drill** | Weak-topic tracker with spaced repetition (SM-2). It brings topics back sooner when an exam is near. Your local AI generates practice problems from your own notes. |
| **Brain** | Full-text search over your School / Google Drive / OneDrive folders (PDF, docx, md, txt, tex). "Ask my notes" answers questions using your local AI and cites the files. |
| **System** | Sync status, connections, courses, and the API your Claude skills can call. |

**Syncs** run automatically at 6:00 and 18:00 (configurable), about 20 seconds after boot, and whenever you tap the ↻ button. Notes re-index every hour, and only changed files are re-read.

**Google Calendar:** your events are read so the planner knows when you're busy. Deadline markers (⏰) and study blocks (📚) are written **only to a separate "Command Center" calendar**. Your main calendar is never written to. Checking an item off deletes its future blocks and its deadline marker.

---

## 1. Install (about 5 minutes)

Requires **Python 3.11+** (check with `python --version`).

```powershell
cd path\to\command-center
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer:
- creates `.venv` and installs the dependencies
- creates `config.toml` with a random 6-digit **passcode** and prints it
- registers a **"CommandCenter" scheduled task** that starts the server hidden at logon and restarts it if it crashes
- if run as Administrator, opens port 8765 to **Tailscale addresses only** (100.64.0.0/10)

Open `http://localhost:8765` on the desktop. On your phone, use `http://<desktop-tailscale-ip>:8765` (`tailscale ip -4` shows the IP). Enter the passcode once and the cookie lasts a year. **Add it to your home screen** (Safari: Share → Add to Home Screen; Chrome: ⋮ → Install app) and it behaves like an app.

> Optional, nicer URL: in the Tailscale admin console, turn on MagicDNS. Then `http://<desktop-name>:8765` works from any of your devices.

After editing `config.toml`, run `.\cc.ps1 restart`.

## 2. Connect Canvas (pick one)

**A. API token (recommended).** This gives submission status, so items you've submitted check themselves off.
Canvas → Account → Settings → **+ New Access Token** → copy it into `config.toml`:
```toml
[canvas]
token = "1234~abcd..."
```

**B. Calendar feed (no token needed).** Canvas → Calendar → **Calendar Feed** (bottom right) → copy the URL into `ics_url`. You'll check items off yourself.

## 3. Connect Google Calendar + Gmail (about 10 minutes, one time)

1. Go to <https://console.cloud.google.com/> → create a project named "Command Center".
2. **APIs & Services → Library** → enable **Google Calendar API** and **Gmail API**.
3. **OAuth consent screen** → User type **External** → fill in the app name and your email → add your Gmail as a **test user**.
4. **⚠ Then click "Publish app" (In production).** In "Testing" mode, Google expires your login every 7 days. Production mode for a personal, unverified app works fine. You'll see a "Google hasn't verified this app" screen once; click *Advanced → Go to Command Center*.
5. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON → save it as `command-center\google_client_secret.json`.
6. Run:
   ```powershell
   .\cc.ps1 google-auth
   ```
   A browser opens. Approve it, and the token is saved to `data\google_token.json`.

Permissions requested: Calendar (read your events, write to the Command Center calendar) and Gmail **read-only**.

The Gmail query lives in `config.toml` → `gmail_query`. By default it covers mail from `vt.edu`, Gradescope, WebAssign, Canvas, and Piazza from the last 3 days.

## 4. Point it at your notes

```toml
[[notes.folders]]
path = "C:/Users/<you>/School"     # your canvas-course-sync folders
course = ""                          # blank = guess course from folder names like "MATH 2214"

[[notes.folders]]
path = "G:/My Drive/School"          # Google Drive for desktop
course = ""

[[notes.folders]]
path = "C:/Users/<you>/OneDrive/Notes"
course = ""
```

Courses are detected from folder or file names such as `MATH 2214`, `MATH_2214`, or `ESM-2104`. You can also set `course = "ESM 2104"` on a folder that belongs to a single class. Scanned PDFs with no text layer can't be searched yet (OCR is on the roadmap).

## 5. Local AI

Any OpenAI-compatible server works. With **Ollama**:
```powershell
ollama pull llama3.1:8b        # or qwen2.5:14b if your GPU has the VRAM. It's better at math.
```
```toml
[ai]
base_url = "http://localhost:11434/v1"
model = "llama3.1:8b"
```
For LM Studio, use `base_url = "http://localhost:1234/v1"`. If the AI is offline, everything still works: Ask falls back to plain search results, and email deadlines fall back to regex parsing.

## Commands

```powershell
.\cc.ps1 check          # what's configured / reachable
.\cc.ps1 sync           # run every sync now (or: sync --only canvas notes)
.\cc.ps1 brief          # print today's brief
.\cc.ps1 restart        # restart the background server
.\cc.ps1 demo           # load sample data to try the UI (writes into your DB; use a throwaway CC_DATA_DIR)
```

## Hooking up your Claude skills

See [`docs/claude-integration.md`](docs/claude-integration.md). In short:
- **morning brief:** `GET http://localhost:8765/api/brief` returns a ready-to-use markdown summary.
- **tutor / solver:** `POST /api/drill/topics` logs something you struggled on, with practice cards, so it comes back in Drill.
- **canvas-course-sync:** `POST /api/items` adds anything it finds that Canvas didn't list.

Requests from the desktop itself (127.0.0.1) skip the passcode. From other devices, send the header `X-CC-Token: <passcode>`.

## How the planner works

- Effort estimates by type: exam 6h, project 5h, lab 3h, assignment 2h (scaled by points), quiz and reading 1h. Edit any item's effort and the planner adapts.
- Each sync plans earliest-deadline-first into free 60-minute slots between 08:00 and 22:00. It avoids your calendar events, places at most 4 blocks per day, and never schedules a block after the item is due.
- Existing blocks are kept, so your calendar doesn't shuffle around. Blocks for finished items are removed. Past blocks count as time already worked.
- Items that can't get enough time before their deadline show up under **At risk** on Today.

All settings are in `[planner]` in `config.toml`.

## Data

Everything lives in `data\command_center.db` (SQLite). Back it up by copying that file. Nothing leaves your machine except calls to Canvas and Google, and AI calls go only to the local AI server you configure.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
CC_CONFIG=path/to/config.toml python -m app serve --no-scheduler
```
Layout: `app/main.py` (API and auth), `app/sync/*` (canvas, google, notes, runner), `app/planner.py`, `app/drill.py`, `app/ai.py`, `app/static/*` (no-build frontend).
