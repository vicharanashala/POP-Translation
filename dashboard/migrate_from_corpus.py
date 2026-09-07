"""One-off CLI: load the `documents` main table from the Zoho corpus.

One row per file-in-a-folder, each with its own ANNAM id.

**THE CRAWL IS THE ROW LIST.** fix/out/zoho_crawl.jsonl is what WorkDrive
actually contains; the CSVs only decorate it. That is the whole point -- the
table is meant to hold every association as seen from the corpus root, and any
CSV is a snapshot of one earlier pass that has since fallen behind. Loading
from report_true.csv instead (as an earlier version of this script did) silently
drops ~900 files that are in the folders but never made it into the CSV.

Sources, in the order they are joined:

  fix/out/zoho_crawl.jsonl                        THE ROW LIST. One entry per
      file, with its state folder, its folder, its Zoho file id and its size.
      Restricted to top-level folders named "State *" or "Central Advisories"
      (--include-non-state keeps the rest) -- 9,811 files of the crawl's 10,298.
  results/state_language_report/report_true.csv   8,934 rows, joined on
      (state, folder, filename) -- supplies sha256, language, num_pages and the
      collection date. Matches 8,913 of the 9,811 crawl files; only 6 of its own
      placements are missing from the crawl.
  fix/out/zoho_metadata.json                      7,931 entries keyed by sha,
      re-indexed here by file_id -- a reliable page count and size.
  results/pops.csv                                7,513 rows keyed by sha --
      the 18 manual metadata fields plus shareable name/link and
      translation/review status. Files with no sha (no CSV match) get null
      metadata rather than being skipped: a file that exists in the corpus
      belongs in the table whether or not anyone has catalogued it.

sha256 is stored but NOT unique, and is null for the ~900 files no CSV covers.
`source_key` is the Zoho **file id**, which is the natural key here: WorkDrive
stores a separate physical copy per folder, so a file id identifies exactly one
placement and makes the load idempotent.

This loader reads NOTHING from the previous schema's database. It is
self-contained: the crawl plus the CSV dumps are the only inputs.

Usage:
    .venv/bin/python3 -m dashboard.migrate_from_corpus --dry-run
    .venv/bin/python3 -m dashboard.migrate_from_corpus
    .venv/bin/python3 -m dashboard.migrate_from_corpus --reconcile-only
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REPORT_TRUE = REPO_ROOT / "results" / "state_language_report" / "report_true.csv"
ZOHO_METADATA = REPO_ROOT / "fix" / "out" / "zoho_metadata.json"
POPS_CSV = REPO_ROOT / "results" / "pops.csv"
ZOHO_CRAWL = REPO_ROOT / "fix" / "out" / "zoho_crawl.jsonl"
# scripts/backfill_new_rows.py -- sha256/pages/language per Zoho FILE ID, for
# the files no CSV covered. Keyed by file id rather than by name, which makes it
# exact where the (state, folder, filename) join is not.
BACKFILL = REPO_ROOT / "fix" / "out" / "backfill_new_rows.jsonl"
# scripts/fill_zoho_metadata.py -- what WorkDrive itself reports, per file id.
ZOHO_BY_ID = REPO_ROOT / "fix" / "out" / "zoho_file_metadata_by_id.json"

_BATCH = 1000


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _str_or_none(v):
    return v if v else None


def _ms_to_dt(ms):
    """Zoho epoch milliseconds -> naive UTC, matching models.utcnow()."""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _file_id_from_link(link: str) -> str | None:
    """report_true's doc_link is https://workdrive.zoho.in/file/<file_id>."""
    if not link:
        return None
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def is_state_or_central(top_level: str) -> bool:
    """A top-level folder that is a state or the all-India category.

    The corpus root also holds `Transition1`, `Others`, `Transition 2`,
    `0Codon Stream` and `Master Sheet` -- 487 files that are staging areas and
    working notes rather than the catalogue. Per the user, the table holds the
    states and Central Advisories.
    """
    return top_level.strip().lower().startswith(("state", "central"))


def load_rows(include_non_state: bool = False) -> list[dict]:
    """The crawl, decorated with whatever the CSVs know about each file.

    One entry per file in the corpus, as the field dicts new_document()
    expects. A file the CSVs have never seen still gets a row -- with its Zoho
    id, name, size and a working link, and null metadata.
    """
    from dashboard.models import MANUAL_METADATA_FIELDS

    if not ZOHO_CRAWL.exists():
        raise SystemExit(f"Missing {ZOHO_CRAWL} -- run scripts/crawl_zoho_root.py first")
    crawl = [json.loads(line) for line in ZOHO_CRAWL.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not include_non_state:
        crawl = [c for c in crawl if is_state_or_central(c["state"])]

    with REPORT_TRUE.open(encoding="utf-8", newline="") as fh:
        report = list(csv.DictReader(fh))
    meta_by_sha = json.loads(ZOHO_METADATA.read_text(encoding="utf-8"))
    # zoho_metadata.json is keyed by sha but each entry names its file_id, and
    # a file id is what the crawl gives us.
    meta_by_fid = {m["file_id"]: m for m in meta_by_sha.values() if m.get("file_id")}
    with POPS_CSV.open(encoding="utf-8", newline="") as fh:
        pops = {p["sha256"]: p for p in csv.DictReader(fh)}

    # Overlaid AFTER the CSV join, because a Zoho file id identifies exactly one
    # physical copy while (state, folder, filename) does not: where a folder
    # holds two files with the same name, the name join can only match the
    # first. These two files are the authority for what they cover.
    backfill: dict[str, dict] = {}
    if BACKFILL.exists():
        for line in BACKFILL.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated final line from a killed run
            if not rec.get("error"):
                backfill[rec["zoho_file_id"]] = rec
    zoho_by_id = json.loads(ZOHO_BY_ID.read_text(encoding="utf-8")) if ZOHO_BY_ID.exists() else {}

    # (state, folder, filename) -> report rows. A list because report_true
    # repeats 11 identical triples; they are consumed in order so two crawl
    # copies of the same name in one folder take one CSV row each.
    def key(state, folder, name):
        return ((state or "").strip(), (folder or "").strip(), (name or "").strip())

    report_by_placement: dict[tuple, list[dict]] = defaultdict(list)
    for r in report:
        report_by_placement[key(r["state"], r["folder"], r["pdf"])].append(r)
    taken: Counter = Counter()

    out = []
    for c in crawl:
        k = key(c["state"], c["folder"], c["name"])
        candidates = report_by_placement.get(k, [])
        i = taken[k]
        row = candidates[i] if i < len(candidates) else {}
        taken[k] += 1

        sha = _str_or_none(row.get("sha"))
        # Prefer what the crawl saw over what a CSV once recorded -- the crawl
        # is this minute's truth about where the file is and how big it is.
        m = meta_by_fid.get(c["file_id"], {})
        p = pops.get(sha, {}) if sha else {}

        fields = {
            # The Zoho file id: unique per placement, because WorkDrive keeps a
            # separate physical copy in each folder.
            "source_key": c["file_id"],
            "sha256": sha,
            "zoho_file_id": c["file_id"],
            "num_pages": m.get("pages") or _int_or_none(row.get("num_pages")),
            "language": _str_or_none(row.get("language")),
            "format_original": row.get("format") or "pdf",
            "shareable_name": c["name"],
            # Derived from THIS copy's own file id, never from the CSVs.
            # pops.csv and report_true are keyed by sha256, so they hold one
            # link per document -- using them here gave all of a document's
            # copies the same URL, pointing 1,000 of 9,811 placements at a
            # different physical file than the one they describe.
            "shareable_link": f"https://workdrive.zoho.in/file/{c['file_id']}"
                              if c.get("file_id")
                              else (_str_or_none(p.get("shareable_link"))
                                    or _str_or_none(row.get("doc_link"))),
            # Collection date is per-placement in report_true, so it is taken
            # from there rather than from the per-file pops row.
            "date_of_collection": _str_or_none(row.get("Date of Collection")),
            "month_of_collection": _int_or_none(row.get("Month of Collection")),
            "year_of_collection": _int_or_none(row.get("Year of Collection")),
            "translation_shareable_link": _str_or_none(p.get("translation_shareable_link")),
            "translation_status": p.get("translation_status") or "not_started",
            "translation_zoho_file_id": _str_or_none(p.get("translation_zoho_file_id")),
            "review_shareable_link": _str_or_none(p.get("review_shareable_link")),
            "review_status": p.get("review_status") or "not_started",
            "review_zoho_file_id": _str_or_none(p.get("review_zoho_file_id")),
        }
        for name in MANUAL_METADATA_FIELDS:
            if name in fields:
                continue  # collection date already taken from report_true
            value = p.get(name)
            fields[name] = _int_or_none(value) if name.startswith(("month_", "year_")) else _str_or_none(value)

        # -- overlay, by file id --------------------------------------------
        bf = backfill.get(c["file_id"])
        if bf:
            fields["sha256"] = bf.get("sha256") or fields["sha256"]
            fields["num_pages"] = bf.get("num_pages") or fields["num_pages"]
            fields["language"] = fields["language"] or bf.get("language")
        zm = zoho_by_id.get(c["file_id"]) or {}
        if not zm.get("error"):
            fields["num_pages"] = fields["num_pages"] or zm.get("pages")
            if zm.get("extn"):
                fields["format_original"] = zm["extn"]
            # The collection date IS the Zoho created date -- it matches
            # report_true to the day for 7,634 of 7,931 original files (96%) --
            # so a file the CSV never covered still gets one. The timestamp
            # itself is not stored; only the collection date it produces is a
            # field anyone asked for.
            created = _ms_to_dt(zm.get("created_ms"))
            if created and not fields.get("date_of_collection"):
                fields["date_of_collection"] = created.date().isoformat()
                fields["month_of_collection"] = created.month
                fields["year_of_collection"] = created.year

        out.append({"state": c["state"], "crop": c["folder"], "fields": fields})
    return out


def reconcile(rows: list[dict], *, write_csv: bool = True) -> None:
    """Report what the crawl holds that this load leaves out, and vice versa.

    Reports only -- never adds or deletes. Since the crawl is now the row list,
    this is no longer a "did we miss files" check; the only two gaps that can
    exist are deliberate ones, and both are printed so neither is silent:

      1. Crawl files in top-level folders that are not a state and not Central
         Advisories -- excluded by default (--include-non-state).
      2. report_true.csv placements the crawl does not have. These are the
         genuinely interesting ones: a row someone catalogued that is no longer
         in the folder it claims. A broken link, a move, or a deletion.

    Note what this CANNOT tell you: the crawl only lists two levels and skips
    "Multiple Uses File" folders, so a file that lives deeper is reported as
    "not in the crawl" when it is really just not looked at. See
    scripts/crawl_zoho_root.py.
    """
    if not ZOHO_CRAWL.exists():
        print(f"[reconcile] no {ZOHO_CRAWL} -- run scripts/crawl_zoho_root.py first")
        return
    crawl = [json.loads(line) for line in ZOHO_CRAWL.read_text(encoding="utf-8").splitlines() if line.strip()]
    loaded_ids = {r["fields"]["source_key"] for r in rows}

    excluded = [c for c in crawl if c["file_id"] not in loaded_ids]
    by_top = Counter(c["state"] for c in excluded)
    print(f"\n[reconcile] crawl holds {len(crawl)} file(s); this load takes {len(rows)}")
    if excluded:
        print(f"[reconcile] {len(excluded)} crawl file(s) excluded, by top-level folder:")
        for name, n in by_top.most_common():
            print(f"[reconcile]     {n:5d}  {name}")

    with REPORT_TRUE.open(encoding="utf-8", newline="") as fh:
        report = list(csv.DictReader(fh))

    def key(state, folder, name):
        return ((state or "").strip(), (folder or "").strip(), (name or "").strip())

    in_crawl = {key(c["state"], c["folder"], c["name"]) for c in crawl}
    missing = [r for r in report if key(r["state"], r["folder"], r["pdf"]) not in in_crawl]
    print(f"[reconcile] {len(missing)} report_true placement(s) the crawl does not have")
    for r in missing[:20]:
        print(f"[reconcile]     {r['state']} / {r['folder']} :: {r['pdf'][:70]}")

    if write_csv and missing:
        out = REPO_ROOT / "fix" / "out" / "reconcile_missing_from_workdrive.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "folder", "pdf", "sha", "doc_link"])
            for r in missing:
                w.writerow([r["state"], r["folder"], r["pdf"], r.get("sha", ""), r.get("doc_link", "")])
        print(f"[reconcile] wrote {out}")


# Fields that describe one physical copy in WorkDrive, not the document. These
# go into `duplicate_links`; everything else in a loaded row is document-level.
# WorkDrive keeps a separate file per folder, so a document grouped by sha256
# can own dozens of these.
_COPY_FIELDS = ("zoho_file_id", "shareable_link", "shareable_name")

# report_true's language column is a VERDICT ("English"/"Non-English") plus a
# handful of OCR failure strings. Neither is a language, so the verdicts are
# turned into a real language by resolve_language(): English stays English,
# anything else takes its state's language. The failure strings are simply
# dropped -- an OCR diagnostic is not a document field.
_LANGUAGE_VERDICTS = {"english": "eng", "non-english": "non_english"}


def _language_code(value: str | None) -> tuple[str | None, str | None]:
    """(language code, note). Returns (None, original) for anything that is not
    one of the two verdicts -- errors, 'Unknown (too little OCR text)'."""
    if not value:
        return None, None
    code = _LANGUAGE_VERDICTS.get(value.strip().lower())
    return (code, None) if code else (None, value)


def _completeness(fields: dict) -> int:
    """How many fields a loaded row actually has a value for. Used to pick which
    of several placements of one document supplies its metadata -- they can
    disagree, and 'the one that knows most' beats 'whichever came first'."""
    return sum(1 for v in fields.values() if v not in (None, ""))


def build_collections(rows: list[dict]) -> tuple[dict, list[dict]]:
    """Group the loaded per-file rows into (unique documents, placements).

    Grouped by sha256, because byte-identical is a fact and needs no human
    approval. Near-duplicates -- a re-scan, a re-export -- stay separate until
    the algorithm proposes them and someone accepts (dashboard/routes_merge.py).

    A file with no sha256 gets its own document keyed by its Zoho file id: it
    cannot be proven identical to anything, so it must not be silently pooled
    with other unhashed files.
    """
    from dashboard.languages import resolve_language

    groups: dict[str, list[dict]] = {}
    for r in rows:
        sha = r["fields"].get("sha256")
        groups.setdefault(sha or f"nosha:{r['fields']['source_key']}", []).append(r)

    docs: dict[str, dict] = {}
    placements: list[dict] = []
    for group_key, members in groups.items():
        best = max(members, key=lambda m: _completeness(m["fields"]))
        fields = {k: v for k, v in best["fields"].items()
                  if k not in _COPY_FIELDS and k != "source_key"}
        # The OCR pass's output is a VERDICT ("English"/"Non-English") plus a
        # few failure strings, not a language. Map the verdicts, then resolve to
        # a real language: English stays English, everything else takes its
        # state's language. Same rule as scripts/fill_language_from_state.py,
        # applied at load so a rebuild and an in-place fill agree.
        verdict, _note = _language_code(fields.pop("language", None))
        code, source = resolve_language(verdict, [m["state"] for m in members])
        fields["language"] = code
        fields["language_source"] = source
        # A representative name and link for the document; each copy keeps its
        # own in duplicate_links, because two copies can be named differently.
        fields["shareable_name"] = best["fields"].get("shareable_name")
        # The same placement anchors the document's file: the one that supplied
        # the metadata is the one the metadata describes.
        fields["representative_file_id"] = best["fields"].get("zoho_file_id")
        fields["shareable_link"] = best["fields"].get("shareable_link")
        docs[group_key] = fields

        for m in members:
            placements.append({
                "group_key": group_key,
                "state": m["state"],
                "crop": m["crop"],
                "source_key": m["fields"]["source_key"],
                "copy": {k: m["fields"].get(k) for k in _COPY_FIELDS} | {
                    "state": m["state"], "crop": m["crop"],
                },
            })
    return docs, placements


def seed_lookups(db, rows: list[dict]) -> None:
    """Fill the states / crops / languages vocabularies.

    Upserted by name, never deleted: a state the team added by hand survives a
    reload, and a name that has stopped appearing in the corpus still resolves
    for any row that references it. `raw_names` records every source spelling
    that normalised to this entry, which is how a lookup stays reversible.
    """
    from dashboard.models import COLL_CROPS, COLL_LANGUAGES, COLL_STATES, utcnow
    from dashboard.languages import LANGUAGES

    for collection, key in ((COLL_STATES, "state"), (COLL_CROPS, "crop")):
        seen: dict[str, dict] = {}
        for r in rows:
            from dashboard.models import normalize_crop_name, normalize_state_name

            raw = r[key]
            name = normalize_state_name(raw) if key == "state" else normalize_crop_name(raw)
            entry = seen.setdefault(name, {"raw_names": set(), "count": 0})
            entry["raw_names"].add(raw)
            entry["count"] += 1
        for name, entry in seen.items():
            db[collection].update_one(
                {"name": name},
                {"$set": {"document_count": entry["count"], "updated_at": utcnow()},
                 "$addToSet": {"raw_names": {"$each": sorted(entry["raw_names"])}},
                 "$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
        print(f"[migrate] {collection}: {len(seen)} entries")

    # English, Non-English, and the 14 tessdata_best languages. "Non-English" is
    # not a language, but it IS what the OCR pass concluded for 1,265 documents,
    # so the team needs it in the list to refine those rows from.
    vocabulary = [{"code": code, "label": label} for code, label in sorted(LANGUAGES.items())]
    vocabulary.append({"code": "non_english", "label": "Non-English"})
    for entry in vocabulary:
        db[COLL_LANGUAGES].update_one(
            {"code": entry["code"]},
            {"$set": {"label": entry["label"], "updated_at": utcnow()},
             "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )
    print(f"[migrate] {COLL_LANGUAGES}: {len(vocabulary)} entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only load this many rows (smoke test).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be loaded; touch no database.")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="Only diff the loaded rows against fix/out/zoho_crawl.jsonl.")
    parser.add_argument("--include-non-state", action="store_true",
                        help="Also load Transition1/Others/Transition 2/etc. (487 files), not just states + central.")
    parser.add_argument("--wipe", action="store_true",
                        help="Drop `documents` and `unique_documents` before loading. Required to "
                             "rebuild over the previous flat schema.")
    args = parser.parse_args()

    for path in (ZOHO_CRAWL, REPORT_TRUE, ZOHO_METADATA, POPS_CSV):
        if not path.exists():
            print(f"Missing {path}", file=sys.stderr)
            sys.exit(1)

    rows = load_rows(include_non_state=args.include_non_state)
    if args.limit:
        rows = rows[: args.limit]
    shas = {r["fields"]["sha256"] for r in rows if r["fields"]["sha256"]}
    no_sha = sum(1 for r in rows if not r["fields"]["sha256"])
    print(f"[migrate] {len(rows)} file(s) from the crawl, {len(shas)} distinct sha256")
    print(f"[migrate] {no_sha} file(s) matched no CSV row -- loaded with a link but null metadata")
    print(f"[migrate] {len({r['state'] for r in rows})} state(s), {len({r['crop'] for r in rows})} crop(s)")
    # What actually arrives populated. The 18 MANUAL_METADATA_FIELDS are empty
    # for every row in pops.csv -- they are the fields a human fills in through
    # the dashboard and no corpus pass ever wrote them -- so they load as null
    # by design, not because the join missed.
    for label, field in (("zoho_file_id", "zoho_file_id"), ("shareable_link", "shareable_link"),
                         ("num_pages", "num_pages"), ("language", "language"),
                         ("year_of_collection", "year_of_collection")):
        filled = sum(1 for r in rows if r["fields"].get(field) not in (None, ""))
        print(f"[migrate]   {label:20s} {filled}/{len(rows)}")
    translated = sum(1 for r in rows if r["fields"].get("translation_status") == "done")
    reviewed = sum(1 for r in rows if r["fields"].get("review_status") == "done")
    print(f"[migrate]   translation_status=done  {translated}")
    print(f"[migrate]   review_status=done       {reviewed}")

    if args.reconcile_only:
        reconcile(rows)
        return

    if args.dry_run:
        sample = rows[0]
        print("\n[migrate] --dry-run, nothing written. First row would be:")
        print(json.dumps({"state": sample["state"], "crop": sample["crop"], **sample["fields"]},
                         indent=2, default=str)[:2000])
        reconcile(rows)
        return

    from pymongo import UpdateOne

    from dashboard.db import get_database, init_db
    from dashboard.display_id import free_display_ids, free_row_ids
    from dashboard.models import (
        COLL_CROPS,
        COLL_DOCUMENTS,
        COLL_LANGUAGES,
        COLL_STATES,
        COLL_UNIQUE_DOCUMENTS,
        new_document,
        new_unique_document,
    )

    db = get_database()
    print(f"[migrate] target database: {db.name}")

    # The previous flat schema kept metadata on `documents` and had no
    # `unique_document_id`. Rebuilding on top of it would leave a collection
    # that is half one shape and half the other, so refuse rather than guess.
    stale = db[COLL_DOCUMENTS].count_documents({"unique_document_id": {"$exists": False}})
    if stale and not args.wipe:
        print(f"[migrate] {stale} row(s) are in the old flat shape (no unique_document_id).",
              file=sys.stderr)
        print("[migrate] Re-run with --wipe to drop `documents` and `unique_documents` "
              "and rebuild both.", file=sys.stderr)
        sys.exit(1)
    if args.wipe:
        # Dropped, not emptied, and BEFORE init_db(): the old rows have no
        # row_id, so building the unique index on them fails outright. Dropping
        # takes the stale indexes with it.
        for name in (COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS):
            count = db[name].count_documents({})
            db[name].drop()
            print(f"[migrate] dropped {name} ({count} row(s))")

    init_db()

    docs, placements = build_collections(rows)
    print(f"[migrate] building {len(docs)} unique document(s) for {len(placements)} placement(s)")

    # -- unique documents -----------------------------------------------------
    display_ids = free_display_ids(db, len(docs))
    doc_ids: dict[str, object] = {}
    batch, keys = [], []
    inserted_docs = 0
    for i, ((group_key, fields), display_id) in enumerate(zip(docs.items(), display_ids), start=1):
        batch.append(new_unique_document(display_id=display_id, **fields))
        keys.append(group_key)
        if len(batch) >= _BATCH or i == len(docs):
            result = db[COLL_UNIQUE_DOCUMENTS].insert_many(batch, ordered=False)
            doc_ids.update(zip(keys, result.inserted_ids))
            inserted_docs += len(batch)
            batch, keys = [], []
            print(f"[migrate] [{i}/{len(docs)}] unique documents inserted...")

    # -- placements -----------------------------------------------------------
    row_ids = free_row_ids(db, len(placements))
    batch, inserted_rows = [], 0
    # row_id -> the copy that placement points at, so duplicate_links can be
    # filled in one pass once every placement has its Mongo _id.
    copies_by_row: dict[int, tuple[str, dict]] = {}
    for i, (placement, row_id) in enumerate(zip(placements, row_ids), start=1):
        batch.append(new_document(
            row_id=row_id,
            unique_document_id=doc_ids[placement["group_key"]],
            state=placement["state"],
            crop=placement["crop"],
            source_key=placement["source_key"],
        ))
        copies_by_row[row_id] = (placement["group_key"], placement["copy"])
        if len(batch) >= _BATCH or i == len(placements):
            db[COLL_DOCUMENTS].insert_many(batch, ordered=False)
            inserted_rows += len(batch)
            batch = []
            print(f"[migrate] [{i}/{len(placements)}] placements inserted...")

    # -- wire the two together ------------------------------------------------
    # Done as one bulk pass rather than through link_placement() per row: that
    # helper is two round trips each, which is right for an upload and 20,000
    # round trips here. The shape it produces is identical.
    per_doc: dict[object, dict] = {}
    for row in db[COLL_DOCUMENTS].find({}, {"row_id": 1, "unique_document_id": 1}):
        entry = copies_by_row.get(row["row_id"])
        if entry is None:
            continue  # a row from an earlier partial run; left alone
        _group_key, copy = entry
        agg = per_doc.setdefault(row["unique_document_id"], {"ids": [], "links": []})
        agg["ids"].append(row["_id"])
        agg["links"].append({**copy, "row_id": row["row_id"]})
    # The anchor's row id can only be known now, once the placements have ids.
    anchor_file = {
        d["_id"]: d.get("representative_file_id")
        for d in db[COLL_UNIQUE_DOCUMENTS].find({}, {"representative_file_id": 1})
    }
    ops = []
    for uid, agg in per_doc.items():
        update = {"main_row_ids": agg["ids"], "duplicate_links": agg["links"]}
        wanted = anchor_file.get(uid)
        anchor = next((l for l in agg["links"] if l.get("zoho_file_id") == wanted), None)
        # Fall back to the first copy rather than leaving a document unanchored:
        # something has to be translatable, and every copy here is byte-identical.
        anchor = anchor or (agg["links"][0] if agg["links"] else None)
        if anchor:
            update["representative_file_id"] = anchor.get("zoho_file_id")
            update["representative_row_id"] = anchor.get("row_id")
            # The document's link is the anchor's link, always. They move
            # together or the document points at a file it does not describe.
            update["shareable_link"] = anchor.get("shareable_link")
            update["shareable_name"] = anchor.get("shareable_name")
        ops.append(UpdateOne({"_id": uid}, {"$set": update}))
    linked = 0
    for i in range(0, len(ops), 500):
        linked += db[COLL_UNIQUE_DOCUMENTS].bulk_write(ops[i : i + 500], ordered=False).modified_count
    print(f"[migrate] linked {linked} document(s) to their placements")

    seed_lookups(db, rows)

    print(f"[migrate] done: {inserted_docs} unique document(s), {inserted_rows} placement(s)")
    print(f"[migrate]   documents        {db[COLL_DOCUMENTS].count_documents({})}")
    print(f"[migrate]   unique_documents {db[COLL_UNIQUE_DOCUMENTS].count_documents({})}")
    print(f"[migrate]   states/crops/languages "
          f"{db[COLL_STATES].count_documents({})}/{db[COLL_CROPS].count_documents({})}/"
          f"{db[COLL_LANGUAGES].count_documents({})}")


if __name__ == "__main__":
    main()
