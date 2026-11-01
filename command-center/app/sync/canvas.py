"""Canvas sync.

Preferred: API token -> /api/v1/planner/items (one call covers assignments,
quizzes, discussions and to-dos across every course, with submission status).
Fallback: the Canvas calendar feed (.ics) — no token required, but no
submission status, so you check items off yourself.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import Config
from ..db import Database, iso, utcnow
from ..items import detect_course, upsert_item

TYPE_KIND = {"quiz": "quiz", "discussion_topic": "assignment", "wiki_page": "reading", "planner_note": "task"}
SKIP_TYPES = {"announcement", "calendar_event", "assessment_request"}


def _paginate(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    first = True
    while next_url:
        r = client.get(next_url, params=params if first else None)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            out.extend(data)
        first = False
        next_url = None
        for part in r.headers.get("link", "").split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1: part.find(">")]
    return out


def course_code_from(course: dict[str, Any]) -> str:
    return (
        detect_course(course.get("course_code"), course.get("name"), course.get("original_name"))
        or (course.get("course_code") or course.get("name") or "COURSE").split("_")[0][:20]
    )


def sync_api(db: Database, cfg: Config, *, client: httpx.Client | None = None) -> dict[str, Any]:
    base = (cfg.get("canvas", "base_url", "https://canvas.vt.edu") or "").rstrip("/")
    token = cfg.get("canvas", "token", "")
    own = client is None
    client = client or httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    default_effort = float(cfg.get("planner", "default_effort_hours", 2.0))
    try:
        courses = _paginate(client, f"{base}/api/v1/courses",
                            {"enrollment_state": "active", "per_page": 100, "include[]": "term"})
        code_by_id: dict[str, str] = {}
        for c in courses:
            if not c.get("id") or c.get("access_restricted_by_date"):
                continue
            code = course_code_from(c)
            code_by_id[str(c["id"])] = code
            db.execute(
                """INSERT INTO courses(code, name, canvas_id) VALUES(?,?,?)
                   ON CONFLICT(code) DO UPDATE SET canvas_id=excluded.canvas_id,
                     name=CASE WHEN courses.name IS NULL OR courses.name=courses.code THEN excluded.name ELSE courses.name END""",
                (code, c.get("name") or code, str(c["id"])),
            )
        start = (utcnow() - timedelta(days=14)).date().isoformat()
        end = (utcnow() + timedelta(days=60)).date().isoformat()
        items = _paginate(client, f"{base}/api/v1/planner/items",
                          {"start_date": start, "end_date": end, "per_page": 100})
        created = updated = 0
        for it in items:
            ptype = it.get("plannable_type")
            if ptype in SKIP_TYPES:
                continue
            p = it.get("plannable") or {}
            subs = it.get("submissions") or {}
            submitted = bool(subs.get("submitted") or subs.get("graded") or subs.get("excused")) if isinstance(subs, dict) else False
            if ptype == "planner_note":
                submitted = bool((it.get("planner_override") or {}).get("marked_complete"))
            elif (it.get("planner_override") or {}).get("marked_complete"):
                submitted = True
            due = p.get("due_at") or p.get("todo_date") or it.get("plannable_date")
            cid = str(it.get("course_id") or p.get("course_id") or "")
            course = code_by_id.get(cid) or detect_course(it.get("context_name"))
            url = it.get("html_url") or p.get("html_url")
            if url and url.startswith("/"):
                url = base + url
            _, was_new = upsert_item(
                db,
                uid=f"canvas:{ptype}:{it.get('plannable_id')}",
                source="canvas",
                title=p.get("title") or p.get("name") or "(untitled)",
                course=course,
                kind=TYPE_KIND.get(ptype or ""),
                due_at=iso(_parse_dt(due)) if due else None,
                url=url,
                points=p.get("points_possible"),
                submitted=submitted,
                details=it.get("context_name"),
                default_effort=default_effort,
            )
            created += was_new
            updated += not was_new
        return {"courses": len(code_by_id), "created": created, "updated": updated, "mode": "api"}
    finally:
        if own:
            client.close()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


ICS_ASSIGNMENT_UID = re.compile(r"event-assignment-(?:override-)?(\d+)")
ICS_COURSE_TAG = re.compile(r"\[([^\]]+)\]\s*$")


def sync_ics(db: Database, cfg: Config, *, text: str | None = None, tz_name: str = "America/New_York") -> dict[str, Any]:
    from icalendar import Calendar
    from zoneinfo import ZoneInfo

    if text is None:
        url = cfg.get("canvas", "ics_url", "")
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        text = r.text
    cal = Calendar.from_ical(text)
    tz = ZoneInfo(tz_name)
    horizon_start = utcnow() - timedelta(days=14)
    default_effort = float(cfg.get("planner", "default_effort_hours", 2.0))
    created = updated = 0
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        m = ICS_ASSIGNMENT_UID.search(uid)
        if not m:
            continue  # lectures / office hours / generic calendar events
        summary = str(ev.get("SUMMARY", ""))
        course_tag = ICS_COURSE_TAG.search(summary)
        title = ICS_COURSE_TAG.sub("", summary).strip()
        course = detect_course(course_tag.group(1) if course_tag else None, summary)
        dt = ev.decoded("DTSTART") if ev.get("DTSTART") else None
        if isinstance(dt, datetime):
            due = dt if dt.tzinfo else dt.replace(tzinfo=tz)
        elif isinstance(dt, date):
            due = datetime(dt.year, dt.month, dt.day, 23, 59, tzinfo=tz)
        else:
            due = None
        if due and due.astimezone(timezone.utc) < horizon_start:
            continue
        url = str(ev.get("URL", "")) or None
        _, was_new = upsert_item(
            db, uid=f"canvas:assignment:{m.group(1)}", source="canvas_ics", title=title, course=course,
            due_at=iso(due) if due else None, url=url, default_effort=default_effort,
        )
        created += was_new
        updated += not was_new
    return {"created": created, "updated": updated, "mode": "ics"}


def sync(db: Database, cfg: Config) -> dict[str, Any]:
    if cfg.get("canvas", "token"):
        return sync_api(db, cfg)
    if cfg.get("canvas", "ics_url"):
        return sync_ics(db, cfg, tz_name=cfg.timezone)
    return {"skipped": "no canvas token or ics_url configured"}
