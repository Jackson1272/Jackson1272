"""Weak-topic drill with spaced repetition (SM-2 variant, 4 grades).

A *topic* is something you struggled on ("Laplace transforms of step functions").
Each topic carries an SM-2 schedule; *cards* are concrete practice questions
for that topic (written by you, by a Claude skill via the API, or by local AI).
If a topic has an exam date, intervals are capped so it comes back before then.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .db import Database, iso, now_iso, parse_iso, utcnow

GRADES = {0: "again", 1: "hard", 2: "good", 3: "easy"}


def log_struggle(db: Database, *, name: str, course: str | None = None, notes: str | None = None,
                 source: str = "manual", exam_date: str | None = None) -> dict[str, Any]:
    """Create a topic or bump an existing one. Struggling again resets it to due-now."""
    name = name.strip()
    if not name:
        raise ValueError("topic name required")
    course = (course or "").strip() or None
    existing = db.one(
        "SELECT * FROM topics WHERE name=? COLLATE NOCASE AND COALESCE(course,'')=COALESCE(?, '')",
        (name, course),
    )
    now = now_iso()
    if existing:
        db.execute(
            """UPDATE topics SET struggle_count=struggle_count+1, due_at=?, interval_days=0,
                   ease=MAX(1.3, ease-0.15), archived=0,
                   notes=CASE WHEN ? IS NULL OR ? = '' THEN notes
                              WHEN notes IS NULL OR notes='' THEN ?
                              ELSE notes || char(10) || ? END,
                   exam_date=COALESCE(?, exam_date)
               WHERE id=?""",
            (now, notes, notes, notes, notes, exam_date, existing["id"]),
        )
        tid = existing["id"]
    else:
        tid = db.execute(
            """INSERT INTO topics(course, name, notes, source, due_at, created_at, exam_date)
               VALUES(?,?,?,?,?,?,?)""",
            (course, name, notes, source, now, now, exam_date),
        )
    topic = db.one("SELECT * FROM topics WHERE id=?", (tid,))
    assert topic is not None
    return topic


def add_card(db: Database, topic_id: int, question: str, answer: str | None = None, source: str = "manual") -> int:
    return db.execute(
        "INSERT INTO cards(topic_id, question, answer, source, created_at) VALUES(?,?,?,?,?)",
        (topic_id, question.strip(), (answer or "").strip() or None, source, now_iso()),
    )


def schedule(topic: dict[str, Any], grade: int, now: datetime) -> dict[str, Any]:
    """Pure SM-2 step. Returns updated fields."""
    ease = float(topic.get("ease") or 2.5)
    interval = float(topic.get("interval_days") or 0)
    reps = int(topic.get("reps") or 0)
    lapses = int(topic.get("lapses") or 0)
    if grade == 0:
        reps, lapses = 0, lapses + 1
        interval = 10 / (24 * 60)  # 10 minutes
        ease = max(1.3, ease - 0.2)
    else:
        q = {1: 3, 2: 4, 3: 5}[grade]
        ease = max(1.3, ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
        if reps == 0:
            interval = {1: 0.5, 2: 1, 3: 3}[grade]
        elif reps == 1:
            interval = {1: 2, 2: 3, 3: 6}[grade]
        else:
            mult = {1: 1.2, 2: ease, 3: ease * 1.3}[grade]
            interval = max(interval * mult, interval + 1)
        reps += 1
    due = now + timedelta(days=interval)
    exam = parse_iso(topic.get("exam_date"))
    if exam and exam > now and grade > 0:
        # Make sure it comes back at least once more ~1-2 days before the exam.
        latest = exam - timedelta(days=1)
        if due > latest:
            due = max(now + timedelta(hours=12), latest)
            interval = (due - now).total_seconds() / 86400
    return {"ease": round(ease, 3), "interval_days": round(interval, 4), "reps": reps, "lapses": lapses,
            "due_at": iso(due)}


def review(db: Database, topic_id: int, grade: int, card_id: int | None = None) -> dict[str, Any]:
    if grade not in GRADES:
        raise ValueError("grade must be 0-3")
    topic = db.one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if not topic:
        raise KeyError(topic_id)
    now = utcnow()
    upd = schedule(topic, grade, now)
    db.execute(
        """UPDATE topics SET ease=?, interval_days=?, reps=?, lapses=?, due_at=?, last_reviewed=? WHERE id=?""",
        (upd["ease"], upd["interval_days"], upd["reps"], upd["lapses"], upd["due_at"], iso(now), topic_id),
    )
    db.execute(
        "INSERT INTO reviews(topic_id, card_id, grade, reviewed_at) VALUES(?,?,?,?)",
        (topic_id, card_id, grade, iso(now)),
    )
    t = db.one("SELECT * FROM topics WHERE id=?", (topic_id,))
    assert t is not None
    return t


def due_topics(db: Database, now: datetime | None = None, course: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    now = now or utcnow()
    sql = "SELECT * FROM topics WHERE archived=0 AND (due_at IS NULL OR due_at <= ?)"
    params: list[Any] = [iso(now)]
    if course:
        sql += " AND course=?"
        params.append(course)
    sql += " ORDER BY (exam_date IS NULL), exam_date, due_at LIMIT ?"
    params.append(limit)
    topics = db.query(sql, tuple(params))
    for t in topics:
        t["cards"] = db.query("SELECT * FROM cards WHERE topic_id=? ORDER BY RANDOM()", (t["id"],))
    return topics


def all_topics(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        """SELECT t.*, (SELECT COUNT(*) FROM cards c WHERE c.topic_id=t.id) AS card_count,
                  (SELECT ROUND(AVG(grade),2) FROM reviews r WHERE r.topic_id=t.id) AS avg_grade
           FROM topics t WHERE archived=0 ORDER BY course, due_at"""
    )
    return rows


def stats(db: Database) -> dict[str, Any]:
    now = iso(utcnow())
    r = db.one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN due_at IS NULL OR due_at <= ? THEN 1 ELSE 0 END) AS due
           FROM topics WHERE archived=0""",
        (now,),
    ) or {}
    since = iso(utcnow() - timedelta(days=7))
    w = db.one("SELECT COUNT(*) AS n FROM reviews WHERE reviewed_at >= ?", (since,)) or {}
    return {"total": r.get("total") or 0, "due": r.get("due") or 0, "reviews_7d": w.get("n") or 0}
