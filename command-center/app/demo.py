"""Sample data so the dashboard has something to show before real syncs run."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database, iso, utcnow
from .drill import add_card, log_struggle
from .items import upsert_item
from .planner import PlannerSettings, plan

NOTES = {
    "MATH 2214 Diff Eq/Week 9 - Laplace transforms.md": """# Laplace Transforms

Definition: L{f(t)} = F(s) = ∫₀^∞ e^(-st) f(t) dt

Key pairs:
- L{1} = 1/s
- L{e^(at)} = 1/(s-a)
- L{sin(bt)} = b/(s²+b²), L{cos(bt)} = s/(s²+b²)
- L{t^n} = n!/s^(n+1)

Derivatives: L{y'} = sY - y(0), L{y''} = s²Y - s·y(0) - y'(0)

Unit step (Heaviside): L{u(t-c) f(t-c)} = e^(-cs) F(s).
Second shifting theorem — the one that keeps tripping me up: rewrite f(t) in terms of (t-c) first!

Example: y'' + 4y = u(t-π), y(0)=y'(0)=0 → Y = e^(-πs) / (s(s²+4)) → partial fractions 1/4(1/s - s/(s²+4)).
""",
    "MATH 2214 Diff Eq/Week 8 - Variation of parameters.md": """# Variation of Parameters

For y'' + p y' + q y = g(t) with y1, y2 fundamental solutions:
W = y1 y2' - y1' y2
u1' = -y2 g / W, u2' = y1 g / W
y_p = u1 y1 + u2 y2
Use when undetermined coefficients fails (g = tan t, sec t, ln t ...).
""",
    "ESM 2104 Statics/Ch 6 - Trusses and frames.md": """# Trusses — Method of Joints vs Sections

Method of joints: ΣFx = 0, ΣFy = 0 at each pin. Start at a joint with ≤ 2 unknowns.
Zero-force members: at a joint with two non-collinear members and no load, both are zero-force.
Method of sections: cut through ≤ 3 members, take moments about the intersection of two unknowns.
Sign convention: assume tension (pulling away from the joint); negative answer = compression.

Frames & machines: multi-force members — take apart, Newton's third law at pins.
""",
    "PHYS 2306 Physics/Lecture 14 - Electric potential.md": """# Electric Potential

ΔV = -∫ E · dl
Point charge: V = kq/r
Potential energy U = qV. E = -∇V.
Equipotential surfaces are perpendicular to field lines.
Capacitance C = Q/V, parallel plates C = ε₀A/d.
""",
}


def load_demo(db: Database, cfg: Config, notes_dir: str | None = None) -> None:
    tz = ZoneInfo(cfg.timezone)
    now = utcnow()
    today = now.astimezone(tz).date()

    def at(days: int, hh: int, mm: int = 59) -> str:
        return iso(datetime.combine(today + timedelta(days=days), time(hh, mm), tzinfo=tz))  # type: ignore[return-value]

    for code, name in [("MATH 2214", "Differential Equations"), ("ESM 2104", "Statics"),
                       ("PHYS 2306", "Foundations of Physics II"), ("ENGE 1216", "Foundations of Engineering"),
                       ("MATH 2204", "Multivariable Calculus")]:
        db.execute("INSERT OR IGNORE INTO courses(code, name) VALUES(?,?)", (code, name))

    demo_items = [
        ("demo:1", "WebAssign 9.2 — Laplace transforms", "MATH 2214", at(0, 23), 10, "assignment"),
        ("demo:2", "Homework 7: Trusses (method of sections)", "ESM 2104", at(1, 23), 40, "assignment"),
        ("demo:3", "Midterm 2", "MATH 2204", at(4, 19, 0), 100, "exam"),
        ("demo:4", "Lab 6 report — Electric potential", "PHYS 2306", at(2, 17, 0), 25, "lab"),
        ("demo:5", "Quiz 8", "PHYS 2306", at(3, 9, 0), 10, "quiz"),
        ("demo:6", "Design project milestone 2", "ENGE 1216", at(6, 23), 50, "project"),
        ("demo:7", "Read Ch. 7.1–7.3 (Internal forces)", "ESM 2104", at(2, 8, 0), None, "reading"),
        ("demo:8", "Problem set 6", "MATH 2214", at(-1, 23), 20, "assignment"),
    ]
    for uid, title, course, due, pts, kind in demo_items:
        upsert_item(db, uid=uid, source="canvas", title=title, course=course, kind=kind, due_at=due, points=pts,
                    url="https://canvas.vt.edu/")

    # A typical class schedule today/tomorrow.
    for d in range(0, 4):
        for hh, mm, dur, title, loc in [(9, 5, 50, "MATH 2214 Lecture", "McBryde 100"),
                                         (11, 0, 75, "ESM 2104 Lecture", "Goodwin 155"),
                                         (14, 30, 50, "PHYS 2306 Lecture", "Robeson 210")]:
            s = datetime.combine(today + timedelta(days=d), time(hh, mm), tzinfo=tz)
            db.execute(
                """INSERT OR REPLACE INTO events(gcal_id, calendar, title, start_at, end_at, all_day, location, owned)
                   VALUES(?,?,?,?,?,0,?,0)""",
                (f"demo-{d}-{hh}", "VT Classes", title, iso(s), iso(s + timedelta(minutes=dur)), loc),
            )

    t1 = log_struggle(db, name="Laplace transforms with unit step functions", course="MATH 2214",
                      notes="Forgot to shift f(t) to f(t-c) before transforming.", source="tutor")
    add_card(db, t1["id"], "Find L{ u(t-2) · t² }.",
             "Rewrite t² = (t-2)² + 4(t-2) + 4 → e^(-2s)(2/s³ + 4/s² + 4/s).", source="tutor")
    add_card(db, t1["id"], "Solve y'' + y = u(t-π), y(0)=0, y'(0)=1.",
             "Y = 1/(s²+1) + e^(-πs)/(s(s²+1)) → y = sin t + u(t-π)(1 - cos(t-π)) = sin t + u(t-π)(1 + cos t).")
    t2 = log_struggle(db, name="Method of sections — choosing the cut and moment point", course="ESM 2104",
                      notes="Took moments about the wrong point → 3 unknowns in one equation.")
    add_card(db, t2["id"], "Why take moments about the intersection of two cut members?",
             "It eliminates both of those forces, leaving one unknown in the moment equation.")
    log_struggle(db, name="Gauss's law with cylindrical symmetry", course="PHYS 2306")
    log_struggle(db, name="Lagrange multipliers", course="MATH 2204",
                 exam_date=at(4, 19, 0))

    db.execute("INSERT INTO quick_notes(body, course, created_at) VALUES(?,?,?)",
               ("Ask Prof about regrade on HW5 #3", "ESM 2104", iso(now)))
    db.execute(
        """INSERT OR IGNORE INTO mail(gmail_id, sender, subject, snippet, received_at, course, url)
           VALUES(?,?,?,?,?,?,?)""",
        ("demo-mail-1", "Prof. Smith <smith@vt.edu>", "MATH 2204 — Midterm 2 review session",
         "Review session Thursday 6pm in McBryde 113. Exam covers 14.1–15.4.", iso(now - timedelta(hours=5)),
         "MATH 2204", "https://mail.google.com/"),
    )

    if notes_dir:
        from pathlib import Path

        root = Path(notes_dir)
        for rel, text in NOTES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    plan(db, tz, PlannerSettings.from_config(cfg.raw.get("planner", {})))
