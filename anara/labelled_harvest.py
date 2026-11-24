"""Harvest the closed questions + expert answers + sources of the Ajrasakha reviewer backend -> data/labelled_db_questions.jsonl.

    AJR_USER=<email> AJR_PASS=<password> .venv/bin/python3 anara/labelled_harvest.py [--limit-ids N]

Incremental: ids already in the jsonl are skipped; the list pages are always re-read (cheap), details only for new ids.
The questions are not in the POP or Atlas Mongo databases, so this calls the reviewer API the web app uses:
Firebase email/password sign-in (the web API key is public in the app's JS bundle) -> POST /questions/detailed (closed,
all states; the endpoint has no state filter, details.state is stored) -> GET /questions/{id}/full for the sources
(data.submission.history[*].answer.sources, repeated under answer.reviews[*].answer.sources) and the answer text.
Credentials come from the environment only; the token stays in memory.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3.util.connection as _conn

_conn.allowed_gai_family = lambda: socket.AF_INET   # this host's IPv6 route hangs; IPv4 works

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

OUT = common.DATA / "labelled_db_questions.jsonl"
SITE = "https://ajrasakha-desk.annam.ai/"
API = "https://reviewer-backend-239934307367.asia-south2.run.app/api"
LIST_QS = ("status=closed&source=AJRASAKHA&crop=all&normalised_crop=all&priority=all&domain=all&user=all&assignedUser=all"
           "&review_level=all&closedInTwoHrs=false&consecutiveApprovals=all&autoAllocateFilter=all"
           "&autoAllocateModeratorFilter=all&answersCountMin=0&answersCountMax=100&hiddenQuestions=false"
           "&duplicateQuestions=false&isOnHold=false&unallocatedQuestions=false")
PAGE_LIMIT = 100
WORKERS = 5
ANSWER_CHARS = 3000

_tok = {"value": None, "key": None}
_lock = threading.Lock()


def _firebase_key() -> str:
    if not _tok["key"]:
        html = requests.get(SITE, timeout=30).text
        for js in re.findall(r'src="(/assets/[^"]+\.js)"', html):
            m = re.search(r"AIza[0-9A-Za-z_-]{35}", requests.get(SITE.rstrip("/") + js, timeout=60).text)
            if m:
                _tok["key"] = m.group(0)
                break
        else:
            raise RuntimeError("Firebase web API key not found in the app's script bundles")
    return _tok["key"]


def _sign_in() -> None:
    r = requests.post("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
                      params={"key": _firebase_key()}, timeout=30,
                      json={"email": os.environ["AJR_USER"], "password": os.environ["AJR_PASS"], "returnSecureToken": True})
    r.raise_for_status()
    _tok["value"] = r.json()["idToken"]
    requests.post(API + "/auth/sync", headers={"Authorization": "Bearer " + _tok["value"]}, json={}, timeout=60)


def call(method: str, path: str, **kw) -> dict:
    """One API call with retry on 429/5xx/network errors and a fresh sign-in on 401."""
    for attempt in range(8):
        if not _tok["value"]:
            with _lock:
                if not _tok["value"]:
                    _sign_in()
        try:
            r = requests.request(method, API + path, headers={"Authorization": "Bearer " + _tok["value"]}, timeout=90, **kw)
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401:
            with _lock:
                _tok["value"] = None
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"giving up on {method} {path.split('?')[0]}")


def list_closed() -> list[dict]:
    out, page, pages = [], 1, 1
    while page <= pages:
        j = call("POST", f"/questions/detailed?page={page}&limit={PAGE_LIMIT}&{LIST_QS}", json={})
        pages = j["totalPages"]
        out += j["questions"]
        print(f"list page {page}/{pages}", flush=True)
        page += 1
    return out


def _answers(hist: list[dict]):
    for h in hist:
        a = h.get("answer") or {}
        yield h, a
        for rv in a.get("reviews") or []:
            yield rv, rv.get("answer") or {}


def record(q: dict, full: dict) -> dict:
    d = q.get("details") or {}
    hist = ((full.get("submission") or {}).get("history")) or []
    sources, seen = [], set()
    for _, a in _answers(hist):
        for s in a.get("sources") or []:
            if not isinstance(s, dict):
                continue
            key = (s.get("source"), str(s.get("page")))
            if key in seen or not s.get("source"):
                continue
            seen.add(key)
            sources.append({"scope": s.get("sourceType"), "name": s.get("sourceName"), "link": s.get("source"),
                            "pages": str(s.get("page") or "")})
    answers = [h["answer"] for h in hist if isinstance(h.get("answer"), dict) and isinstance(h["answer"].get("answer"), str)]
    finals = [a for a in answers if a.get("isFinalAnswer")]
    text = (finals or answers or [{}])[-1].get("answer") or ""
    if isinstance(full.get("closedFinalAnswer"), str) and full["closedFinalAnswer"]:
        text = full["closedFinalAnswer"]
    domain = d.get("domain")
    return {"question_id": q["_id"], "question": q.get("question"), "answer": (text or "")[:ANSWER_CHARS],
            "state": d.get("state"), "district": d.get("district"), "crop": d.get("crop"), "season": d.get("season"),
            "domain": ", ".join(domain) if isinstance(domain, list) else domain, "source_channel": q.get("source"),
            "created_at": q.get("createdAt"), "closed_at": q.get("closedAt"), "sources": sources}


def main() -> None:
    cap = int(sys.argv[sys.argv.index("--limit-ids") + 1]) if "--limit-ids" in sys.argv else None
    done = set()
    if OUT.exists():
        done = {json.loads(l)["question_id"] for l in OUT.read_text().splitlines() if l.strip()}
    todo = [q for q in list_closed() if q["_id"] not in done]
    if cap:
        todo = todo[:cap]
    print(f"{len(done)} already harvested, {len(todo)} to fetch", flush=True)
    wl, n = threading.Lock(), 0

    def work(q):
        nonlocal n
        full = call("GET", f"/questions/{q['_id']}/full")["data"]
        line = json.dumps(record(q, full), ensure_ascii=False)
        with wl:
            with OUT.open("a") as f:
                f.write(line + "\n")
            n += 1
            if n % 100 == 0:
                print(f"{n}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, todo))
    print("done", n, "new ->", OUT)


if __name__ == "__main__":
    main()
