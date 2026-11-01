# Wiring Claude skills into Command Center

Command Center listens on `http://localhost:8765`. Calls made from the desktop itself don't need the passcode. From anywhere else, add `-H "X-CC-Token: <passcode>"`.

Paste the snippets below into the matching skill's `SKILL.md` (Cowork / Claude Code on the desktop).

## morning (4am brief)

Add this step at the start of the skill:

> Run `curl -s http://localhost:8765/api/brief` and use the output as the deadlines / schedule / drill section of the brief. It already includes overdue items, items due today and tomorrow, the ranked next-up list, items at risk, today's classes and study blocks, and the count of drill topics due. If the call fails, say "Command Center offline" and continue.

For structured data, use `curl -s http://localhost:8765/api/today` (JSON).

## tutor / solver (track struggles → re-quiz)

Add this at the end of every tutoring or problem-set session:

> For every concept Jackson got wrong or needed a hint on, log it to Command Center:
> ```bash
> curl -s -X POST http://localhost:8765/api/drill/topics -H "Content-Type: application/json" -d '{
>   "name": "<specific concept, e.g. Second shifting theorem with u(t-c)>",
>   "course": "<course code, e.g. MATH 2214>",
>   "notes": "<one line: what went wrong>",
>   "source": "tutor",
>   "exam_date": "<YYYY-MM-DD if an exam on this is coming, else omit>",
>   "cards": [{"question": "<a fresh practice problem like the one missed>", "answer": "<answer + key steps>"}]
> }'
> ```
> Logging the same topic again bumps its struggle count and makes it due immediately.

## canvas-course-sync

> After syncing, add anything that has a due date but isn't on Canvas (syllabus-only deadlines, exam dates from PDFs):
> ```bash
> curl -s -X POST http://localhost:8765/api/items -H "Content-Type: application/json" \
>   -d '{"title": "Midterm 2", "course": "MATH 2204", "due": "2026-11-05T19:00", "kind": "exam", "source": "syllabus"}'
> ```
> `due` is local time (America/New_York). Use `"YYYY-MM-DD"` for an end-of-day deadline.

## Other endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/items?status=open&course=…` | ranked open items |
| PATCH | `/api/items/{id}` | `{"status": "done"}`, `{"effort_hours": 3}`, `{"due": "…"}`, `{"pinned": 1}` |
| GET | `/api/week?days=7` | agenda per day |
| GET | `/api/search?q=…&course=…` | full-text search over notes |
| POST | `/api/ask` | `{"question": "…", "course": "…"}` answered by local AI, citing your notes |
| GET | `/api/drill/due` | topics due for review, with their cards |
| POST | `/api/inbox` | `{"body": "…"}` quick capture |
| POST | `/api/sync` | `{"only": ["canvas"]}` start a sync |

Interactive API docs: `http://localhost:8765/api/docs-ui`
