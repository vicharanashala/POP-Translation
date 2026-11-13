# first_render backend — main table + unique documents

Branch `first_render`. Written 2026-09-05, split into two collections
2026-09-06.

## Shape

```
documents          9,811 rows — the MAIN TABLE. A pure association.
                   row_id (POP_#####), state, crop, unique_document_id.
                   No metadata. No links.

unique_documents   8,748 rows — one per distinct document, keyed by sha256.
                   display_id (ANNAM_#####), the 18 metadata fields, page
                   count, language, translation/review, chunk_embeddings [],
                   main_row_ids[], duplicate_links[], merged_from[].

states / crops / languages   controlled vocabularies for the dropdowns.
```

The API serves them joined: `GET /documents` `$lookup`s the document, so the
frontend renders a row without a second request. **Stored split, served
joined.**

## Why two collections

| One flat table | This |
|---|---|
| metadata copied onto every placement — editing "the document" was up to 66 writes | one document row, one write |
| translating a document cost once per placement (the most-placed file has 66) | once per document |
| nowhere for a team-approved duplicate merge to land | `merge`, `merged_from` |
| state/crop/language free text | vocabularies the team picks from |

**Grouped by sha256 at load.** Byte-identical is a fact, not a judgement, so it
needs no approval. Near-duplicates — a re-scan, a re-export — have a different
hash and stay separate until the algorithm proposes them and a person accepts.

### Where the links live

WorkDrive keeps a **separate physical copy of a file in every folder** — it does
not link one file into many. So 9,811 placements are 9,811 distinct Zoho files
over 8,748 distinct sha256, and grouping by hash has to keep every copy's link
or 1,063 files become unreachable.

They live on the document, as `duplicate_links`: one entry per copy, with its
Zoho file id, link, name, and the placement it belongs to. `main_row_ids` is the
same relationship seen from the other side. Both are only ever written by
`link_placement()` / `unlink_placement()` in `dashboard/models.py`, so the load,
the upload and the merge cannot drift apart.

## The row

Exactly the 26 fields specified, and nothing else. `Crop Name` and `State Name`
are on the placement; the other 24 are on the document.

```
documents                            unique_documents
  _id                                  _id
  row_id      int UNIQUE POP_#####     display_id  int UNIQUE ANNAM_#####  ← Document Code
  unique_document_id ──────────────▶   sha256      UNIQUE (partial index)
  state / state_raw   ← State Name
  crop  / crop_raw    ← Crop Name      shareable_name / shareable_link  ← the anchor's
  subpath                              num_pages · format_original
  source_key  (Zoho file id)           language  ← a code from `languages`
  created_at / updated_at              language_source  ← detected | manual | state
                                       advisory_type · advisory_scope · season
                                       edition_revision_volume
                                       date/month/year_of_release
                                       date/month/year_of_collection
                                       advisory_name · advisory_released_org
                                       advisory_org_address · live_source_link
                                       domain · verification_status
                                       verified_by · document_status
                                       translation_* / review_*
                                       chunk_embeddings []
                                       main_row_ids     [ObjectId]
                                       duplicate_links  [{zoho_file_id, shareable_link,
                                                          shareable_name, state, crop,
                                                          row_id}]
                                       representative_file_id / representative_row_id
                                       merged_from      [ANNAM ids]
```

Anything not on that list was removed on 2026-09-07 at the user's instruction:
`size_bytes`, `ocr_language`, `language_note`, `pdf_producer`, `pdf_version`,
`pdf_language`, `zoho_created_at`, `zoho_modified_at`, `source`. Only
`language_source` was kept from that set, because without it an inferred
language is indistinguishable from a measured one. `tests/test_render_main_table.py`
asserts the exact field set on both collections, so a stray field cannot creep
back in unnoticed.

Zoho's created timestamp is still *used* at load — it is where
`date_of_collection` comes from for files no CSV covered — it is simply not
stored.

**Two id series.** ANNAM names the **document**, POP names the **placement**. A
merge repoints a placement and never renumbers it.

**Nothing reads the previous schema.** `legacy_document_code` and the read-only
lookup into `pop_dashboard` that populated it were both removed on 2026-09-07 —
this schema is self-contained, and the loader's only inputs are the crawl and
the CSV dumps.

**The anchor.** `representative_file_id` names which entry of `duplicate_links`
*is* this document, and `shareable_name`/`shareable_link` are that copy's.
Translation acts on the anchor and no other copy. It never moves on a merge, and
a person can re-anchor with `PATCH /unique-documents/{id}` — validated to be one
of that document's own copies. Today every grouped copy is byte-identical so it
changes nothing; the moment the algorithm merges near-duplicates it is what
stops translation working on a re-scan.

**`state_raw` matters.** The OCR language for a document is keyed off the
ORIGINAL state name (`"State Karnataka"`), not the normalised one. Normalising
in place would silently break non-English OCR. `states.raw_names` keeps the same
mapping at the vocabulary level.

**`chunk_embeddings` is always `[]`.** The vectors are computed and live on
local disk (`fix/out/fingerprint_v3_chunks.f32`, keyed by sha256); the cluster
is a 512 MB free tier. The column exists so filling it in later needs no schema
change, and there is no vector index on it.

## Indexes

`documents`: `row_id` UNIQUE, `unique_document_id`, `(state, crop)`, `crop`,
`(created_at DESC, _id ASC)`.

`unique_documents`: `display_id` UNIQUE, **`sha256` UNIQUE**,
`duplicate_links.zoho_file_id`, `translation_status`, `review_status`,
`(created_at DESC, _id ASC)`.

Two things worth knowing:

- `sha256` is unique via a **partialFilterExpression** (`{"sha256": {"$type":
  "string"}}`), not `sparse`. `sparse` only skips a *missing* field — an
  explicit `sha256: null` is still indexed, so several unhashed documents would
  collide. The partial filter is what makes an unhashed document possible.
- The `(created_at DESC, _id ASC)` sort's `_id` tiebreak is load-bearing: the
  whole corpus loads with an identical `created_at`, and skip/limit over a
  non-unique sort repeats and drops rows.

## API

```
GET    /dashboard/documents                     paginated 100/page, joined
GET    /dashboard/documents/{row_id}
GET    /dashboard/documents/{row_id}/siblings   the document's other placements
PATCH  /dashboard/documents/{row_id}            state/crop → row; the rest → document
DELETE /dashboard/documents/{row_id}            the placement only

GET    /dashboard/unique-documents             paginated 100/page, filterable
GET    /dashboard/unique-documents/{id}
PATCH  /dashboard/unique-documents/{id}
GET    /dashboard/unique-documents             paginated 100/page, filterable
GET    /dashboard/unique-documents/{id}/placements

POST   /dashboard/documents/{row_id}/find-duplicates          ← stub, returns []
POST   /dashboard/unique-documents/{id}/find-duplicates       ← stub, returns []
POST   /dashboard/unique-documents/{id}/merge  {"absorb": [...]}

GET    /dashboard/states                        [{name, raw_names, document_count}]
GET    /dashboard/crops[?state=X]
GET    /dashboard/languages                     15: 14 tessdata + Non-English
GET    /dashboard/stats

POST   /dashboard/uploads                       → queue item; poll GET /uploads/{id}
POST   /dashboard/uploads/{id}/add   {"document_id"}  file NEW placements onto it
POST   /dashboard/uploads/{id}/new              create a separate document
POST   /dashboard/uploads/{id}/cancel           discard; nothing uploaded
POST   /dashboard/states  /  /dashboard/crops   add to the vocabulary

POST   /dashboard/documents/{row_id}/translate           ← resolves to the document
POST   /dashboard/unique-documents/{id}/translate
GET    /dashboard/translation-jobs
```

Filtering is whitelisted and split by collection: placement filters
(`state`, `crop`, `row_id`, `created_at`) run **before** the join so the
`(state, crop)` index narrows first; document filters run after it. The page and
its count come from one `$facet`, so the two cannot be filtered differently.

Each whitelisted key names a stored field and a **kind** (`TEXT`, `EXACT`,
`INT`, `DATE`, `DATETIME`, `ANNAM`, `POP`), which is what lets multi-value and
range handling be added once rather than per column:

```
filter[state]=Karnataka,Kerala              any of      (all kinds)
filter[num_pages_min]=10                    range end   (INT: _min/_max)
filter[date_of_collection_from]=2026-08-01  range end   (DATE/DATETIME: _from/_to)
```

Comma rather than a repeated `filter[state]=A&filter[state]=B`: with repeats
Starlette's `query_params.get()` returns only the **last** value, so a
multi-select silently filtered on one value. A range on a text column returns an
empty page rather than being ignored.

`GET /stats` reports `documents` (placements) and `files` (distinct documents)
separately — the gap is the duplication that is already resolved.

### Editing

A PATCH on a row is routed: `state`/`crop` change that placement, everything
else changes the **document** and therefore all of its placements. The caller
does not have to know which is which. Moving a placement also updates its entry
in the document's `duplicate_links`, so the two never disagree about which
folder a copy is in.

`language` must be a code from `GET /dashboard/languages` — free text is
refused, which is the entire reason the vocabulary exists.

### Duplicate review

```
POST /documents/{row_id}/find-duplicates
  → { "candidates": [], "note": "…not connected yet…" }
```

The button's endpoint exists so the frontend can be built now. It returns an
empty list **and says why**, so it never reads as "no duplicates exist". The
seam is `_find_candidates()` in `dashboard/routes_merge.py`: read this
document's chunk vectors from local disk by sha256, compare, return candidates
with a real score. Everything downstream already works.

```
POST /unique-documents/{id}/merge  {"absorb": ["ANNAM_00123", …]}
```

Repoints every placement of the absorbed documents, appends their
`duplicate_links` so no physical copy becomes unreachable, moves their queued
translation jobs, records their ANNAM ids in `merged_from`, then deletes them.
Validated in full before anything is written, so a typo in the fifth id does not
leave the first four already merged.

**A merge never deletes a `documents` row.** Each one is a real file in a real
folder; a merge changes which document a folder entry is understood to hold, not
whether the folder entry exists. `merged_from` plus the carried-over copy links
are what make a bad merge reconstructible by hand.

## Loading the corpus

```
.venv/bin/python3 -m dashboard.migrate_from_corpus --dry-run
.venv/bin/python3 -m dashboard.migrate_from_corpus
```

`--wipe` drops both collections and rebuilds. It is **required** to load over
the previous flat schema: those rows have no `row_id`, so building the unique
index on them fails outright, and a half-converted collection would be worse
than either shape. The drop happens before `init_db()` for the same reason.

**The crawl is the row list.** `fix/out/zoho_crawl.jsonl` is what WorkDrive
actually contains; the CSVs only decorate it. Loading from `report_true.csv`
instead — which an earlier version of this script did — silently drops ~900
files that are in the folders but never made it into the CSV.

- `fix/out/zoho_crawl.jsonl` — **the row list.** Restricted to top-level folders
  named `State *` or `Central Advisories` (`--include-non-state` keeps the rest:
  `Transition1` 432, `Others` 49, `Transition 2` 3, `0Codon Stream` 2,
  `Master Sheet` 1). 9,811 of the crawl's 10,298 files.
- `results/state_language_report/report_true.csv` — 8,934 rows, joined on
  **(state, folder, filename)**. Supplies `sha256`, language, page count and the
  collection date. Matches 8,913 of the 9,811; only 6 of its own placements are
  missing from the crawl.
- `fix/out/zoho_metadata.json` — 7,931 entries by sha, re-indexed here by
  `file_id`; reliable page counts and sizes.
- `results/pops.csv` — 7,513 rows by sha. **Its 18 manual metadata columns are
  empty for every row** — they are what humans fill in through the dashboard,
  and no corpus pass ever wrote them. It contributes shareable link and
  translation/review status.
- `fix/out/backfill_new_rows.jsonl` and `fix/out/zoho_file_metadata_by_id.json`
  — overlaid **last, keyed by Zoho file id**. A file id identifies exactly one
  physical copy while `(state, folder, filename)` does not, so where a folder
  holds two files with the same name the name join can only match the first.
  These two files are the authority for what they cover, and they are what
  closed the last 20 gaps.
Grouping: rows are keyed by `sha256`; a file with no hash gets its own document
keyed by its Zoho file id, because it cannot be proven identical to anything and
must not be silently pooled with other unhashed files. Where several placements
of one document disagree about a field, the one with the most values filled in
supplies the metadata — "the placement that knows most" beats "whichever came
first".

Loaded state: **9,811 placements, 8,748 documents, 33 top-level folders
(32 states + Central), 473 crops, POP_00000–POP_09810,
ANNAM_00000–ANNAM_08747, 15.8 MB.** 390 documents have more than one placement;
the most-placed has 66.

## Fresh crawl and reconcile

```
.venv/bin/python3 scripts/crawl_zoho_root.py            # uses ZOHO_CORPUS_FOLDER_ID
.venv/bin/python3 -m dashboard.migrate_from_corpus --reconcile-only
```

The corpus root is **"Repository of Advisories - POP Bank"**
(`ZOHO_CORPUS_FOLDER_ID`), layout `<state>/<crop>/<file>`. Note this is **not**
`ZOHO_ROOT_FOLDER_ID`, which points at the translation pipeline's small working
area (~80 files) — the pipeline depends on that var, so a separate one was
added rather than repointing it.

**Two levels only.** The root holds every state plus `Central Advisories`;
each of those holds folders and/or loose files; those files are the catalogue.
Nothing below that is listed (`--max-depth`, default 2), and `Multiple Uses
File`-style folders are skipped (`--include-shared-store` keeps them). Both
skips are counted and reported at the end of a run rather than being silent.

That is a decision about scope, and it has a price worth knowing: the two skips
together leave **7,014 files** unlisted — 5,135 in shared-store folders and
1,879 below depth 2.

**A third level is rare, not the norm.** `Central Advisories` holds 183 folders;
92 of them contain a subfolder at all, and **73 of those 92 contain only a
`Multiple Uses File` folder** — a parking spot, not a crop level. Just **19**
have a genuine subfolder, and one of them, `National Horticulture Board (NHB),
Gurugram, Haryana`, accounts for 34 crop folders and 377 of the 398 deep files
under Central Advisories. So the "organisation at level 2, crop at level 3"
shape is essentially **one folder**, not a pattern — clicking into Central
Advisories at random will almost always show a flat folder of PDFs.

Of the 1,879 deep files, only **1,167 are inside a `State *` or
`Central Advisories` folder** and therefore in scope at all; the largest single
block is 563 under `Transition 2`, which this load excludes anyway. The in-scope
remainder is concentrated: Central Advisories 398 (377 of them NHB),
`State Uttar Pradesh` 255, `State Tamilnadu` 159, `State Karnataka` 115.
Raise `--max-depth` to 3 to pick them up.

**The crawl is slow and that is not fixable.** ~1,350 folder listings, and
WorkDrive answers a burst with HTTP 429 and `Retry-After` of ~900 s, so a full
sweep spans hours. The crawler therefore banks every folder listing to
`fix/out/zoho_crawl_cache.jsonl` the moment it succeeds and replays that cache
on a re-run — no successful call is ever paid for twice, and a kill costs
nothing. It runs 4-wide rather than 16 for the same reason.

`--reconcile-only` **reports only, never writes**. Now that the crawl is the row
list it can no longer find "files we missed"; it prints the two deliberate gaps
instead — crawl files excluded for being outside a state/central folder, and
`report_true.csv` placements the crawl does not have (6, likely moved or
deleted; written to `fix/out/reconcile_missing_from_workdrive.csv`).

### Why the crawl, not the CSV

At `--max-depth 2` the crawl lists **10,298 files across 38 top-level folders**;
`report_true.csv` has 8,934 rows over **7,931 distinct Zoho file ids**. Only
7,929 ids are common, so the crawl holds **2,369 files the CSV has no id for
anywhere**.

The reason is how WorkDrive stores duplication: **every crawled file id is
distinct** — no id appears in two folders, because WorkDrive keeps a separate
physical copy per folder. The CSV's 8,934-rows-over-7,931-files ratio came from
it listing one document under several crops while the metadata join gave all
those rows the *same* file id, whichever single copy it matched. That is also
why 877 of the old rows pointed at a file the crawl sees in a different folder
than the row claimed (800 same state, 74 a different state) — not wrong about
the document, only about which physical copy backed it.

Of the 2,369, **553 carry a filename the CSV already has** (a further physical
copy) and **1,816 have filenames it has never seen**. 487 sit in the non-state
top-level folders this load excludes; 23 sit loose directly under a top-level
folder with no crop folder at all (10 of those inside state/central, which load
with `crop: ""`).

## Translation

Per **document** now, not per placement — that is the change that stops paying
66× to translate one file. `POST /documents/{row_id}/translate` resolves the row
to its document and delegates, so the button can live on a main-table row while
the work and the cost stay per document.

Which physical file gets translated: a document owns one copy per folder, all
byte-identical by construction, so the first entry in `duplicate_links` is taken
(`_source_file_id()`).

Otherwise unchanged — it reuses `pop_server._run_one_doc`, gated on `TRANS=on`
plus an LLM API key.

## Tests

```
.venv/bin/uvicorn pop_server:app --host 127.0.0.1 --port 8047
POP_RENDER_BASE_URL=http://127.0.0.1:8047 .venv/bin/python3 -m pytest tests/test_render_main_table.py -v
```

75 tests. Self-contained — deliberately does not use `tests/conftest.py`, which
pins port 8032 and the previous schema's database, so both suites can run at
once.

## Later: embeddings

Deferred by design, not forgotten. `chunk_embeddings` is a real column on every
document and is always `[]`. When the grouping algorithm lands, the vectors stay
**on local disk** (`fix/out/fingerprint_v3_chunks.f32`) and join by `sha256`;
the cluster cannot hold them. It is deliberately not serialised by any endpoint:
a page of 100 rows must not carry thousands of floats each.

The algorithm's landing point is `_find_candidates()` in
`dashboard/routes_merge.py` — one function. The endpoint, the response shape,
the merge and its audit trail already work and are tested.

## Not done

- **Real language detection.** Every non-English value is inferred from the
  state, not measured (see `language_source`). `scripts/state_language_report.py`
  does the real two-tier pass — the PDF's `/Lang` attribute first, then OCR the
  middle page with the English-only Tesseract model — and would replace the
  inference with a verdict for any subset worth the OCR time. The Central
  Advisories block is where it would pay most.
- **1,033 files one level deeper than the crawl looks** (`Private Product`,
  the NHB crop folders, KVK folders, the UP Paddy programme codes). See the
  crawl section. `--max-depth 3` picks them up, but the shared-store regex has
  to be fixed first or ~1,000 `Used in <State> <Crop>` folders arrive as crops.
- **5,135 files in `Multiple Uses File` folders**, skipped by decision.
