"""AI features on top of your notes, using the local model."""
from __future__ import annotations

import re
from typing import Any

from . import llm
from .db import Database
from .drill import add_card
from .sync.notes import search

STOP = set("""a an and are as at be by can do does for from how i in is it me my of on or so that the this to
what when where which who why with you your explain define find show give""".split())


def _keywords(q: str) -> str:
    words = [w for w in re.findall(r"[\w\-']+", q.lower()) if w not in STOP and len(w) > 1]
    return " ".join(words) or q


def retrieve(db: Database, question: str, course: str | None = None, k: int = 6) -> list[dict[str, Any]]:
    hits = search(db, _keywords(question), course=course, limit=k * 2)
    seen, out = set(), []
    for h in hits:
        key = (h["doc_id"], h["chunk_no"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= k:
            break
    return out


def ask(db: Database, question: str, course: str | None = None) -> dict[str, Any]:
    ctx = retrieve(db, question, course)
    sources = [{"n": i + 1, "title": h["title"], "path": h["path"], "page": h["page"], "doc_id": h["doc_id"],
                "course": h["course"]} for i, h in enumerate(ctx)]
    if not ctx:
        context = "(no matching notes found)"
    else:
        context = "\n\n".join(
            f"[{i + 1}] {h['title']}{' p.' + str(h['page']) if h['page'] else ''}\n{h['content'][:1400]}"
            for i, h in enumerate(ctx)
        )
    system = (
        "You are Jackson's study assistant (mechanical engineering, Virginia Tech). Answer using the notes "
        "excerpts provided, citing them like [1], [2]. If the notes don't cover it, say so plainly and then "
        "answer from general knowledge, clearly labeled. Be concise; show equations and steps when relevant."
    )
    answer = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"NOTES:\n{context}\n\nQUESTION: {question}"}],
        max_tokens=900,
    )
    return {"answer": answer, "sources": sources}


def generate_cards(db: Database, topic: dict[str, Any], n: int = 3) -> list[dict[str, Any]]:
    ctx = retrieve(db, topic["name"], topic.get("course"), k=4)
    context = "\n\n".join(h["content"][:1200] for h in ctx) or "(no notes found; use standard textbook material)"
    prompt = (
        f"Write {n} practice problems for an engineering student who is weak on: \"{topic['name']}\""
        f"{' in ' + topic['course'] if topic.get('course') else ''}.\n"
        f"{('Student notes on why they struggled: ' + topic['notes']) if topic.get('notes') else ''}\n"
        "Make them exam-style, short enough to do in 5-10 minutes, increasing difficulty. "
        "Each answer should give the final result and the key steps.\n"
        'Return JSON: {"cards": [{"question": str, "answer": str}]}\n\n'
        f"COURSE NOTES:\n{context}"
    )
    reply = llm.chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=1500, temperature=0.5)
    data = llm.extract_json(reply)
    cards = data.get("cards", []) if isinstance(data, dict) else data
    out = []
    for c in cards[:n]:
        if isinstance(c, dict) and c.get("question"):
            cid = add_card(db, topic["id"], str(c["question"]), str(c.get("answer") or ""), source="ai")
            out.append({"id": cid, **c})
    return out
