"""Labelled test of the search modes against the closed questions of the Ajrasakha reviewer DB.

The questions come from data/labelled_db_questions.jsonl (written by labelled_harvest.py: question, expert answer, state,
and the sources the expert cited as `{scope, name, link, pages}`). Only when that file is missing the two Excel exports
data/questions_*_Punjab_closed.xlsx are read instead (fallback; their `Sources` column has one
`<name>: <WorkDrive link> (p.8,9)` per line). A run picks a random share of the questions of one state (default Punjab,
status closed), runs each through one search mode in-process, and scores where the labelled documents / pages came out.

    .venv/bin/python3 anara/labelled_test.py bm25nearcov 10     # quick command-line run, 10% of the questions

Source documents are grouped by their STATES: a document of the sheet is in the state(s) of its sheet rows ("State Punjab"
and "Punjab" are both Punjab), a Central-scope document is in "Central". A run's `states` option limits which source-document
states count (default: all); a source whose document is in none of the chosen states is excluded as "outside chosen states".

Score of a question (headline = its BEST source; it counts as answered if ANY labelled source is found):
    rank           1-based position of the source's document in the mode's full ranked list (None: not returned)
    page_hit       any of the document's top-5 returned pages is a labelled page (None: no pages / not returned)
    page_distance  smallest |returned page - labelled page| over the top-5 pages (0 = exact; None as above)
Only eligible (question, document) pairs are scored (in the index, English text, inside the chosen states; see ELIGIBLE_RULE);
the others are listed as "excluded" with the reason and are in no metric.
Finished runs are kept in data/test_runs/<run_id>.json.
"""
from __future__ import annotations

import functools
import glob
import json
import random
import re
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import modes  # noqa: E402

MODES = modes.KEYS
EXCLUDE_REASONS = ["not in index", "no extractable text", "not English", "outside chosen states",
                   "mode searches state documents only"]
LIMIT = 3000        # documents asked of the ranking functions (a mode's whole list)
RANK_CAP = 50        # a source document ranked below this (or not returned) is scored as RANK_CAP + 1
TOP_PAGES = 5       # returned pages per document that are scored
RUNS = common.DATA / "test_runs"
QUESTION_FILE = common.DATA / "labelled_db_questions.jsonl"
QUESTION_GLOB = str(common.DATA / "questions_*_Punjab_closed.xlsx")   # fallback when QUESTION_FILE is missing
DEFAULT_QUESTION_STATE = "Punjab"
ALL_STATES = "All"      # question_state value: every state's questions

_LINK = re.compile(r":\s*(https?://\S+)")
_PAGES = re.compile(r"\(\s*pp?\.?\s*([0-9,\s\-\u2013]*)\)\s*,?\s*$", re.I)


def parse_pages(text: str) -> list[int]:
    """Sorted page numbers of "8,9" / "27-29" / "8, 12-13" (anything else is skipped)."""
    pages: list[int] = []
    for part in re.split(r"[,\s;]+", str(text or "").strip()):
        r = re.fullmatch(r"(\d+)\s*[-\u2013]\s*(\d+)", part)
        if r and int(r.group(1)) <= int(r.group(2)) <= int(r.group(1)) + 200:
            pages += range(int(r.group(1)), int(r.group(2)) + 1)
        elif part.isdigit():
            pages.append(int(part))
    return sorted(set(pages))


def parse_source(line: str) -> dict | None:
    """{name, link, pages} of one Sources line, or None. The LAST ': http' splits name from link (a name may hold colons)."""
    found = list(_LINK.finditer(line))
    if not found:
        return None
    m = found[-1]
    rest = line[m.start(1):]
    pm = _PAGES.search(rest)
    pages: list[int] = []
    if pm:
        pages = parse_pages(pm.group(1))
        rest = rest[:pm.start()]
    return {"name": line[:m.start()].strip(), "link": rest.split()[0].strip(" ,"), "pages": pages}


_cache: dict = {}


def _resolve(s: dict) -> dict:
    s["doc_id"] = common.doc_for_link(s["link"])
    return s


def _load_db(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        sources = [_resolve({"name": (x.get("name") or "").strip(), "link": x["link"], "pages": parse_pages(x.get("pages"))})
                   for x in r.get("sources") or [] if x.get("link")]
        out.append({"id": str(r["question_id"]), "question": (r.get("question") or "").strip(), "crop": r.get("crop") or None,
                    "channel": r.get("source_channel") or None, "state": r.get("state") or "(none)", "status": "closed",
                    "sources": sources})
    return out


def _load_excel(files: list[str]) -> list[dict]:
    seen, out = set(), []
    for f in files:
        for r in pd.read_excel(f).to_dict("records"):
            qid = r.get("Question ID")
            if pd.isna(qid) or qid in seen:
                continue
            seen.add(qid)
            sources = []
            for line in str(r.get("Sources") if not pd.isna(r.get("Sources")) else "").splitlines():
                if line.strip() and (s := parse_source(line.strip())):
                    sources.append(_resolve(s))
            clean = lambda k: None if pd.isna(r.get(k)) else str(r.get(k)).strip()
            out.append({"id": str(qid), "question": clean("Question") or "", "crop": clean("Crop"),
                        "channel": clean("Source"), "state": clean("State") or DEFAULT_QUESTION_STATE, "status": "closed",
                        "sources": sources})
    return out


def load_questions() -> dict:
    """{"questions": [...], "files": [...], "origin": "db"|"excel"}: every closed question of the harvested jsonl (all
    states; each has "state"), or, if that file is missing, of the Excel exports. Sources are parsed and resolved to
    documents (cached until a file changes)."""
    if QUESTION_FILE.exists():
        files, origin = [str(QUESTION_FILE)], "db"
    else:
        files, origin = sorted(glob.glob(QUESTION_GLOB)), "excel"
    stamp = [(f, Path(f).stat().st_mtime) for f in files]
    if _cache.get("stamp") == stamp:
        return _cache["data"]
    out = _load_db(QUESTION_FILE) if origin == "db" else _load_excel(files)
    data = {"questions": sorted(out, key=lambda q: q["id"]), "files": [Path(f).name for f in files], "origin": origin}
    _cache.update(stamp=stamp, data=data)
    return data


def question_options() -> dict:
    """What the test panel can choose: {"question_states": [{state, questions}], "doc_states": [...], "default_state": ...}.
    The counts are the questions with an eligible source (indexed, English text, labelled pages; any source state)."""
    return {"question_states": random_states(), "statuses": ["closed"], "doc_states": doc_state_names(),
            "default_state": DEFAULT_QUESTION_STATE, "origin": load_questions()["origin"]}


@functools.lru_cache(maxsize=1)
def _doc_states() -> dict[str, tuple[str, ...]]:
    """{doc_id: states of the document}: the sheet's state names ("State Punjab" -> "Punjab"), plus "Central" for every
    Central-scope document (data/central_docs.csv)."""
    out = {}
    central = common.central_ids()
    for d, sts in common.load_documents().drop_duplicates("doc_id")[["doc_id", "states"]].itertuples(index=False):
        names = {re.sub(r"^State\s+", "", str(x)).strip() for x in sts if str(x) != "Central"}
        if d in central or "Central" in sts:
            names.add("Central")
        out[d] = tuple(sorted(names))
    return out


def doc_state_names() -> list[str]:
    return sorted({x for v in _doc_states().values() for x in v})


def _top_pages(r: dict, mode: str) -> list[int]:
    if mode == "zone":    # `pages` is the whole stretch in page order: take the highest-scoring ones
        pg = r["page_groups"][0] if r.get("page_groups") else []
        return [p["page"] for p in sorted(pg, key=lambda p: (-p["score"], p["page"]))][:TOP_PAGES]
    return [int(p) for p in r["pages"][:TOP_PAGES]]      # every other mode lists its pages best first


def _search_scope(states: tuple[str, ...]) -> str:
    """The all/state/central scope the ranking functions understand, for the chosen document states."""
    if set(states) == set(doc_state_names()):
        return "all"
    if set(states) == {"Central"}:
        return "central"
    return "state" if "Central" not in states else "all"


def ranked(mode: str, query: str, states: tuple[str, ...]) -> list[dict]:
    """The mode's full ranked document list (rows with doc_id and pages), limited to documents of the chosen `states`."""
    import proximity as px
    scope = _search_scope(states)
    if mode == "zone":
        from zonesearch import search_zone
        res = search_zone(query, scope=scope)
    elif mode in ("proximity", "bm25sum", "bm25near", "bm25nearmax", "bm25nearscaled", "bm25nearown", "bm25nearcov", "bm25nearcov2"):
        fn = px.search_proximity if mode == "proximity" else getattr(px, "search_" + mode)
        res = fn(query, scope=scope, limit=LIMIT)
    else:
        if mode == "restrict":
            from hybrid import search_restrict
            res = search_restrict(query)
        elif mode == "hybrid":
            from hybrid import search_hybrid
            res = search_hybrid(query)
        else:
            from search import search
            res = search(query)
        res = [r for r in res if common.in_scope(r["doc_id"], scope)]
    if scope != "all" or len(states) != len(doc_state_names()):
        res = [r for r in res if set(_doc_states().get(r["doc_id"], ())) & set(states)]
    return res


# ---------------------------------------------------------------- eligibility

NO_TEXT = ("scanned_no_text", "mixed", "not_pdf")   # text_report.csv verdicts; "unreadable..." counts too
ELIGIBLE_RULE = ("A (question, document) pair is scored only if the document is in the keyword index (all indexed documents "
                 "have extractable text), its text was found to be English by the text check (data/text_report.csv, "
                 "verdict english_text; the catalogued language column is shown but not used, since catalogued-Hindi/"
                 "Punjabi PDFs whose text is English are counted), one of its states is among the chosen states, and the mode can "
                 "search it. Other pairs are excluded from every metric and listed on the Excluded sheet. A question with "
                 "no eligible source is not scored and is not a miss.")


@functools.lru_cache(maxsize=1)
def _eligibility_data() -> dict:
    rep = pd.read_csv(common.DATA / "text_report.csv", keep_default_na=False).drop_duplicates("doc_id").set_index("doc_id")
    docs = common.load_documents().drop_duplicates("doc_id").set_index("doc_id")
    index = set(json.loads((common.DATA / "prox_meta.json").read_text())["docs"])
    return {"verdict": rep["verdict"].to_dict(), "lang": rep["lang"].to_dict(), "catalogued": docs["language"].to_dict(),
            "index": index}


def classify(source: dict, mode: str, states: tuple[str, ...]) -> dict:
    """{"states": [state names of the document]|None, "reason": None (eligible) or why excluded, "detail": text} for one
    labelled source. Reasons: not in index / no extractable text / not English (mode independent), then outside chosen
    states / mode searches state documents only."""
    e, d = _eligibility_data(), source["doc_id"]
    if not d:
        return {"states": None, "reason": "not in index", "detail": "the link matches no document of the document list"}
    sc = list(_doc_states().get(d, ()))
    v, cat = e["verdict"].get(d), e["catalogued"].get(d, "?")
    if d not in e["index"]:
        if v == "non_english":
            return {"states": sc, "reason": "not English", "detail": f"text check: {e['lang'].get(d)} (catalogued {cat})"}
        if v and v != "english_text":
            return {"states": sc, "reason": "no extractable text", "detail": f"text check: {str(v)[:20]} (catalogued {cat})"}
        return {"states": sc, "reason": "not in index", "detail": "document is catalogued but not in the keyword index"}
    if not set(sc) & set(states):
        return {"states": sc, "reason": "outside chosen states",
                "detail": f"document is in {', '.join(sc) or 'no state'}; chosen: {', '.join(states)}"}
    if d in common.central_ids() and modes.BY_KEY[mode]["needs_embeddings"]:
        return {"states": sc, "reason": "mode searches state documents only", "detail": "central documents have no embeddings"}
    return {"states": sc, "reason": None, "detail": f"catalogued {cat}"}


def score_question(q: dict, mode: str, states: tuple[str, ...]) -> dict:
    """One question's row: per-source numbers, best-source aggregates (see the module docstring). Sources are
    classified first; only eligible ones are scored, the others are kept in row["excluded"]."""
    cls = [(s, classify(s, mode, states)) for s in q["sources"]]
    scored = [(s, c) for s, c in cls if c["reason"] is None]
    # the question's states are those of its in-index English sources inside the chosen states, whatever the mode can search,
    # so that a mode which cannot see central documents still puts the question under "Central" (state-only rows then compare across modes)
    qstates = sorted({x for _, c in cls if c["reason"] in (None, "mode searches state documents only") for x in c["states"]
                      if x in states})
    row = {"id": q["id"], "question": q["question"], "crop": q["crop"], "channel": q["channel"], "question_state": q.get("state"),
           "n_sources": len(q["sources"]), "n_eligible": len(scored), "n_excluded": len(cls) - len(scored),
           "states": qstates,
           "n_returned": None, "seconds": 0.0, "sources": [], "best_rank": None, "best_page_distance": None,
           "any_page_hit": None, "best_pages_returned": [], "best_pages_labelled": [],
           "excluded": [{"question_id": q["id"], "question": q["question"], "name": s["name"], "link": s["link"],
                         "doc_id": s["doc_id"], "states": c["states"], "reason": c["reason"], "detail": c["detail"],
                         "pages_labelled": s["pages"]} for s, c in cls if c["reason"]]}
    if not scored:
        return row
    t = time.time()
    res = ranked(mode, q["question"], states)
    row["seconds"], row["n_returned"] = round(time.time() - t, 3), len(res)
    pos = {}
    for i, r in enumerate(res, 1):
        pos.setdefault(r["doc_id"], (i, r))
    for s, c in scored:
        i, r = pos.get(s["doc_id"], (None, None))
        ret = _top_pages(r, mode) if r else []
        if i is None or i > RANK_CAP:       # indexed (eligible) but outside the top 50, or not returned: rank 51 whatever the real rank
            i = RANK_CAP + 1
        dist = min((abs(a - b) for a in ret for b in s["pages"]), default=None)
        row["sources"].append({"name": s["name"], "link": s["link"], "doc_id": s["doc_id"], "states": c["states"], "rank": i,
                               "page_hit": None if dist is None else dist == 0, "page_distance": dist,
                               "pages_returned": ret, "pages_labelled": s["pages"]})
    ranks = [s["rank"] for s in row["sources"] if s["rank"]]
    dists = [s["page_distance"] for s in row["sources"] if s["page_distance"] is not None]
    row["best_rank"] = min(ranks) if ranks else None
    row["best_page_distance"] = min(dists) if dists else None
    row["any_page_hit"] = (any(s["page_hit"] for s in row["sources"]) if dists else None)
    best = min((s for s in row["sources"] if s["rank"]), key=lambda s: s["rank"], default=None)
    if best:
        row["best_pages_returned"], row["best_pages_labelled"] = best["pages_returned"], best["pages_labelled"]
    return row


def _random_pool() -> list[tuple[dict, list[dict]]]:
    """[(question, eligible sources)] for every labelled question with at least one source that is in the keyword index
    (English text), has labelled pages, and a document state (`classify` with every state chosen). Each eligible source
    carries its document states."""
    all_states = tuple(doc_state_names())
    out = []
    for q in load_questions()["questions"]:
        ok = []
        for s in q["sources"]:
            c = classify(s, "bm25nearcov", all_states)
            if c["reason"] is None and s["pages"]:
                ok.append({"name": s["name"], "link": s["link"], "doc_id": s["doc_id"], "pages": sorted(s["pages"]),
                           "states": c["states"]})
        if ok:
            out.append((q, ok))
    return out


def random_states() -> list[dict]:
    """[{state, questions}]: "All" first, then every question state with the number of its labelled questions that have an
    eligible source (indexed, English text, labelled pages; the source may be in any state or Central)."""
    pool = _random_pool()
    count: dict[str, int] = {}
    for q, _ in pool:
        count[q["state"]] = count.get(q["state"], 0) + 1
    return [{"state": ALL_STATES, "questions": len(pool)}] + \
           [{"state": k, "questions": v} for k, v in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))]


def random_question(state: str = ALL_STATES, seed: int | None = None) -> dict:
    """One random labelled question of question-state `state` ("All": any) that has an eligible source (in the index,
    English text, labelled pages), the source's own state or Central not mattering. Returns {"found": bool, "question",
    "id", "question_state", "crop", "state", "pool", "sources": [eligible sources: name, link, doc_id, pages, states]}."""
    pool = [(q, ok) for q, ok in _random_pool() if state == ALL_STATES or q["state"] == state]
    if not pool:
        return {"found": False, "state": state, "pool": 0}
    q, ok = random.Random(seed).choice(pool)
    return {"found": True, "question": q["question"], "id": q["id"], "question_state": q.get("state"), "crop": q["crop"],
            "state": state, "pool": len(pool), "sources": ok}


def _norm_question(text: str) -> str:
    return re.sub(r"[\s?.!]+$", "", re.sub(r"\s+", " ", text or "").strip().lower())


def known_question(text: str, mode: str, states: tuple[str, ...] | None = None) -> dict:
    """The labelled questions whose text equals `text` (ignoring case, spacing and a trailing ?/./!), with their sources
    merged: {"matches": n, "question_ids": [...], "sources": [{name, link, doc_id, pages, states, searchable, reason}]}.
    `searchable` is False when the source cannot be retrieved by `mode` inside `states` (default every state; see classify); `reason` says why. Used by the
    search page so that a question that is in the collection is checked against its own sources and pages."""
    key = _norm_question(text)
    hits = [q for q in load_questions()["questions"] if _norm_question(q["question"]) == key] if key else []
    all_states = tuple(states) if states else tuple(doc_state_names())     # a source outside these is "outside chosen states", not a miss
    sources, seen = [], {}
    for q in hits:
        for s in q["sources"]:
            k = s["doc_id"] or s["link"]
            if k in seen:
                seen[k]["pages"] = sorted(set(seen[k]["pages"]) | set(s["pages"]))
                continue
            c = classify(s, mode, all_states)
            seen[k] = {"name": s["name"], "link": s["link"], "doc_id": s["doc_id"], "pages": sorted(s["pages"]),
                       "states": c["states"], "searchable": c["reason"] is None, "reason": c["reason"], "detail": c["detail"]}
            sources.append(seen[k])
    return {"matches": len(hits), "question_ids": [q["id"] for q in hits], "sources": sources,
            "answer_state": hits[0]["state"] if hits else None}

def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def stats(rows: list[dict]) -> dict:
    """Headline numbers over question rows. Only questions with an eligible source are scored; one not returned by
    the mode counts as a miss in every rate and is left out of the means/medians."""
    sc = [r for r in rows if r["n_eligible"]]
    ranks = [r["best_rank"] for r in sc if r["best_rank"]]
    pg = [r for r in sc if any(s["pages_labelled"] for s in r["sources"])]    # questions with labelled pages to compare
    dist = [r["best_page_distance"] for r in pg if r["best_page_distance"] is not None]
    out = {"questions": len(rows), "scored": len(sc), "no_eligible_source": len(rows) - len(sc),
           "pairs": sum(r["n_eligible"] for r in rows),
           "returned": sum(1 for x in ranks if x <= RANK_CAP), "not_returned": len(sc) - sum(1 for x in ranks if x <= RANK_CAP),
           "rank_mean": round(statistics.mean(ranks), 2) if ranks else None,
           "rank_median": statistics.median(ranks) if ranks else None,
           "page_scored": len(pg), "page_distance_mean": round(statistics.mean(dist), 2) if dist else None}
    for k in (1, 5, 10, 50):
        out[f"top{k}"] = _pct(sum(1 for x in ranks if x <= k), len(sc))
    out["page_hit"] = _pct(sum(1 for r in pg if r["any_page_hit"]), len(pg))
    for k in (0, 1, 2, 5):
        out[f"dist_le_{k}"] = _pct(sum(1 for x in dist if x <= k), len(pg))
    return out


def summarise(rows: list[dict]) -> dict:
    s = stats(rows)
    s["by_state"] = {st: stats([r for r in rows if st in r["states"]]) for st in sorted({x for r in rows for x in r["states"]})}
    s["by_channel"] = {c: stats([r for r in rows if (r["channel"] or "(none)") == c])
                       for c in sorted({r["channel"] or "(none)" for r in rows})}
    count: dict[str, int] = {}
    for r in rows:
        if r["n_eligible"]:
            count[r["crop"] or "(none)"] = count.get(r["crop"] or "(none)", 0) + 1
    top = [c for c, _ in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    by = {c: stats([r for r in rows if (r["crop"] or "(none)") == c]) for c in top}
    rest = [r for r in rows if (r["crop"] or "(none)") not in top]
    if rest:
        by["other"] = stats(rest)
    s["by_crop"] = by
    return s


def eligibility(rows: list[dict]) -> dict:
    """Pair counts: how many (question, document) pairs there were, how many are scored (by document state), how many
    were excluded and why."""
    excl = [x for r in rows for x in r["excluded"]]
    ok = [x for r in rows for x in r["sources"]]
    return {"rule": ELIGIBLE_RULE, "pairs_total": len(ok) + len(excl), "pairs_eligible": len(ok),
            "pairs_eligible_by_state": {st: sum(1 for x in ok if st in x["states"])
                                        for st in sorted({y for x in ok for y in x["states"]})},
            "pairs_excluded": len(excl),
            "excluded_by_reason": {k: sum(1 for x in excl if x["reason"] == k) for k in EXCLUDE_REASONS},
            "questions_total": len(rows), "questions_scored": sum(1 for r in rows if r["n_eligible"]),
            "questions_not_scored": sum(1 for r in rows if not r["n_eligible"])}


# ---------------------------------------------------------------- background runs

_lock = threading.Lock()
_state: dict = {"status": "idle"}
_cancel = threading.Event()


def status() -> dict:
    with _lock:
        return dict(_state)


def start(mode: str, percent: float, states: list[str] | None = None, seed: int | None = None,
          question_state: str = DEFAULT_QUESTION_STATE, status: str = "closed") -> dict:
    """Start a run in the background. `states`: source-document states that count (None: all); `question_state` /
    `status` choose which questions are sampled (percent of those)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    names = doc_state_names()
    chosen = tuple(sorted(set(states))) if states else tuple(names)
    bad = [x for x in chosen if x not in names]
    if bad:
        raise ValueError(f"unknown document state(s) {bad}")
    if status != "closed":
        raise ValueError("only closed questions exist")
    if not 1 <= percent <= 100:
        raise ValueError("percent must be between 1 and 100")
    with _lock:
        if _state.get("status") == "running":
            raise RuntimeError("a test run is already in progress")
        data = load_questions()
        of_state = [q for q in data["questions"] if question_state == ALL_STATES or q["state"] == question_state]
        # only questions with at least one source that is in the index, English-text, page-mapped and searchable by this mode inside the
        # chosen states are sampled; the others (no usable source) are neither scored nor counted as misses
        qs = [q for q in of_state if any(s["pages"] and classify(s, mode, chosen)["reason"] is None for s in q["sources"])]
        if not qs:
            raise ValueError(f"no {status} questions of state {question_state!r} with a source in the index")
        seed = random.randrange(2 ** 31) if seed is None else int(seed)
        k = max(1, round(len(qs) * percent / 100))
        picked = random.Random(seed).sample(qs, k)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{mode}"
        settings = {"mode": mode, "percent": percent, "seed": seed, "states": list(chosen),
                    "states_label": "all" if len(chosen) == len(names) else ", ".join(chosen),
                    "question_state": question_state, "question_status": status, "questions_available": len(qs),
                    "questions_of_state": len(of_state), "questions_without_indexed_source": len(of_state) - len(qs),
                    "questions_picked": k, "files": data["files"], "origin": data["origin"]}
        _cancel.clear()
        _state.clear()
        _state.update(status="running", run_id=run_id, done=0, total=k, current=None, settings=settings,
                      started=time.time())
    threading.Thread(target=_work, args=(run_id, settings, picked), daemon=True).start()
    with _lock:     # (the `status` argument shadows the status() function here)
        return dict(_state)


def cancel() -> dict:
    _cancel.set()
    return status()



def _work(run_id: str, settings: dict, picked: list[dict]) -> None:
    t0, rows = time.time(), []
    try:
        for q in picked:
            if _cancel.is_set():
                break
            with _lock:
                _state["current"] = q["question"][:140]
            rows.append(score_question(q, settings["mode"], tuple(settings["states"])))
            with _lock:
                _state["done"] = len(rows)
        if _cancel.is_set():
            with _lock:
                _state["status"] = "cancelled"
            return
        searched = [r for r in rows if r["n_eligible"]]
        seconds = round(time.time() - t0, 1)
        m = modes.BY_KEY[settings["mode"]]
        report = {"run_id": run_id, "created": datetime.now().isoformat(timespec="seconds"), "settings": settings,
                  "mode": {"key": m["key"], "number": m["number"], "name": m["name"], "description": m["description"],
                           "config": m["config"], "built_on": m["built_on"]},
                  "seconds": seconds,
                  "seconds_per_searched_question": round(sum(r["seconds"] for r in searched) / len(searched), 3) if searched else None,
                  "eligibility": eligibility(rows), "summary": summarise(rows), "questions": rows,
                  "excluded": [x for r in rows for x in r["excluded"]]}
        RUNS.mkdir(parents=True, exist_ok=True)
        (RUNS / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False))
        with _lock:
            _state["status"] = "done"
    except Exception as e:  # noqa: BLE001  (shown to the user in the progress panel)
        with _lock:
            _state.update(status="error", error=f"{type(e).__name__}: {e}")


def _normalise(r: dict) -> dict:
    """Runs saved before the eligibility filter (no `eligibility` key) get the new field names with what they have:
    resolved sources count as eligible, unresolved ones as excluded "not in index"; `old_run` is True and the
    scope split is absent. Runs with the filter pass through unchanged (`old_run` False)."""
    if "eligibility" in r:
        r["old_run"] = False
        return r
    r["old_run"] = True
    s = r["summary"]
    s.setdefault("no_eligible_source", s.get("no_resolvable_source"))
    s.setdefault("pairs", r.get("sources_resolved"))
    for q in r["questions"]:
        q.setdefault("n_eligible", q.get("n_resolved", 0))
        q.setdefault("scope", None)
    r["excluded"] = [{"question_id": x["question_id"], "question": "", "name": x["name"], "link": x["link"], "doc_id": None,
                      "scope": None, "reason": "not in index", "detail": "old run, before the eligibility filter",
                      "pages_labelled": x["pages_labelled"]} for x in r.get("not_indexed", [])]
    m = modes.BY_KEY[r["settings"]["mode"]]
    r["mode"] = {"key": m["key"], "number": m["number"], "name": m["name"], "description": m["description"],
                 "config": m["config"], "built_on": m["built_on"]}
    return r


def list_runs() -> list[dict]:
    out = []
    for f in sorted(RUNS.glob("*.json"), reverse=True) if RUNS.exists() else []:
        r = _normalise(json.loads(f.read_text()))
        s, st, bs = r["summary"], r["settings"], r["summary"].get("by_state") or {}
        out.append({"run_id": r["run_id"], "created": r["created"], **{k: st[k] for k in
                    ("mode", "percent", "seed", "questions_picked")},
                    "states": st.get("states_label", "all"), "question_state": st.get("question_state", "Punjab"),
                    "number": r["mode"]["number"], "name": r["mode"]["name"], "old_run": r["old_run"],
                    "scored": s["scored"], "top1": s["top1"], "top5": s["top5"], "top10": s["top10"], "top50": s["top50"],
                    "rank_median": s["rank_median"], "page_distance_mean": s["page_distance_mean"], "seconds": r["seconds"],
                    "by_state": {k: {"scored": v["scored"], "top10": v["top10"]} for k, v in bs.items()},
                    "pairs_excluded": r.get("eligibility", {}).get("pairs_excluded")})
    return out


def load_run(run_id: str) -> dict:
    if not re.fullmatch(r"[\w\-]+", run_id) or not (RUNS / f"{run_id}.json").exists():
        raise FileNotFoundError(run_id)
    return _normalise(json.loads((RUNS / f"{run_id}.json").read_text()))


def to_xlsx(rep: dict, path) -> None:
    """Workbook with the sheets Summary, Questions, Sources, Excluded."""
    st, s, el, mo = rep["settings"], rep["summary"], rep.get("eligibility"), rep["mode"]
    info = [("Run", rep["run_id"]), ("Created", rep["created"]), ("Mode", f"#{mo['number']} {mo['name']} ({mo['key']})"),
            ("Mode description", mo["description"])]
    info += [(f"Mode config: {k}", v) for k, v in mo["config"]]
    info += [("Question state", st.get("question_state")), ("Question status", st.get("question_status")),
             ("Source-document states counted", st.get("states_label", "all")),
             ("Percent of questions", st["percent"]), ("Seed", st["seed"]),
             ("Questions available", st["questions_available"]), ("Questions picked", st["questions_picked"]),
             ("Files", ", ".join(st["files"])), ("Runtime (s)", rep["seconds"]),
             ("Seconds per searched question", rep["seconds_per_searched_question"]), ("", "")]
    if el:
        info += [("Eligibility rule", el["rule"]), ("Pairs (question, document), all", el["pairs_total"]),
                 ("Pairs eligible (scored)", el["pairs_eligible"]), ("Pairs excluded", el["pairs_excluded"])]
        info += [(f"  eligible pairs, documents of {k}", v) for k, v in el["pairs_eligible_by_state"].items()]
        info += [(f"  excluded: {k}", v) for k, v in el["excluded_by_reason"].items()]
    else:
        info += [("Eligibility rule", "OLD RUN, before the eligibility filter: sources that resolved to an indexed document "
                  "were scored whatever their language or text; the per-state split is not available.")]
    info += [("", ""), ("Questions with no eligible source (not scored, not a miss)", s["no_eligible_source"]),
             ("Questions scored", s["scored"]), ("Scored, document returned", s["returned"]),
             ("Scored, document NOT returned", s["not_returned"]),
             ("Mean best rank (returned only)", s["rank_mean"]), ("Median best rank (returned only)", s["rank_median"]),
             ("Top-1 %", s["top1"]), ("Top-5 %", s["top5"]), ("Top-10 %", s["top10"]), ("Top-50 %", s["top50"]),
             ("Questions with labelled pages", s["page_scored"]), ("Mean page distance", s["page_distance_mean"]),
             ("Page hit in top 5 pages %", s["page_hit"]), ("Page distance <= 0 %", s["dist_le_0"]),
             ("Page distance <= 1 %", s["dist_le_1"]), ("Page distance <= 2 %", s["dist_le_2"]),
             ("Page distance <= 5 %", s["dist_le_5"])]
    cols = ["questions", "scored", "pairs", "returned", "rank_mean", "rank_median", "top1", "top5", "top10", "top50",
            "page_distance_mean", "page_hit", "dist_le_0", "dist_le_1", "dist_le_2", "dist_le_5"]
    cols = [c for c in cols if c in s]
    blocks = [("By state of the source document (a question counts under every state of its sources' documents)", s.get("by_state") or {}),
              ("By channel", s["by_channel"]), ("By crop", s["by_crop"])]
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        pd.DataFrame(info, columns=["Item", "Value"]).to_excel(w, sheet_name="Summary", index=False)
        row = len(info) + 3
        for title, obj in blocks:
            if not obj:
                continue
            pd.DataFrame([{title: k, **{c: v.get(c) for c in cols}} for k, v in obj.items()]).to_excel(
                w, sheet_name="Summary", index=False, startrow=row)
            row += len(obj) + 3
        pd.DataFrame([{"Question ID": r["id"], "Question": r["question"], "Crop": r["crop"], "Channel": r["channel"],
                       "Question state": r.get("question_state"), "Source states": ", ".join(r.get("states") or []), "Sources": r["n_sources"], "Eligible": r["n_eligible"],
                       "Best rank": r["best_rank"], "Best page distance": r["best_page_distance"],
                       "Any page hit": r["any_page_hit"],
                       "Returned pages (best source)": ", ".join(map(str, r["best_pages_returned"])),
                       "Labelled pages (best source)": ", ".join(map(str, r["best_pages_labelled"])),
                       "Seconds": r["seconds"]} for r in rep["questions"] if r["n_eligible"]]).to_excel(w, sheet_name="Questions", index=False)
        pd.DataFrame([{"Question ID": r["id"], "Document": x["name"], "Link": x["link"], "Doc ID": x["doc_id"],
                       "States": ", ".join(x.get("states") or []), "Rank": x["rank"], "Page hit": x["page_hit"], "Page distance": x["page_distance"],
                       "Returned pages": ", ".join(map(str, x["pages_returned"])),
                       "Labelled pages": ", ".join(map(str, x["pages_labelled"]))}
                      for r in rep["questions"] for x in r["sources"]]).to_excel(w, sheet_name="Sources", index=False)
        pd.DataFrame([{"Question ID": x["question_id"], "Question": x["question"], "Document": x["name"], "Link": x["link"],
                       "Doc ID": x["doc_id"], "States": ", ".join(x["states"] or []), "Reason": x["reason"], "Detail": x["detail"],
                       "Labelled pages": ", ".join(map(str, x["pages_labelled"]))} for x in rep["excluded"]],
                     columns=["Question ID", "Question", "Document", "Link", "Doc ID", "States", "Reason", "Detail",
                              "Labelled pages"]).to_excel(w, sheet_name="Excluded", index=False)


if __name__ == "__main__":
    start(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10)
    while status()["status"] == "running":
        time.sleep(2)
        print(status()["done"], "/", status()["total"])
    print(status()["status"], list_runs()[:1])
