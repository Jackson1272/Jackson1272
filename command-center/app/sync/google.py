"""Google Calendar + Gmail.

Auth: run `python -m app.cli google-auth` once on the desktop. It opens a browser,
you approve, and a refresh token is saved to data/google_token.json.

Calendar: reads your calendars into the local cache (so the planner knows when
you're busy) and writes deadline markers + study blocks ONLY to the calendar
named in config (default "Command Center"), creating it if needed.

Gmail: reads recent mail matching gmail_query, keeps a "from your profs" feed,
and pulls deadlines out of it (local AI if available, regex otherwise).
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import Database, iso, now_iso, parse_iso, utcnow
from ..items import detect_course, upsert_item

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def token_path(cfg: Config):
    return cfg.data_dir / "google_token.json"


def get_credentials(cfg: Config):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    tp = token_path(cfg)
    if not tp.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tp.write_text(creds.to_json())
    return creds if creds and creds.valid else None


def run_auth_flow(cfg: Config) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = cfg.resolve(cfg.get("google", "client_secret_file", "google_client_secret.json"))
    if not secret.exists():
        raise FileNotFoundError(f"Google OAuth client file not found: {secret}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path(cfg).write_text(creds.to_json())
    return str(token_path(cfg))


def build_services(cfg: Config):
    from googleapiclient.discovery import build

    creds = get_credentials(cfg)
    if creds is None:
        return None, None
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return cal, gmail


# --------------------------------------------------------------------------- calendar
def ensure_calendar(cal, name: str, tz: str) -> str:
    page = None
    while True:
        resp = cal.calendarList().list(pageToken=page).execute()
        for c in resp.get("items", []):
            if c.get("summary") == name:
                return c["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    created = cal.calendars().insert(body={"summary": name, "timeZone": tz}).execute()
    return created["id"]


def pull_events(db: Database, cal, own_cal_id: str, days_back: int = 1, days_ahead: int = 8) -> int:
    now = utcnow()
    tmin, tmax = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    cals = [c for c in cal.calendarList().list().execute().get("items", [])
            if c.get("selected") or c.get("primary") or c["id"] == own_cal_id]
    rows: list[tuple] = []
    for c in cals:
        page = None
        while True:
            resp = cal.events().list(
                calendarId=c["id"], timeMin=iso(tmin), timeMax=iso(tmax), singleEvents=True,
                orderBy="startTime", maxResults=250, pageToken=page,
            ).execute()
            for e in resp.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s, f = e.get("start", {}), e.get("end", {})
                all_day = "date" in s
                start = s.get("dateTime") or (s.get("date") + "T00:00:00Z" if s.get("date") else None)
                end = f.get("dateTime") or (f.get("date") + "T00:00:00Z" if f.get("date") else None)
                if all_day:
                    # Interpret all-day events in the calendar's own zone — fine for display.
                    start, end = s.get("date") + "T05:00:00Z", f.get("date") + "T05:00:00Z"
                rows.append((f"{c['id']}::{e['id']}", c.get("summaryOverride") or c.get("summary"),
                             e.get("summary", "(busy)"), iso(parse_iso(start)), iso(parse_iso(end)),
                             int(all_day), e.get("location"), int(c["id"] == own_cal_id)))
            page = resp.get("nextPageToken")
            if not page:
                break
    with db.tx() as conn:
        conn.execute("DELETE FROM events WHERE end_at > ? AND start_at < ?", (iso(tmin), iso(tmax)))
        conn.executemany(
            """INSERT OR REPLACE INTO events(gcal_id, calendar, title, start_at, end_at, all_day, location, owned)
               VALUES(?,?,?,?,?,?,?,?)""",
            rows,
        )
    return len(rows)


def _delete_event(cal, cal_id: str, event_id: str) -> None:
    from googleapiclient.errors import HttpError

    try:
        cal.events().delete(calendarId=cal_id, eventId=event_id).execute()
    except HttpError as e:
        if getattr(e, "status_code", None) not in (404, 410) and e.resp.status not in (404, 410):
            raise


def push_calendar(db: Database, cfg: Config, cal, cal_id: str) -> dict[str, int]:
    tz = cfg.timezone
    stats = {"blocks_created": 0, "blocks_deleted": 0, "deadlines_created": 0, "deadlines_updated": 0,
             "deadlines_deleted": 0}

    # Study blocks
    for b in db.query(
        """SELECT b.*, i.title, i.course, i.url FROM blocks b JOIN items i ON i.id=b.item_id
           WHERE b.status='planned' AND b.gcal_event_id IS NULL AND b.start_at > ?""",
        (now_iso(),),
    ):
        body = {
            "summary": f"📚 {b['course'] + ': ' if b['course'] else ''}{b['title']}",
            "description": f"Study block planned by Command Center.\n{b['url'] or ''}",
            "start": {"dateTime": b["start_at"], "timeZone": tz},
            "end": {"dateTime": b["end_at"], "timeZone": tz},
            "colorId": "9",
            "extendedProperties": {"private": {"cc_block": str(b["id"])}},
        }
        ev = cal.events().insert(calendarId=cal_id, body=body).execute()
        db.execute("UPDATE blocks SET gcal_event_id=? WHERE id=?", (ev["id"], b["id"]))
        stats["blocks_created"] += 1
    for b in db.query("SELECT * FROM blocks WHERE status='cancelled' AND gcal_event_id IS NOT NULL"):
        _delete_event(cal, cal_id, b["gcal_event_id"])
        db.execute("UPDATE blocks SET gcal_event_id=NULL WHERE id=?", (b["id"],))
        stats["blocks_deleted"] += 1

    # Deadline markers (15 min ending at the due time) for the next 14 days.
    horizon = iso(utcnow() + timedelta(days=14))
    last_push = db.kv_get("gcal_last_push", "1970-01-01T00:00:00Z")
    for it in db.query(
        "SELECT * FROM items WHERE status='open' AND due_at IS NOT NULL AND due_at > ? AND due_at <= ?",
        (now_iso(), horizon),
    ):
        due = parse_iso(it["due_at"])
        assert due is not None
        body = {
            "summary": f"⏰ DUE: {it['course'] + ' ' if it['course'] else ''}{it['title']}",
            "description": it["url"] or "",
            "start": {"dateTime": iso(due - timedelta(minutes=15)), "timeZone": tz},
            "end": {"dateTime": iso(due), "timeZone": tz},
            "colorId": "11",
            "transparency": "transparent",
            "extendedProperties": {"private": {"cc_item": str(it["id"])}},
        }
        if not it["gcal_event_id"]:
            ev = cal.events().insert(calendarId=cal_id, body=body).execute()
            db.execute("UPDATE items SET gcal_event_id=? WHERE id=?", (ev["id"], it["id"]))
            stats["deadlines_created"] += 1
        elif (it["updated_at"] or "") > (last_push or ""):
            cal.events().patch(calendarId=cal_id, eventId=it["gcal_event_id"], body=body).execute()
            stats["deadlines_updated"] += 1
    for it in db.query("SELECT * FROM items WHERE status!='open' AND gcal_event_id IS NOT NULL"):
        _delete_event(cal, cal_id, it["gcal_event_id"])
        db.execute("UPDATE items SET gcal_event_id=NULL WHERE id=?", (it["id"],))
        stats["deadlines_deleted"] += 1
    db.kv_set("gcal_last_push", now_iso())
    return stats


# --------------------------------------------------------------------------- gmail
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DUE_RE = re.compile(
    r"(?P<label>[^.\n]{0,80}?)\b(?:due|deadline|submit(?:ted)? by|turn in by)\b[^.\n]{0,30}?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})"
    r"|(?P<m2>\d{1,2})/(?P<d2>\d{1,2}))"
    r"(?:[^.\n]{0,20}?(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>am|pm))?",
    re.I,
)


def heuristic_deadlines(text: str, received: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    out = []
    for m in DUE_RE.finditer(text):
        if m.group("mon"):
            month, day = MONTHS[m.group("mon")[:3].lower()], int(m.group("day"))
        else:
            month, day = int(m.group("m2")), int(m.group("d2"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        hour, minute = 23, 59
        if m.group("hour"):
            hour = int(m.group("hour")) % 12 + (12 if m.group("ampm").lower() == "pm" else 0)
            minute = int(m.group("min") or 0)
        local_recv = received.astimezone(tz)
        year = local_recv.year
        try:
            due = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            continue
        if due < local_recv - timedelta(days=60):
            due = due.replace(year=year + 1)
        label = re.sub(r"\s+", " ", m.group("label") or "").strip(" :-,;")
        out.append({"title": label[-80:] or "Deadline from email", "due": due})
    return out


def _body_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(p: dict[str, Any]) -> None:
        mt = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if mt == "text/plain" and data:
            parts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if not parts:
        data = (payload.get("body") or {}).get("data")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
            parts.append(re.sub(r"<[^>]+>", " ", html))
    return "\n".join(parts)


def ai_deadlines(subject: str, body: str, received: datetime, tz: ZoneInfo) -> list[dict[str, Any]] | None:
    from .. import llm

    prompt = (
        "Extract every concrete academic deadline (homework, exam, quiz, project, form) from this email. "
        f"The email was received {received.astimezone(tz).strftime('%A %Y-%m-%d %H:%M')} (America/New_York). "
        'Return JSON: {"deadlines": [{"title": str, "course": str|null, "kind": '
        '"assignment|exam|quiz|project|lab|reading|task", "due": "YYYY-MM-DDTHH:MM" local time}]}. '
        'If there are none, return {"deadlines": []}. Do not invent dates.\n\n'
        f"SUBJECT: {subject}\n\n{body[:6000]}"
    )
    try:
        reply = llm.chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=600)
        data = llm.extract_json(reply)
    except Exception:  # noqa: BLE001 - fall back to heuristics
        return None
    out = []
    for d in (data.get("deadlines") if isinstance(data, dict) else []) or []:
        try:
            due = datetime.fromisoformat(str(d["due"])).replace(tzinfo=tz)
        except (KeyError, ValueError):
            continue
        out.append({"title": str(d.get("title") or subject)[:120], "due": due,
                    "course": d.get("course"), "kind": d.get("kind")})
    return out


def sync_gmail(db: Database, cfg: Config, gmail, *, use_ai: bool = True) -> dict[str, int]:
    tz = ZoneInfo(cfg.timezone)
    q = cfg.get("google", "gmail_query", "newer_than:3d")
    resp = gmail.users().messages().list(userId="me", q=q, maxResults=50).execute()
    new_msgs = found = 0
    for ref in resp.get("messages", []) or []:
        mid = ref["id"]
        if db.one("SELECT 1 FROM kv WHERE key=?", (f"gmail_seen:{mid}",)):
            continue
        msg = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "(no subject)")
        sender = headers.get("from", "")
        received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
        body = _body_text(msg.get("payload", {}))
        course = detect_course(subject, body[:2000])
        db.execute(
            """INSERT OR IGNORE INTO mail(gmail_id, sender, subject, snippet, received_at, course, url)
               VALUES(?,?,?,?,?,?,?)""",
            (mid, sender, subject, msg.get("snippet", ""), iso(received), course,
             f"https://mail.google.com/mail/u/0/#all/{msg.get('threadId', mid)}"),
        )
        deadlines = ai_deadlines(subject, body, received, tz) if use_ai else None
        if deadlines is None:
            deadlines = heuristic_deadlines(f"{subject}\n{body}", received, tz)
        for n, d in enumerate(deadlines):
            if d["due"].astimezone(timezone.utc) < utcnow() - timedelta(days=1):
                continue
            upsert_item(
                db, uid=f"gmail:{mid}:{n}", source="gmail", title=d["title"],
                course=d.get("course") or course, kind=d.get("kind"), due_at=iso(d["due"]),
                url=f"https://mail.google.com/mail/u/0/#all/{msg.get('threadId', mid)}",
                details=f"From: {sender}\nSubject: {subject}",
            )
            found += 1
        db.kv_set(f"gmail_seen:{mid}", now_iso())
        new_msgs += 1
    return {"messages": new_msgs, "deadlines": found}
