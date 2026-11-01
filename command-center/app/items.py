"""Deadline items: upsert from sources, classify, estimate effort, rank."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database, iso, now_iso, parse_iso, utcnow

COURSE_RE = re.compile(r"(?<![A-Z])([A-Z]{2,5})[\s_\-]?(\d{4}[A-Z]?)(?![0-9A-Z])")

KIND_RULES = [
    ("exam", re.compile(r"\b(exam|midterm|final|test\s*\d*)\b", re.I)),
    ("quiz", re.compile(r"\bquiz", re.I)),
    ("lab", re.compile(r"\blab\b|\blaboratory", re.I)),
    ("project", re.compile(r"\bproject|\bdesign review|\bpresentation|\breport\b", re.I)),
    ("reading", re.compile(r"\bread(ing)?\b|\bchapter\b", re.I)),
    ("assignment", re.compile(r"\b(hw|homework|assignment|problem set|pset|webassign|gradescope)\b", re.I)),
]

DEFAULT_EFFORT = {
    "exam": 6.0,
    "project": 5.0,
    "lab": 3.0,
    "assignment": 2.0,
    "quiz": 1.0,
    "reading": 1.0,
    "task": 1.0,
}


def detect_course(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = COURSE_RE.search(t.upper())
        if m:
            return f"{m.group(1)} {m.group(2)}"
    return None


def classify(title: str, hint: str | None = None) -> str:
    text = f"{title} {hint or ''}"
    for kind, rx in KIND_RULES:
        if rx.search(text):
            return kind
    return "assignment"


def estimate_effort(kind: str, points: float | None, default: float = 2.0) -> float:
    base = DEFAULT_EFFORT.get(kind, default)
    if kind == "assignment":
        base = default
        if points:
            # Bigger assignments take longer — gently.
            if points >= 50:
                base *= 1.5
            elif points <= 5:
                base *= 0.5
    return round(base, 1)


def upsert_item(
    db: Database,
    *,
    uid: str | None,
    source: str,
    title: str,
    course: str | None = None,
    kind: str | None = None,
    due_at: str | None = None,
    url: str | None = None,
    points: float | None = None,
    submitted: bool = False,
    details: str | None = None,
    default_effort: float = 2.0,
) -> tuple[int, bool]:
    """Insert or update an item. Returns (id, created).

    User-controlled fields (status, effort_hours, pinned) are never clobbered,
    except that a Canvas submission auto-completes an open item.
    """
    title = (title or "").strip() or "(untitled)"
    kind = kind or classify(title, details)
    course = course or detect_course(title, details)
    now = now_iso()
    existing = db.one("SELECT * FROM items WHERE uid=?", (uid,)) if uid else None
    if existing:
        status = existing["status"]
        completed_at = existing["completed_at"]
        if submitted and status == "open":
            status, completed_at = "done", now
        db.execute(
            """UPDATE items SET title=?, course=COALESCE(?, course), kind=?, due_at=?, url=COALESCE(?, url),
                   points=COALESCE(?, points), submitted=?, details=COALESCE(?, details), status=?,
                   completed_at=?, updated_at=? WHERE id=?""",
            (title, course, kind, due_at, url, points, int(submitted), details, status, completed_at, now, existing["id"]),
        )
        return existing["id"], False
    effort = estimate_effort(kind, points, default_effort)
    status = "done" if submitted else "open"
    new_id = db.execute(
        """INSERT INTO items(uid, source, course, title, kind, due_at, url, points, effort_hours, status,
                             submitted, details, created_at, updated_at, completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, source, course, title, kind, due_at, url, points, effort, status, int(submitted), details,
         now, now, now if submitted else None),
    )
    if course:
        db.execute("INSERT OR IGNORE INTO courses(code, name) VALUES(?, ?)", (course, course))
    return new_id, True


def set_status(db: Database, item_id: int, status: str) -> dict[str, Any] | None:
    if status not in ("open", "done", "dismissed"):
        raise ValueError("bad status")
    completed = now_iso() if status != "open" else None
    db.execute(
        "UPDATE items SET status=?, completed_at=?, updated_at=? WHERE id=?",
        (status, completed, now_iso(), item_id),
    )
    return db.one("SELECT * FROM items WHERE id=?", (item_id,))


def urgency_bucket(due: datetime | None, now: datetime, tz: ZoneInfo) -> str:
    if due is None:
        return "someday"
    if due < now:
        return "overdue"
    local_due = due.astimezone(tz).date()
    today = now.astimezone(tz).date()
    if local_due == today:
        return "today"
    if local_due == today + timedelta(days=1):
        return "tomorrow"
    if local_due <= today + timedelta(days=7):
        return "week"
    return "later"


def priority_score(item: dict[str, Any], now: datetime) -> float:
    """Higher = do sooner. Roughly 'hours of work per hour remaining', weighted."""
    due = parse_iso(item.get("due_at"))
    effort = float(item.get("effort_hours") or 1.0)
    weight = {"exam": 1.6, "project": 1.3, "quiz": 1.2, "lab": 1.1}.get(item.get("kind") or "", 1.0)
    if item.get("pinned"):
        weight *= 2
    if due is None:
        return 0.05 * weight
    hours_left = (due - now).total_seconds() / 3600
    if hours_left <= 0:
        return 100 + min(-hours_left, 72) / 72  # overdue floats to the top
    # Assume ~6 usable work hours per day remaining.
    usable = max(hours_left / 24 * 6, 0.5)
    return round(weight * effort / usable * 10, 3)


def ranked_open_items(db: Database, tz: ZoneInfo, now: datetime | None = None,
                      horizon_days: int | None = None) -> list[dict[str, Any]]:
    now = now or utcnow()
    rows = db.query("SELECT * FROM items WHERE status='open'")
    out = []
    for r in rows:
        due = parse_iso(r["due_at"])
        if horizon_days is not None and due and due > now + timedelta(days=horizon_days):
            continue
        r["score"] = priority_score(r, now)
        r["bucket"] = urgency_bucket(due, now, tz)
        out.append(r)
    out.sort(key=lambda r: (-r["score"], r["due_at"] or "9999"))
    return out


def course_color(code: str | None, overrides: dict[str, dict[str, str]] | None = None) -> str:
    if code and overrides and code in overrides and overrides[code].get("color"):
        return overrides[code]["color"]
    palette = ["#e5751f", "#3b82f6", "#10b981", "#a855f7", "#ef4444", "#f59e0b", "#14b8a6", "#ec4899"]
    if not code:
        return "#64748b"
    return palette[sum(map(ord, code)) % len(palette)]


def ensure_utc_iso(value: str | None, tz: ZoneInfo) -> str | None:
    """Accept 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM' (local) or full ISO; return UTC ISO."""
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            dt = datetime.fromisoformat(value).replace(hour=23, minute=59, tzinfo=tz)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
    except ValueError:
        return None
    return iso(dt.astimezone(timezone.utc))
