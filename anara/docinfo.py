"""Document inventory for the /docs page: how many documents there are, per state (Central included), how many are
catalogued as English, and how many have English text that could be extracted (= the documents in the keyword index).

Sources, all local files: the document list (common.load_documents: sheet + Central + other states), the text-check report
data/text_report.csv (verdict per downloaded document) and the keyword index's document list (data/prox_meta.json).
A document with several states is counted once in each of its states; the Total row counts each document once.
"""
from __future__ import annotations

import io
import json

import pandas as pd

import common
import labelled_test

VERDICTS = ["english_text", "scanned_no_text", "mixed", "non_english", "not_pdf", "unreadable", "not_checked"]
VERDICT_LABELS = {"english_text": "English text extractable", "scanned_no_text": "Scanned, no text", "mixed": "Mixed (some pages scanned)",
                  "non_english": "Text not English", "not_pdf": "Not a PDF", "unreadable": "Unreadable file",
                  "not_checked": "Not checked (never downloaded)"}
NO_STATE = "(no state)"


def _stamp():
    return [(f.name, f.stat().st_mtime) for f in (common.DATA / "text_report.csv", common.DATA / "prox_meta.json",
                                                     common.DATA / "other_state_docs.csv", common.DATA / "central_docs.csv") if f.exists()]


_cache: dict = {}


def table() -> pd.DataFrame:
    """One row per document: doc_id, name, link, fmt, pages, catalogued_lang, text_lang, verdict, indexed, states (list)."""
    st = _stamp()
    if _cache.get("stamp") == st:
        return _cache["df"]
    docs = common.load_documents().drop_duplicates("doc_id").set_index("doc_id")
    rep = pd.read_csv(common.DATA / "text_report.csv", keep_default_na=False).drop_duplicates("doc_id").set_index("doc_id")
    index = set(json.loads((common.DATA / "prox_meta.json").read_text())["docs"])
    sts = labelled_test._doc_states()
    v = rep["verdict"].astype(str).map(lambda x: "unreadable" if x.startswith("unreadable") else x)
    df = pd.DataFrame({"doc_id": docs.index, "name": docs["name"].values, "link": docs["link"].values, "fmt": docs["fmt"].values,
                       "pages": pd.to_numeric(docs["pages"], errors="coerce").fillna(0).astype(int).values,
                       "catalogued_lang": docs["language"].values})
    df["text_lang"] = df.doc_id.map(rep["lang"]).fillna("")
    df["verdict"] = df.doc_id.map(v).fillna("not_checked")
    df["indexed"] = df.doc_id.isin(index)
    df["states"] = df.doc_id.map(lambda d: list(sts.get(d, ())) or [NO_STATE])
    _cache.update(stamp=st, df=df)
    return df


def _counts(g: pd.DataFrame) -> dict:
    vc = g.verdict.value_counts()
    out = {"docs": len(g), "pages": int(g.pages.sum()), "catalogued_english": int((g.catalogued_lang == "eng").sum()),
           "indexed": int(g.indexed.sum()), "indexed_pages": int(g.loc[g.indexed, "pages"].sum())}
    out.update({k: int(vc.get(k, 0)) for k in VERDICTS})
    out["checked"] = out["docs"] - out["not_checked"]
    out["catalogued_english_extractable"] = int(((g.catalogued_lang == "eng") & (g.verdict == "english_text")).sum())
    out["english_text_not_catalogued_english"] = int(((g.catalogued_lang != "eng") & (g.verdict == "english_text")).sum())
    return out


def summary() -> dict:
    df = table()
    ex = df.explode("states")
    rows = [{"state": state, **_counts(g)} for state, g in ex.groupby("states")]
    rows.sort(key=lambda r: (r["state"] == NO_STATE, -r["docs"], r["state"]))
    langs = df.catalogued_lang.value_counts().head(12)
    return {"total": _counts(df), "states": rows, "verdicts": VERDICT_LABELS,
            "catalogued_languages": [{"lang": k, "docs": int(v)} for k, v in langs.items()],
            "multi_state_docs": int((df.states.map(len) > 1).sum())}


def _filtered(state, verdict, lang, q, only_indexed) -> pd.DataFrame:
    df = table()
    if state:
        df = df[df.states.map(lambda s: state in s)]
    if verdict:
        df = df[df.verdict == verdict]
    if lang:
        df = df[df.catalogued_lang == lang]
    if only_indexed:
        df = df[df.indexed]
    if q.strip():
        df = df[df.name.str.contains(q.strip(), case=False, regex=False)]
    return df.sort_values("name")


def _row(r) -> dict:
    return {"doc_id": r.doc_id, "name": r.name, "link": r.link, "fmt": r.fmt, "pages": int(r.pages), "catalogued_lang": r.catalogued_lang,
            "text_lang": r.text_lang, "verdict": r.verdict, "indexed": bool(r.indexed), "states": list(r.states)}


def listing(state: str = "", verdict: str = "", lang: str = "", q: str = "", only_indexed: bool = False,
            offset: int = 0, limit: int = 100) -> dict:
    df = _filtered(state, verdict, lang, q, only_indexed)
    return {"total": len(df), "offset": offset, "rows": [_row(r) for r in df.iloc[offset: offset + limit].itertuples(index=False)]}


def to_xlsx(state: str = "", verdict: str = "", lang: str = "", q: str = "", only_indexed: bool = False) -> bytes:
    """Summary sheet + the (filtered) document list."""
    s = summary()
    cols = ["state", "docs", "catalogued_english", "pages", "checked"] + VERDICTS + ["indexed", "indexed_pages",
                                                                                      "catalogued_english_extractable"]
    sm = pd.DataFrame([s["total"] | {"state": "TOTAL (each document once)"}] + s["states"])[cols]
    df = _filtered(state, verdict, lang, q, only_indexed).copy()
    df["states"] = df.states.map(", ".join)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        sm.to_excel(w, sheet_name="By state", index=False)
        df[["doc_id", "name", "states", "fmt", "pages", "catalogued_lang", "text_lang", "verdict", "indexed", "link"]].to_excel(
            w, sheet_name="Documents", index=False)
    return buf.getvalue()
