"""Study-block planner.

Greedy earliest-deadline-first: for each open item due within the horizon,
figure out how many hours are still unplanned and drop blocks into free slots
(between day_start and day_end, avoiding calendar events and existing blocks),
always finishing before the deadline. Existing future blocks are kept so the
calendar doesn't churn; blocks for finished items are cancelled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database, iso, parse_iso, utcnow
from .items import ranked_open_items


@dataclass
class PlannerSettings:
    day_start: time = time(8, 0)
    day_end: time = time(22, 0)
    block_minutes: int = 60
    horizon_days: int = 5
    max_blocks_per_day: int = 4

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "PlannerSettings":
        def t(s: str, d: time) -> time:
            try:
                h, m = s.split(":")
                return time(int(h), int(m))
            except Exception:
                return d

        return cls(
            day_start=t(cfg.get("day_start", "08:00"), time(8, 0)),
            day_end=t(cfg.get("day_end", "22:00"), time(22, 0)),
            block_minutes=int(cfg.get("block_minutes", 60)),
            horizon_days=int(cfg.get("horizon_days", 5)),
            max_blocks_per_day=int(cfg.get("max_blocks_per_day", 4)),
        )


Interval = tuple[datetime, datetime]


def _busy_intervals(db: Database, start: datetime, end: datetime) -> list[Interval]:
    busy: list[Interval] = []
    for e in db.query(
        "SELECT start_at, end_at, all_day, owned FROM events WHERE end_at > ? AND start_at < ?",
        (iso(start), iso(end)),
    ):
        if e["all_day"] or e["owned"]:
            continue  # all-day events (holidays, due markers) don't block time; owned = our own blocks
        s, f = parse_iso(e["start_at"]), parse_iso(e["end_at"])
        if s and f:
            busy.append((s, f))
    for b in db.query(
        "SELECT start_at, end_at FROM blocks WHERE status='planned' AND end_at > ? AND start_at < ?",
        (iso(start), iso(end)),
    ):
        s, f = parse_iso(b["start_at"]), parse_iso(b["end_at"])
        if s and f:
            busy.append((s, f))
    busy.sort()
    return busy


def _free_slots(day_start: datetime, day_end: datetime, busy: list[Interval], minutes: int) -> list[Interval]:
    slots: list[Interval] = []
    cursor = day_start
    step = timedelta(minutes=minutes)
    for s, f in busy:
        if f <= cursor:
            continue
        if s >= day_end:
            break
        while cursor + step <= min(s, day_end):
            slots.append((cursor, cursor + step))
            cursor += step
        cursor = max(cursor, f)
    while cursor + step <= day_end:
        slots.append((cursor, cursor + step))
        cursor += step
    return slots


def _round_up(dt: datetime, minutes: int = 15) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = (-dt.minute) % minutes
    return dt + timedelta(minutes=extra)


def plan(db: Database, tz: ZoneInfo, settings: PlannerSettings, now: datetime | None = None) -> dict[str, Any]:
    """Create/cancel blocks in the DB. Returns {'created': [...], 'cancelled': [...]} block rows."""
    now = now or utcnow()
    created: list[dict[str, Any]] = []
    cancelled: list[dict[str, Any]] = []

    # 1. Cancel future blocks whose item is no longer open.
    for b in db.query(
        """SELECT b.* FROM blocks b JOIN items i ON i.id=b.item_id
           WHERE b.status='planned' AND b.start_at > ? AND i.status != 'open'""",
        (iso(now),),
    ):
        db.execute("UPDATE blocks SET status='cancelled' WHERE id=?", (b["id"],))
        cancelled.append(b)

    # 2. Past planned blocks count as worked.
    db.execute("UPDATE blocks SET status='done' WHERE status='planned' AND end_at <= ?", (iso(now),))

    horizon_end = now + timedelta(days=settings.horizon_days)
    items = ranked_open_items(db, tz, now, horizon_days=settings.horizon_days)
    # Earliest deadline first among things with deadlines in the horizon.
    items = [i for i in items if i["due_at"] and parse_iso(i["due_at"]) and parse_iso(i["due_at"]) > now]
    items.sort(key=lambda i: i["due_at"])

    block_h = settings.block_minutes / 60
    # Precompute free slots per local day.
    day_slots: dict[str, list[Interval]] = {}
    blocks_per_day: dict[str, int] = {}
    local_now = now.astimezone(tz)
    for d in range(settings.horizon_days + 1):
        day = (local_now + timedelta(days=d)).date()
        ds = datetime.combine(day, settings.day_start, tzinfo=tz).astimezone(timezone.utc)
        de = datetime.combine(day, settings.day_end, tzinfo=tz).astimezone(timezone.utc)
        if de <= now:
            continue
        ds = max(ds, _round_up(now + timedelta(minutes=15)))
        if ds >= de:
            continue
        key = day.isoformat()
        day_slots[key] = _free_slots(ds, de, _busy_intervals(db, ds, de), settings.block_minutes)
        existing = db.one(
            "SELECT COUNT(*) AS n FROM blocks WHERE status IN ('planned','done') AND start_at >= ? AND start_at < ?",
            (iso(datetime.combine(day, time(0, 0), tzinfo=tz)), iso(datetime.combine(day, time(23, 59), tzinfo=tz))),
        )
        blocks_per_day[key] = existing["n"] if existing else 0

    for item in items:
        due = parse_iso(item["due_at"])
        assert due is not None
        done_h = sum(
            (parse_iso(b["end_at"]) - parse_iso(b["start_at"])).total_seconds() / 3600  # type: ignore[operator]
            for b in db.query(
                "SELECT start_at, end_at FROM blocks WHERE item_id=? AND status IN ('planned','done')", (item["id"],)
            )
        )
        need = float(item["effort_hours"] or 0) - done_h
        while need > 0.01:
            placed = False
            for key in sorted(day_slots):
                if blocks_per_day.get(key, 0) >= settings.max_blocks_per_day:
                    continue
                slots = day_slots[key]
                for idx, (s, f) in enumerate(slots):
                    if f > due or s >= horizon_end:
                        continue
                    bid = db.execute(
                        "INSERT INTO blocks(item_id, start_at, end_at, status) VALUES(?,?,?, 'planned')",
                        (item["id"], iso(s), iso(f)),
                    )
                    row = db.one("SELECT * FROM blocks WHERE id=?", (bid,))
                    assert row is not None
                    row["title"] = item["title"]
                    row["course"] = item["course"]
                    created.append(row)
                    slots.pop(idx)
                    blocks_per_day[key] = blocks_per_day.get(key, 0) + 1
                    need -= block_h
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                break  # no room before the deadline — surfaced in the UI as "at risk"
    return {"created": created, "cancelled": cancelled}


def at_risk(db: Database, tz: ZoneInfo, settings: PlannerSettings, now: datetime | None = None) -> list[dict[str, Any]]:
    """Open items in the horizon whose planned + done time < estimated effort."""
    now = now or utcnow()
    out = []
    for item in ranked_open_items(db, tz, now, horizon_days=settings.horizon_days):
        if not item["due_at"] or item["bucket"] == "overdue":
            continue
        r = db.one(
            """SELECT COALESCE(SUM((julianday(end_at)-julianday(start_at))*24),0) AS h
               FROM blocks WHERE item_id=? AND status IN ('planned','done')""",
            (item["id"],),
        )
        planned = r["h"] if r else 0
        if planned + 0.01 < float(item["effort_hours"] or 0):
            item["planned_hours"] = round(planned, 2)
            out.append(item)
    return out
