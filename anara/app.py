"""Local search page: type a query, see the relevant documents and pages.

    .venv/bin/python3 anara/app.py        # then open http://127.0.0.1:8050
    GET /api/search?q=...&k=50  ->  search() output as JSON

Listens on 127.0.0.1 only. Loads the embedding model once at startup.
"""
from __future__ import annotations

import sys
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
from db import EMBEDDINGS, UNIQUE_DOCS, get_db  # noqa: E402
from embedder import get_model  # noqa: E402
from search import search  # noqa: E402
import docinfo  # noqa: E402
import labelled_test  # noqa: E402
import modes  # noqa: E402
import zonesearch  # noqa: E402
from zonesearch import search_zone  # noqa: E402
from proximity import search_bm25near, search_bm25nearcov, search_bm25nearcov2, search_bm25nearmax, search_bm25nearown, search_bm25nearscaled, search_bm25sum, search_proximity  # noqa: E402
from hybrid import keyword_groups, restrict_docs, search_hybrid, search_restrict  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
    get_db().command("ping")
    yield


app = FastAPI(lifespan=lifespan)

PAGE_FILE = Path(__file__).with_name("page.html")   # read per request: UI edits need no restart
REPORT_FILE = Path(__file__).with_name("report.html")
DOCS_FILE = Path(__file__).with_name("docs.html")


GROUPS = 5   # page blocks shown per document


def add_page_groups(results: list[dict]) -> list[dict]:
    """A top page is shown with its neighbours: page n becomes the block n-1, n, n+1 (clipped to
    the document), so a hit and its context read together even when n-1 / n+1 scored lower.
    Blocks are built best page first; a page already inside an earlier block is skipped."""
    db = get_db()
    shown = results
    info = {d["_id"]: d for d in db[UNIQUE_DOCS].find(
        {"_id": {"$in": [r["doc_id"] for r in shown]}}, {"pages": 1, "num_chunks": 1})}
    total = {k: v["pages"] for k, v in info.items()}
    plan = {}
    for r in shown:
        score = {h["page"]: h for h in r["page_hits"]}
        covered, blocks = set(), []
        for h in r["page_hits"]:
            n = h["page"]
            if n in covered:
                continue
            pages = [p for p in (n - 1, n, n + 1) if 1 <= p <= total[r["doc_id"]] and p not in covered]
            covered.update(pages)
            blocks.append([{"page": p, "top": p == n, "score": score[p]["score"] if p in score else None,
                            "snippet": score[p]["snippet"] if p in score else ""} for p in pages])
            if len(blocks) == GROUPS:
                break
        plan[r["doc_id"]] = blocks
    need = [(d, b["page"]) for d, bl in plan.items() for g in bl for b in g if not b["snippet"]]
    if need:
        text = {}
        for row in db[EMBEDDINGS].find({"$or": [{"document_id": d, "page": p} for d, p in need]},
                                        {"document_id": 1, "page": 1, "chunk_index": 1, "text": 1}):
            key = (row["document_id"], row["page"])
            if key not in text or row["chunk_index"] < text[key][0]:
                text[key] = (row["chunk_index"], row["text"])
        for d, bl in plan.items():
            for g in bl:
                for b in g:
                    if not b["snippet"]:
                        b["snippet"] = text.get((d, b["page"]), (0, ""))[1]
    for r in shown:
        r["page_groups"] = plan[r["doc_id"]]
        # share of this document's chunks that were scored: the ones among the 500 nearest
        # (embedding only) or inside the keyword radius (keywords + embedding)
        n = info[r["doc_id"]]["num_chunks"]
        r["total_pages"], r["num_chunks"], r["coverage"] = total[r["doc_id"]], n, round(r["matches"] / n, 3)
    return results


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE_FILE.read_text()


@app.get("/report", response_class=HTMLResponse)
def report_page():
    return REPORT_FILE.read_text()


@app.get("/documents", response_class=HTMLResponse)
def docs_page():
    return DOCS_FILE.read_text()


@app.get("/api/docs/summary")
def api_docs_summary():
    return docinfo.summary()


@app.get("/api/docs/list")
def api_docs_list(state: str = "", verdict: str = "", lang: str = "", q: str = "", indexed: bool = False,
                  offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return docinfo.listing(state, verdict, lang, q, indexed, offset, limit)


@app.get("/api/docs/export")
def api_docs_export(state: str = "", verdict: str = "", lang: str = "", q: str = "", indexed: bool = False):
    return Response(docinfo.to_xlsx(state, verdict, lang, q, indexed), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="source_mapper_documents.xlsx"'})


@app.get("/api/modes")
def api_modes():
    """The mode registry (modes.py): key, number in order of invention, short name, description, config."""
    return modes.modes()


@app.get("/api/test/options")
def api_test_options():
    """What the test panel offers: question states (with counts), statuses, source-document states."""
    return labelled_test.question_options()


@app.post("/api/test/run")
def api_test_run(body: dict = Body(...)):
    """Start a labelled test (see labelled_test.py): {mode, percent, seed, question_state, status, states}. One run at a time."""
    try:
        return labelled_test.start(body.get("mode", "bm25nearcov"), float(body.get("percent", 10)),
                                   states=body.get("states") or None,
                                   seed=body.get("seed") if body.get("seed") not in (None, "") else None,
                                   question_state=body.get("question_state") or labelled_test.DEFAULT_QUESTION_STATE,
                                   status=body.get("status") or "closed")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/test/status")
def api_test_status():
    return labelled_test.status()


@app.post("/api/test/cancel")
def api_test_cancel():
    return labelled_test.cancel()


@app.get("/api/test/runs")
def api_test_runs():
    return labelled_test.list_runs()


@app.get("/api/test/report")
def api_test_report(run: str = Query(min_length=1), format: str = Query("json", pattern="^(json|xlsx)$")):
    try:
        rep = labelled_test.load_run(run)
    except FileNotFoundError:
        raise HTTPException(404, "no such run")
    if format == "json":
        return JSONResponse(rep)
    import io
    buf = io.BytesIO()
    labelled_test.to_xlsx(rep, buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="test-{run}.xlsx"'})


MAX_DOCS = 50      # a normal query returns at most this many documents


@app.get("/api/search")
def api_search(q: str = Query(min_length=1), k: int | None = Query(None, ge=5, le=1000),
               rank: str = Query("top5", pattern="^(top5|count|best)$"),
               mode: str = Query("full", pattern="^(full|hybrid|restrict|zone|proximity|bm25sum|bm25near|bm25nearmax|bm25nearscaled|bm25nearown|bm25nearcov|bm25nearcov2)$"),
               cover: float = Query(0.75, ge=0.1, le=1.0), span: int = Query(6, ge=1, le=30), need_cap: int = Query(4, ge=0, le=20),
               vector: str = Query("original", pattern="^(original|scalar|binary)$"),
               scope: str = Query("all", pattern="^(all|state|state_central|central)$"), state: str = Query(""),
               content: bool = Query(False)):
    """The ranked documents of the mode (see _search_all), at most MAX_DOCS (50) of them."""
    return _search_all(q, k, rank, mode, cover, span, need_cap, vector, scope, state, content)[:MAX_DOCS]


def _search_all(q: str = Query(min_length=1), k: int | None = Query(None, ge=5, le=1000),
               rank: str = Query("top5", pattern="^(top5|count|best)$"),
               mode: str = Query("full", pattern="^(full|hybrid|restrict|zone|proximity|bm25sum|bm25near|bm25nearmax|bm25nearscaled|bm25nearown|bm25nearcov|bm25nearcov2)$"),
               cover: float = Query(0.75, ge=0.1, le=1.0), span: int = Query(6, ge=1, le=30), need_cap: int = Query(4, ge=0, le=20),
               vector: str = Query("original", pattern="^(original|scalar|binary)$"),
               scope: str = Query("all", pattern="^(all|state|state_central|central)$"), state: str = Query(""),
               content: bool = Query(False)):
    """mode=hybrid narrows by keywords (+/-2 pages) before the embedding search; mode=restrict keeps
    only documents containing every keyword group (see hybrid.py); mode=proximity matches keywords on 5-page BM25 pages, closest together first (see proximity.py);
    mode=zone maps the query's crop to
    page zones and scans them forward for the keyword groups (see zonesearch.py).
    scope: all = every document; state_central = `state`'s documents + Central; state = `state`'s documents only
    (with no `state`: state = every non-Central document); central. See common.scope_key."""
    scope = common.scope_key(scope, state)
    if mode == "bm25nearcov2":  # bm25nearcov with coverage weights 0.9 (+/-1 page) and 0.7 (+/-2 pages) for neighbour-only groups
        return search_bm25nearcov2(q, scope=scope, content=content)
    if mode == "bm25nearcov":  # bm25nearown x group coverage (page groups + half neighbour-only groups) / all groups
        return search_bm25nearcov(q, scope=scope, content=content)
    if mode == "bm25nearown":  # own page score in full, neighbours at half, best per group, scaled crop factor
        return search_bm25nearown(q, scope=scope, content=content)
    if mode == "bm25nearscaled":  # best-of-neighbours, crop bump scaled by the crop score and distance, -25% without
        return search_bm25nearscaled(q, scope=scope, content=content)
    if mode == "bm25nearmax":  # best of each group over a page and its +/-2 neighbours, then summed; crop factor +30% / -15%
        return search_bm25nearmax(q, scope=scope, content=content)
    if mode == "bm25near":     # raw BM25 summed over a page and its +/-2 neighbours, scores <= 1 left out
        return search_bm25near(q, scope=scope, content=content)
    if mode == "bm25sum":      # raw BM25 sum per page, no threshold, no windows, no crop filter (see proximity.py)
        return search_bm25sum(q, scope=scope, content=content)
    if mode == "proximity":   # no embeddings, no crop zones: 5-page BM25 keyword matrix, closest keywords win
        return search_proximity(q, cover=cover, scope=scope, content=content)   # content: page text snippets (slower)
    if mode == "zone":   # no embeddings, no Atlas: crop zones + forward pass over keyword layers
        return search_zone(q, cover=cover, max_span=span, need_cap=need_cap, scope=scope)
    # the embedding modes only hold the original documents; scope filters their results afterwards
    if mode == "restrict":
        res = search_restrict(q)
    elif mode == "hybrid":
        res = search_hybrid(q)
    else:
        res = search(q, k, rank=rank, backend=vector)
    return add_page_groups([r for r in res if common.in_scope(r["doc_id"], scope)])   # vector: which Atlas index (mode=full only)


@app.get("/api/groups")
def api_groups(q: str = Query(min_length=1)):
    """Keyword groups of a query and how many documents contain each (restrict mode)."""
    groups = keyword_groups(q)
    docs, counts = restrict_docs(groups)
    return {"used": groups, "unmatched": [], "doc_counts": counts, "candidate_docs": len(docs)}


@app.get("/api/random_question/states")
def api_random_states():
    """Document states with the number of labelled questions that have an eligible source in them (+ "All")."""
    return labelled_test.random_states()


@app.get("/api/random_question")
def api_random_question(state: str = Query(labelled_test.ALL_STATES), seed: int | None = Query(None)):
    """A random labelled closed question with an eligible source (indexed, English text, labelled pages) whose document
    is in `state` ("All" = any); see labelled_test.random_question."""
    return labelled_test.random_question(state, seed)


@app.get("/api/known")
def api_known(q: str = Query(min_length=1), mode: str = Query("bm25nearcov"),
              scope: str = Query("all", pattern="^(all|state|state_central|central)$"), state: str = Query("")):
    """Labelled questions with exactly this text and their sources/pages (see labelled_test.known_question). A source whose
    document is outside the search scope (as in /api/search) is reported as not checkable, not as a miss."""
    names = labelled_test.doc_state_names()
    chosen = {"all": names, "central": ["Central"], "state_central": [state, "Central"] if state else names,
              "state": [state] if state else [n for n in names if n != "Central"]}[scope]
    return labelled_test.known_question(q, mode, tuple(chosen))


@app.get("/api/link")
def api_link(url: str = Query(min_length=1)):
    """The document behind a pasted link: {"found": bool, "indexed": bool, ...}."""
    doc_id = common.doc_for_link(url)
    if doc_id is None:
        return {"found": False}
    info = common.aliases()["docs"].get(doc_id)
    res = _link_doc(doc_id)
    if info:   # the DB's original link; "duplicate" when the pasted link is another copy of it
        res["link"] = info["original_link"] or res["link"]
        res["duplicate"] = common.link_id(url) != info["original_file_id"]
    return res


def _link_doc(doc_id: str) -> dict:
    row = get_db()[UNIQUE_DOCS].find_one({"_id": doc_id})
    if row:
        return {"found": True, "indexed": True, "modes": "all", "doc_id": doc_id, "name": row["name"],
                "link": row["link"], "pages": row["pages"]}
    zone = zonesearch._load()["table"]      # keyword-only documents: in the zone index, no embeddings
    if doc_id in zone.index:
        z = zone.loc[doc_id]
        return {"found": True, "indexed": True, "modes": "zone", "doc_id": doc_id, "name": z["name"],
                "link": z["link"], "pages": int(z["pages"])}
    import pandas as pd
    rep = pd.read_csv(common.DATA / "text_report.csv").set_index("doc_id")
    docs = common.load_documents().set_index("doc_id")
    why = rep.loc[doc_id, "verdict"] if doc_id in rep.index else "unknown"
    return {"found": True, "indexed": False, "doc_id": doc_id, "name": docs.loc[doc_id, "name"],
            "link": docs.loc[doc_id, "link"], "pages": int(docs.loc[doc_id, "pages"]), "verdict": why}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
