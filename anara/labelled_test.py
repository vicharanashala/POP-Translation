"""Labelled test of the search modes against the closed Punjab questions (data/questions_*_Punjab_closed.xlsx).

Each question lists the documents (and pages) an expert used as its answer (column `Sources`, one
`<name>: <WorkDrive link> (p.8,9)` per line). A run picks a random share of the questions, runs each through
one search mode in-process, and scores where the labelled documents / pages came out.

    .venv/bin/python3 anara/labelled_test.py bm25nearcov 10     # quick command-line run, 10% of the questions

Score of a question (headline = its BEST source; it counts as answered if ANY labelled source is found):
    rank           1-based position of the source's document in the mode's full ranked list (None: not returned)
    page_hit       any of the document's top-5 returned pages is a labelled page (None: no pages / not returned)
    page_distance  smallest |returned page - labelled page| over the top-5 pages (0 = exact; None as above)
Only eligible (question, document) pairs are scored (in the index, English text, inside the scope; see ELIGIBLE_RULE);
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
EXCLUDE_REASONS = ["not in index", "no extractable text", "not English", "outside chosen scope",
                   "mode searches state documents only"]
LIMIT = 3000        # documents asked of the ranking functions (a mode's whole list)
TOP_PAGES = 5       # returned pages per document that are scored
RUNS = common.DATA / "test_runs"
QUESTION_GLOB = str(common.DATA / "questions_*_Punjab_closed.xlsx")

_LINK = re.compile(r":\s*(https?://\S+)")
_PAGES = re.compile(r"\(\s*pp?\.?\s*([0-9,\s\-\u2013]*)\)\s*,?\s*$", re.I)


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
        for part in re.split(r"[,\s]+", pm.group(1).strip()):
            r = re.fullmatch(r"(\d+)\s*[-\u2013]\s*(\d+)", part)
            if r and int(r.group(1)) <= int(r.group(2)) <= int(r.group(1)) + 200:
                pages += range(int(r.group(1)), int(r.group(2)) + 1)
            elif part.isdigit():
                pages.append(int(part))
        rest = rest[:pm.start()]
    return {"name": line[:m.start()].strip(), "link": rest.split()[0].strip(" ,"), "pages": sorted(set(pages))}


_cache: dict = {}


def load_questions() -> dict:
    """{"questions": [...], "files": [...]}: every matching file, de-duplicated by Question ID, sources parsed and
    resolved (cached until a file changes)."""
    files = sorted(glob.glob(QUESTION_GLOB))
    stamp = [(f, Path(f).stat().st_mtime) for f in files]
    if _cache.get("stamp") == stamp:
        return _cache["data"]
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
                    s["doc_id"] = common.doc_for_link(s["link"])
                    sources.append(s)
            clean = lambda k: None if pd.isna(r.get(k)) else str(r.get(k)).strip()
            out.append({"id": str(qid), "question": clean("Question") or "", "crop": clean("Crop"),
                        "channel": clean("Source"), "sources": sources})
    data = {"questions": sorted(out, key=lambda q: q["id"]), "files": [Path(f).name for f in files]}
    _cache.update(stamp=stamp, data=data)
    return data


def _top_pages(r: dict, mode: str) -> list[int]:
    if mode == "zone":    # `pages` is the whole stretch in page order: take the highest-scoring ones
        pg = r["page_groups"][0] if r.get("page_groups") else []
        return [p["page"] for p in sorted(pg, key=lambda p: (-p["score"], p["page"]))][:TOP_PAGES]
    return [int(p) for p in r["pages"][:TOP_PAGES]]      # every other mode lists its pages best first


def ranked(mode: str, query: str, scope: str) -> list[dict]:
    """The mode's full ranked document list (rows with doc_id and pages), already limited to `scope`."""
    import proximity as px
    if mode == "zone":
        from zonesearch import search_zone
        return search_zone(query, scope=scope)
    if mode in ("proximity", "bm25sum", "bm25near", "bm25nearmax", "bm25nearscaled", "bm25nearown", "bm25nearcov"):
        fn = px.search_proximity if mode == "proximity" else getattr(px, "search_" + mode)
        return fn(query, scope=scope, limit=LIMIT)
    if mode == "restrict":
        from hybrid import search_restrict
        res = search_restrict(query)
    elif mode == "hybrid":
        from hybrid import search_hybrid
        res = search_hybrid(query)
    else:
        from search import search
        res = search(query)
    return [r for r in res if common.in_scope(r["doc_id"], scope)]


# ---------------------------------------------------------------- eligibility

NO_TEXT = ("scanned_no_text", "mixed", "not_pdf")   # text_report.csv verdicts; "unreadable..." counts too
ELIGIBLE_RULE = ("A (question, document) pair is scored only if the document is in the keyword index (all indexed documents "
                 "have extractable text), its text was found to be English by the text check (data/text_report.csv, "
                 "verdict english_text; the catalogued language column is shown but not used, since catalogued-Hindi/"
                 "Punjabi PDFs whose text is English are counted), it lies inside the chosen scope, and the mode can "
                 "search it. Other pairs are excluded from every metric and listed on the Excluded sheet. A question with "
                 "no eligible source is not scored and is not a miss.")


@functools.lru_cache(maxsize=1)
def _eligibility_data() -> dict:
    rep = pd.read_csv(common.DATA / "text_report.csv", keep_default_na=False).drop_duplicates("doc_id").set_index("doc_id")
    docs = common.load_documents().drop_duplicates("doc_id").set_index("doc_id")
    index = set(json.loads((common.DATA / "prox_meta.json").read_text())["docs"])
    return {"verdict": rep["verdict"].to_dict(), "lang": rep["lang"].to_dict(), "catalogued": docs["language"].to_dict(),
            "index": index}


def classify(source: dict, mode: str, scope: str) -> dict:
    """{"scope": state|central|None, "reason": None (eligible) or why excluded, "detail": text} for one labelled source.
    Reasons: not in index / no extractable text / not English (mode independent), then outside chosen scope /
    mode searches state documents only."""
    e, d = _eligibility_data(), source["doc_id"]
    if not d:
        return {"scope": None, "reason": "not in index", "detail": "the link matches no document of the document list"}
    sc = "central" if d in common.central_ids() else "state"
    v, cat = e["verdict"].get(d), e["catalogued"].get(d, "?")
    if d not in e["index"]:
        if v == "non_english":
            return {"scope": sc, "reason": "not English", "detail": f"text check: {e['lang'].get(d)} (catalogued {cat})"}
        if v and v != "english_text":
            return {"scope": sc, "reason": "no extractable text", "detail": f"text check: {str(v)[:20]} (catalogued {cat})"}
        return {"scope": sc, "reason": "not in index", "detail": "document is catalogued but not in the keyword index"}
    if not common.in_scope(d, scope):
        return {"scope": sc, "reason": "outside chosen scope", "detail": f"document is {sc}, run scope is {scope}"}
    if sc == "central" and modes.BY_KEY[mode]["needs_embeddings"]:
        return {"scope": sc, "reason": "mode searches state documents only", "detail": "central documents have no embeddings"}
    return {"scope": sc, "reason": None, "detail": f"catalogued {cat}"}


def score_question(q: dict, mode: str, scope: str) -> dict:
    """One question's row: per-source numbers, best-source aggregates (see the module docstring). Sources are
    classified first; only eligible ones are scored, the others are kept in row["excluded"]."""
    cls = [(s, classify(s, mode, scope)) for s in q["sources"]]
    scored = [(s, c) for s, c in cls if c["reason"] is None]
    # the question's scope is that of its in-index English sources whatever the mode can search, so that a mode which cannot
    # see central documents still puts a state+central question under "both" (state-only rows then compare across modes)
    scopes = {c["scope"] for _, c in cls if c["reason"] in (None, "mode searches state documents only")}
    row = {"id": q["id"], "question": q["question"], "crop": q["crop"], "channel": q["channel"],
           "n_sources": len(q["sources"]), "n_eligible": len(scored), "n_excluded": len(cls) - len(scored),
           "scope": None if not scopes else "both" if len(scopes) > 1 else scopes.pop(),
           "n_returned": None, "seconds": 0.0, "sources": [], "best_rank": None, "best_page_distance": None,
           "any_page_hit": None, "best_pages_returned": [], "best_pages_labelled": [],
           "excluded": [{"question_id": q["id"], "question": q["question"], "name": s["name"], "link": s["link"],
                         "doc_id": s["doc_id"], "scope": c["scope"], "reason": c["reason"], "detail": c["detail"],
                         "pages_labelled": s["pages"]} for s, c in cls if c["reason"]]}
    if not scored:
        return row
    t = time.time()
    res = ranked(mode, q["question"], scope)
    row["seconds"], row["n_returned"] = round(time.time() - t, 3), len(res)
    pos = {}
    for i, r in enumerate(res, 1):
        pos.setdefault(r["doc_id"], (i, r))
    for s, c in scored:
        i, r = pos.get(s["doc_id"], (None, None))
        ret = _top_pages(r, mode) if r else []
        dist = min((abs(a - b) for a in ret for b in s["pages"]), default=None)
        row["sources"].append({"name": s["name"], "link": s["link"], "doc_id": s["doc_id"], "scope": c["scope"], "rank": i,
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
           "returned": len(ranks), "not_returned": len(sc) - len(ranks),
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
    s["by_scope"] = {sc: stats([r for r in rows if r["scope"] == sc]) for sc in ("state", "central", "both")}
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
    """Pair counts: how many (question, document) pairs there were, how many are scored (by document scope), how many
    were excluded and why."""
    excl = [x for r in rows for x in r["excluded"]]
    ok = [x for r in rows for x in r["sources"]]
    return {"rule": ELIGIBLE_RULE, "pairs_total": len(ok) + len(excl), "pairs_eligible": len(ok),
            "pairs_eligible_state": sum(1 for x in ok if x["scope"] == "state"),
            "pairs_eligible_central": sum(1 for x in ok if x["scope"] == "central"),
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


def start(mode: str, percent: float, scope: str = "all", seed: int | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if scope not in ("all", "state", "central"):
        raise ValueError(f"unknown scope {scope!r}")
    if not 1 <= percent <= 100:
        raise ValueError("percent must be between 1 and 100")
    with _lock:
        if _state.get("status") == "running":
            raise RuntimeError("a test run is already in progress")
        data = load_questions()
        seed = random.randrange(2 ** 31) if seed is None else int(seed)
        qs = data["questions"]
        k = max(1, round(len(qs) * percent / 100))
        picked = random.Random(seed).sample(qs, k)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{mode}"
        settings = {"mode": mode, "percent": percent, "seed": seed, "scope": scope, "questions_available": len(qs),
                    "questions_picked": k, "files": data["files"]}
        _cancel.clear()
        _state.clear()
        _state.update(status="running", run_id=run_id, done=0, total=k, current=None, settings=settings,
                      started=time.time())
    threading.Thread(target=_work, args=(run_id, settings, picked), daemon=True).start()
    return status()


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
            rows.append(score_question(q, settings["mode"], settings["scope"]))
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
        s, st, sc = r["summary"], r["settings"], r["summary"].get("by_scope") or {}
        out.append({"run_id": r["run_id"], "created": r["created"], **{k: st[k] for k in
                    ("mode", "percent", "seed", "scope", "questions_picked")},
                    "number": r["mode"]["number"], "name": r["mode"]["name"], "old_run": r["old_run"],
                    "scored": s["scored"], "top1": s["top1"], "top5": s["top5"], "top10": s["top10"], "top50": s["top50"],
                    "rank_median": s["rank_median"], "page_distance_mean": s["page_distance_mean"], "seconds": r["seconds"],
                    "state_scored": sc.get("state", {}).get("scored"), "state_top10": sc.get("state", {}).get("top10"),
                    "central_scored": sc.get("central", {}).get("scored"), "central_top10": sc.get("central", {}).get("top10"),
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
    info += [("Scope", st["scope"]), ("Percent of questions", st["percent"]), ("Seed", st["seed"]),
             ("Questions available", st["questions_available"]), ("Questions picked", st["questions_picked"]),
             ("Files", ", ".join(st["files"])), ("Runtime (s)", rep["seconds"]),
             ("Seconds per searched question", rep["seconds_per_searched_question"]), ("", "")]
    if el:
        info += [("Eligibility rule", el["rule"]), ("Pairs (question, document), all", el["pairs_total"]),
                 ("Pairs eligible (scored)", el["pairs_eligible"]), ("  of which state documents", el["pairs_eligible_state"]),
                 ("  of which central documents", el["pairs_eligible_central"]), ("Pairs excluded", el["pairs_excluded"])]
        info += [(f"  excluded: {k}", v) for k, v in el["excluded_by_reason"].items()]
    else:
        info += [("Eligibility rule", "OLD RUN, before the eligibility filter: sources that resolved to an indexed document "
                  "were scored whatever their language or text; the state/central split is not available.")]
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
    blocks = [("By source scope (state / central / both: a question with sources of both)", s.get("by_scope") or {}),
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
                       "Source scope": r.get("scope"), "Sources": r["n_sources"], "Eligible": r["n_eligible"],
                       "Best rank": r["best_rank"], "Best page distance": r["best_page_distance"],
                       "Any page hit": r["any_page_hit"],
                       "Returned pages (best source)": ", ".join(map(str, r["best_pages_returned"])),
                       "Labelled pages (best source)": ", ".join(map(str, r["best_pages_labelled"])),
                       "Seconds": r["seconds"]} for r in rep["questions"] if r["n_eligible"]]).to_excel(w, sheet_name="Questions", index=False)
        pd.DataFrame([{"Question ID": r["id"], "Document": x["name"], "Link": x["link"], "Doc ID": x["doc_id"],
                       "Scope": x.get("scope"), "Rank": x["rank"], "Page hit": x["page_hit"], "Page distance": x["page_distance"],
                       "Returned pages": ", ".join(map(str, x["pages_returned"])),
                       "Labelled pages": ", ".join(map(str, x["pages_labelled"]))}
                      for r in rep["questions"] for x in r["sources"]]).to_excel(w, sheet_name="Sources", index=False)
        pd.DataFrame([{"Question ID": x["question_id"], "Question": x["question"], "Document": x["name"], "Link": x["link"],
                       "Doc ID": x["doc_id"], "Scope": x["scope"], "Reason": x["reason"], "Detail": x["detail"],
                       "Labelled pages": ", ".join(map(str, x["pages_labelled"]))} for x in rep["excluded"]],
                     columns=["Question ID", "Question", "Document", "Link", "Doc ID", "Scope", "Reason", "Detail",
                              "Labelled pages"]).to_excel(w, sheet_name="Excluded", index=False)


if __name__ == "__main__":
    start(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 10)
    while status()["status"] == "running":
        time.sleep(2)
        print(status()["done"], "/", status()["total"])
    print(status()["status"], list_runs()[:1])
