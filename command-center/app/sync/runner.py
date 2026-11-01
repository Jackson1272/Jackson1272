"""Runs every sync step, logs results, never lets one failure stop the others."""
from __future__ import annotations

import threading
import traceback
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import Database, now_iso
from ..planner import PlannerSettings, plan

_lock = threading.Lock()


def _step(db: Database, source: str, fn: Callable[[], Any]) -> dict[str, Any]:
    started = now_iso()
    try:
        result = fn()
        ok, msg = True, result
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"{type(e).__name__}: {e}"
        traceback.print_exc()
    count = None
    if isinstance(msg, dict):
        count = msg.get("created", msg.get("indexed", msg.get("messages")))
    db.execute(
        "INSERT INTO sync_log(source, started_at, finished_at, ok, count, message) VALUES(?,?,?,?,?,?)",
        (source, started, now_iso(), int(ok), count, str(msg)[:1000]),
    )
    return {"source": source, "ok": ok, "result": msg}


def run_all(db: Database, cfg: Config, *, only: set[str] | None = None) -> list[dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return [{"source": "all", "ok": False, "result": "sync already running"}]
    try:
        from . import canvas, notes

        results = []
        want = (lambda s: only is None or s in only)  # noqa: E731
        if want("canvas") and (cfg.get("canvas", "token") or cfg.get("canvas", "ics_url")):
            results.append(_step(db, "canvas", lambda: canvas.sync(db, cfg)))

        cal = gmail = None
        google_ready = False
        if want("google") or want("gmail") or want("calendar") or want("plan"):
            try:
                from . import google

                cal, gmail = google.build_services(cfg)
                google_ready = cal is not None
            except Exception as e:  # noqa: BLE001
                results.append({"source": "google", "ok": False, "result": f"auth: {e}"})

        if google_ready and cfg.get("google", "gmail_enabled", True) and (want("gmail") or want("google")):
            from . import google

            results.append(_step(db, "gmail", lambda: google.sync_gmail(db, cfg, gmail)))

        own_cal_id = None
        if google_ready and cfg.get("google", "calendar_enabled", True):
            from . import google

            def pull():
                nonlocal own_cal_id
                own_cal_id = google.ensure_calendar(cal, cfg.get("google", "calendar_name", "Command Center"),
                                                    cfg.timezone)
                db.kv_set("gcal_own_id", own_cal_id)
                days = int(cfg.get("planner", "horizon_days", 5)) + 2
                return {"created": google.pull_events(db, cal, own_cal_id, days_ahead=days)}

            if want("calendar") or want("google") or want("plan"):
                results.append(_step(db, "calendar", pull))

        if want("plan") and cfg.get("planner", "enabled", True):
            settings = PlannerSettings.from_config(cfg.raw.get("planner", {}))
            results.append(_step(db, "planner", lambda: {
                k: len(v) for k, v in plan(db, ZoneInfo(cfg.timezone), settings).items()}))

        if google_ready and own_cal_id and cfg.get("google", "calendar_enabled", True):
            from . import google

            results.append(_step(db, "calendar_push", lambda: google.push_calendar(db, cfg, cal, own_cal_id)))

        if want("notes") and cfg.notes_folders:
            results.append(_step(db, "notes", lambda: notes.index_folders(db, cfg)))

        db.kv_set("last_sync", now_iso())
        return results
    finally:
        _lock.release()


def push_calendar_only(db: Database, cfg: Config) -> None:
    """Best-effort immediate calendar cleanup after a check-off in the UI."""
    try:
        from . import google

        cal, _ = google.build_services(cfg)
        own = db.kv_get("gcal_own_id")
        if cal and own:
            google.push_calendar(db, cfg, cal, own)
    except Exception:  # noqa: BLE001 - next scheduled sync retries
        traceback.print_exc()
