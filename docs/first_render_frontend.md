# first_render frontend — API contract and interaction notes

Branch `first_render`. Written 2026-09-07, to be handed to a frontend session.

> **This is NOT `docs/dashboard_frontend_plan.md`.** That file belongs to the
> other workstream and describes the *previous* schema (`pop_dashboard`, integer
> state/crop ids, `document_associations`, 7,361 unique documents). Nothing in it
> applies here. This backend's collections all live under the `pop_` prefix, in
> whichever database `POP_ENV` selects (`STAGING_DB_NAME`, shared with another
> application entirely, or `PROD_DB_NAME`).

Base URL: everything below is under `/dashboard`. Ids in URLs are 24-character
MongoDB ObjectId hex strings — opaque, pass them back unchanged. The ids shown to
people are different and are described next.

## The two things on screen

There are **placements** and there are **documents**, and the difference decides
what every screen does.

```
POP_00342   a PLACEMENT — one document, filed under one state and one crop.
            This is a row of the main table.
ANNAM_00321 a DOCUMENT — the file and all its metadata.
            One document can be filed in many places; the record-holder is
            filed in 66.
```

Editing a **placement** field (state, crop) changes that row. Editing anything
else changes the **document**, and therefore every row that uses it — a person
editing "Season" on one row is editing it for all 66. **Say so in the UI.**
`placement_count` on every row is how you know when to warn.

Live data to build against: **9,811 placements over 8,748 documents**, 33 states,
473 crops, 15 languages. 390 documents have more than one placement.

## Main table

```
GET /documents?page=1&filter[state]=Karnataka&filter[language]=kan
```

100 per page, `{items, total, page, page_size}`. A row arrives with its document
already joined — no second request to render the table:

```json
{ "id": "6a9da0756e28d244c1ad5916",
  "row_id": "POP_00342",
  "state": "Central", "crop": "ICAR - Indian Council Of Agriculture Research",
  "subpath": "",
  "unique_document_id": "6a9da05a6e28d244c1ad36d5",
  "document_id": "ANNAM_00321",
  "shareable_name": "Indian Horticulture-Global Potato Conclave-2020_ICAR.pdf",
  "shareable_link": "https://workdrive.zoho.in/file/vn19v641…",
  "sha256": "ad654501…", "num_pages": 78, "format_original": "pdf",
  "language": "eng", "language_source": "detected",
  "translation_status": "not_started", "review_status": "not_started",
  "placement_count": 3,
  "created_at": "…", "updated_at": "…" }
```

**Filters** are `filter[<name>]=<value>`, whitelisted, combinable, all AND-ed.

Three forms, per key:

```
filter[state]=Karnataka                 match
filter[state]=Karnataka,Kerala          any of  ← multi-select
filter[num_pages_min]=10                range end
filter[num_pages_max]=20
filter[date_of_collection_from]=2026-08-01
filter[date_of_collection_to]=2026-08-31
```

**Multi-select is comma-separated, not a repeated param.** A repeated
`filter[state]=A&filter[state]=B` keeps only the **last** value — that was the
multi-select bug. Whitespace and a trailing comma are tolerated.

`filter[state]` / `filter[crop]` match names (case-insensitive, substring, and
also each entry's other spellings — `filter[crop]=Black Pepper` finds `Pepper`).
`filter[crop]` covers the whole Crop column: crops **and** organisations.
**Many organisation names contain commas** (`"Ministry of Jal Shakti, Government
of India"`), and those split — so dropdown filters should send ids instead:
`filter[state_id]`, `filter[crop_id]`, `filter[organization_id]` (exact, comma =
any of). `filter[crop_kind]=crop|organization` narrows to one kind.

**Ranges**: `_min`/`_max` on numeric keys, `_from`/`_to` on date keys. Either end
may be omitted. Dates are `YYYY-MM-DD`. A range on a text column returns an empty
page rather than being ignored, so a wrong key is visible.

| on the placement | on the document |
|---|---|
| `row_id`¹ · `state` · `crop` · `created_at`² | `document_id`¹ · `sha256` · `shareable_name` · `shareable_link` · `language`³ · `language_source`³ · `format_original`³ · `translation_status`³ · `review_status`³ · `num_pages`⁴ · `advisory_type` · `advisory_scope` · `advisory_name` · `advisory_released_org` · `advisory_org_address` · `edition_revision_volume` · `live_source_link` · `season` · `domain` · `verification_status` · `verified_by` · `document_status` · `date_of_release`² · `month_of_release`⁴ · `year_of_release`⁴ · `date_of_collection`² · `month_of_collection`⁴ · `year_of_collection`⁴ |

¹ exact, accepts `ANNAM_00042`/`POP_00042` or the bare number  ² date, supports
`_from`/`_to`  ³ exact  ⁴ numeric, supports `_min`/`_max`. Everything else is
case-insensitive substring.

> **The three `*_of_release` fields are empty on all 8,748 documents.** No corpus
> pass ever wrote them; they are filled in by hand through the dashboard.
> Filtering on one is valid and returns nothing — that is correct, not a broken
> filter. Don't spend time debugging it.

A filter value that cannot possibly match (a non-numeric `num_pages`, a
malformed date) returns an empty page, not an error.

```
GET    /documents/{id}                 one row
GET    /documents/{id}/siblings        the document's OTHER placements
PATCH  /documents/{id}                 see below
DELETE /documents/{id}                 removes the PLACEMENT only
GET    /stats                          {documents, files, states, crops, translated, reviewed}
```

`DELETE` never deletes the document, even when removing its last placement — its
metadata and any translation survive. Do not label it "delete document".

### PATCH routes itself

Send any mix; the backend decides where each field lands.

```jsonc
PATCH /documents/{id}
{ "crop_id": "<id>",       // → this row only; or "organization_id", or a name:
                           //   "crop" must be a master crop, "organization" is created if new
  "season": "Kharif",      // → the DOCUMENT, so all its placements
  "language": "kan" }      // → the document; must be a code from /languages
```

`language` is validated against the vocabulary — a free-typed value is a 400.
Setting it also stamps `language_source: "manual"`, which protects it from the
state-inference pass (below).

## Documents tab

```
GET /unique-documents?page=1&filter[language]=kan&filter[multi_placement]=true
```

The counterpart of `GET /documents`, same envelope, 100/page. **8,748 rows**
against the main table's 9,811 — one per document rather than one per placement.
Every field of the document is on the row, so a 24-column table needs no second
request.

Do **not** try to build this by de-duplicating the main table client-side.
Pagination is server-side, so a page of 100 placements collapses to an
unpredictable number of documents and the total is simply wrong.

Filters are the document-level names from the main table's list —
`document_id`, `sha256`, `shareable_name`, `language`, `language_source`,
`num_pages`, `format_original`, `translation_status`, `review_status`, the
metadata fields — and they mean the same thing from either tab. Plus one that is
only meaningful here:

```
filter[multi_placement]=true     the 390 documents filed in more than one place
filter[multi_placement]=false    the 8,358 filed in exactly one
```

That is the duplication the sha256 grouping already resolved, and the natural
starting point for a review pass.

```
GET    /unique-documents/{id}
PATCH  /unique-documents/{id}
GET    /unique-documents/{id}/placements     every row using it
DELETE /unique-documents/{id}                the destructive one -- see below
```

**`DELETE /unique-documents/{id}` deletes everything.** The document, every one
of its placements, and every file it owns — each copy in `duplicate_links`, the
translation and the review — removed from WorkDrive as well. It is not the
placement-only `DELETE /documents/{id}`; do not describe the two the same way.

For a crawled document those copies are the files in the shared WorkDrive
repository, so this removes them from there. Zoho's delete is a move to trash
rather than a purge, so it can be undone from WorkDrive's own trash — but
nothing in this dashboard can undo it. **Confirm with the row count and the
file count spelled out** (`placement_count` and `duplicate_links.length` are
both on the document you already fetched).

Two responses to handle: **409** while a translation job for that document is
queued or running (cancel it first), and **502** if WorkDrive refuses to delete
a file — the detail names the ids, and nothing at all was removed, so the call
can simply be retried.

`GET` returns all 24 document fields plus:

```json
{ "placement_count": 3,
  "representative_file_id": "vn19v641…",
  "representative_row_id": 343,
  "duplicate_links": [
    { "zoho_file_id": "ifu0n51…", "shareable_link": "https://workdrive.zoho.in/file/ifu0n51…",
      "shareable_name": "Global Potato Conclave – 2020 - ICAR-CPRI.pdf",
      "state": "Central", "crop": "ICAR-Central Potato Research Institute…",
      "row_id": 341 } ],
  "merged_from": [] }
```

**`duplicate_links` is every physical FILE of this document in WorkDrive** — not
one per placement. A file is listed exactly once.

An entry's `state`/`crop` are read from the placement named by its `row_id`, so
they are the same names the main table shows (`"Rajasthan"`) — they used to be
the raw folder spelling (`"State Rajasthan"`), and they follow a rename.

For the crawled corpus the two counts coincide: WorkDrive stores a copy per
folder rather than linking one file into many, so 9,811 placements really are
9,811 separate files, and each entry's `row_id` lets a row open *its own* copy.

A dashboard upload is the other case. It writes **one** file into one flat
folder and then files it in several places, so `duplicate_links` is shorter than
`placement_count` — an upload filed in two places has one copy, not two. So:

- **Never assume `duplicate_links.length === placement_count`.** `len(links) <=
  placement_count` is the only guarantee.
- **Hide the anchor control when there is one copy.** With a single file there
  is nothing to choose, and offering the choice reads as a button that does not
  work. `representative_file_id` is that file.
- The `state`/`crop` on an entry is the placement it was first filed under. For
  a shared file that names one of several places, not where the file "is" —
  it is in the dashboard's flat folder, not in a state/crop folder at all.
- The anchor is always one of the listed copies: `representative_row_id` matches
  some entry's `row_id`, so you can mark it in the list.

**The anchor.** `representative_file_id` names which copy *is* the document.
`shareable_name`/`shareable_link` on the document are the anchor's, and
translation always acts on it. A person can re-anchor:

```jsonc
PATCH /unique-documents/{id}   { "representative_file_id": "ifu0n51…" }
// 400 unless that file is one of this document's own copies
```

## Uploading

Two phases with a person in the middle. **Nothing reaches WorkDrive until they
decide**, so cancelling is free.

```
POST /uploads
  → queued → hashing → checking_duplicate → awaiting_review   (waits)
      ↓
  POST /uploads/{id}/add    file the NEW placements onto an existing document
  POST /uploads/{id}/new    create a separate document
  POST /uploads/{id}/cancel discard
```

Poll `GET /uploads/{id}`. `status` and `progress_pct` drive the box; `note` is a
sentence you can show verbatim.

### The form

`multipart/form-data`. Required: **file**, **language**, and **at least one
state with at least one crop**. Everything else is optional.

Placements are **per-state crop groups** — a state, then that state's crops, then
another state:

```
placements_json = [ {"state": "State Karnataka",
                     "crop_ids": ["<master id>"],                 // or "crops": ["Paddy"]
                     "organization_ids": ["<id>"]},               // or "organizations": ["ICAR - ..."]
                    {"state": "State Kerala", "crops": ["Coconut"]} ]
```

Take the options from `GET /folders?advisory_type=<form's Advisory Type>&state=<group's state>`
(see *The Folder dropdown follows the advisory type*). Each group needs at least
one entry across the four lists. A name under
`"crops"` must be a crop master crop (an existing organisation's name is also
accepted there) — anything else is a **400** telling the user to pick from
`/crops` or send it as an organisation. `"organizations"` may introduce a new
one; it is created when the upload is filed. Prefer ids from the dropdowns.

`states_json` + `crops_json` are still accepted and mean the cross product of the
two; use them only when every state really does get the same crop list.

Pairs are normalised and de-duplicated, and resolved against the existing
vocabulary — `"state karnataka"` and `"State Karnataka"` become the one
placement, spelled the way the vocabulary already spells it.

Optional, all as plain form fields: `advisory_type`, `advisory_scope`, `season`,
`edition_revision_volume`, `date_of_release`, `month_of_release`,
`year_of_release`, `date_of_collection`, `month_of_collection`,
`year_of_collection`, `advisory_name`, `advisory_released_org`,
`advisory_org_address`, `live_source_link`, `domain`, `verification_status`,
`verified_by`, `document_status`.

**A crop that does not exist yet is fine** — type it and it joins the vocabulary.
`POST /crops {"name": "..."}` creates one ahead of time; `POST /states` likewise.

### The decision screen

At `awaiting_review` the item carries up to **three candidate documents**, best
score first:

```json
{ "status": "awaiting_review",
  "sha256": "…", "num_pages": 1,
  "placements": [{"state": "Karnataka", "crop": "Paddy"},
                 {"state": "Karnataka", "crop": "Maize"}],
  "candidates": [
    { "document_id": "6a9d…", "document_code": "ANNAM_08748",
      "shareable_name": "…", "score": 1.0, "match_type": "sha",
      "placement_count": 3,
      "new_placements": [{"state": "Karnataka", "crop": "Maize"}],
      "can_add": true, "can_create_new": false } ],
  "note": "This exact file is already in the catalogue as ANNAM_08748, filed in 3 place(s). Adding files it under 1 new place(s): Karnataka/Maize." }
```

Render one row per candidate, let the person pick one, then offer only the
actions that candidate allows:

| | show | why |
|---|---|---|
| `can_add` | **Add** | this upload names a place the document is not in yet |
| `can_add: false` | hide Add | it is already filed everywhere you selected — adding would create nothing |
| `can_create_new` | **New** | a separate document is a legitimate answer |
| `can_create_new: false` | hide New | **exact byte match** — `sha256` is unique, a second document *cannot be stored* |
| always | **Discard** | |

`candidates: []` means nothing matched → offer **New** and **Discard** only.

```jsonc
POST /uploads/{id}/add   { "document_id": "ANNAM_08748" }   // or the ObjectId hex
POST /uploads/{id}/new                                       // no body
POST /uploads/{id}/cancel                                    // 204
```

`document_id` may be omitted when there is exactly one candidate. The backend
enforces the same rules the flags describe, so a wrong action is a 409 with a
readable `detail` — but do not rely on that for the happy path.

**A finished item deletes itself.** The queue holds work that is in flight or
waiting on a person; a completed upload is neither. So there is no `done` row to
show or clear — when the decision's work lands, the item is gone from
`GET /uploads` and `GET /uploads/{id}` returns 404. **That disappearance is the
success signal**: poll the list, and when an item vanishes, refetch the main
table (it sorts `created_at DESC`, so the new rows are at the top). Do not wait
for `status: "done"` — it no longer arrives.

Failed items are the exception and stay in the queue: they carry
`error_message`, and `add`/`new` on them is a retry. `cancel` deletes as before.

**`add` uploads nothing.** The document already has the file, and dashboard
uploads live in one flat WorkDrive folder rather than one per state/crop — so
filing it somewhere else is rows only. It is fast; do not show an upload bar.

> **What the check does NOT do yet.** It compares `sha256` and nothing else, so
> `candidates: []` means *"not this exact file"*, never *"not a duplicate"*. A
> re-scan or re-export of a document already in the catalogue looks completely
> new. Word the empty state accordingly. When the chunk-match algorithm lands,
> the same field fills with up to three real scores and nothing else here
> changes.

## Duplicate review, outside upload

```
POST /documents/{row_id}/find-duplicates          the button on a main-table row
POST /unique-documents/{id}/find-duplicates
```

Returns `{document_id, candidates: [], note}`. **Always empty today** — the
algorithm is not connected. `note` explains that; show it rather than "no
duplicates found", which would be a false statement.

```jsonc
POST /unique-documents/{id}/merge   { "absorb": ["ANNAM_00123", "ANNAM_00456"] }
→ { "document_id": "ANNAM_00321", "absorbed": ["ANNAM_00123"],
    "placements_repointed": 4, "placement_count": 7 }
```

The absorbed documents are deleted; their placements are **repointed, never
deleted**, and their copies join the survivor's `duplicate_links`. The survivor's
anchor does not move. `merged_from` is the audit trail.

## Lookups

```
GET    /states                            [{id, name, raw_names, document_count}]
GET    /crops[?state=X|?state_id=]        same shape — crop master crops, read-only
GET    /organizations[?state=X|?state_id=] same shape
GET    /folders?advisory_type=X[&state=|&state_id=]  [{id, name, kind, raw_names, document_count}]
GET    /languages                         [{code, label, tessdata_best}] — 24

POST   /states          {"name": "..."}   idempotent → the entry (201)
POST   /organizations   {"name": "..."}   idempotent → the entry (201)
PATCH  /organizations/{id}        {"name": "..."}          rename → the entry
POST   /organizations/{id}/merge  {"absorb": ["<id>", ...]} → {id, name, absorbed, placements_repointed}
DELETE /organizations/{id}                                 204, or 409 while in use
        (the same three for /states/{id})

POST / PATCH / merge / DELETE on /crops → 403 — crops are maintained elsewhere
```

**The folder under a state is a crop OR an organisation.** Every placement
stores `state_id` plus exactly one of `crop_id` (an entry in the **crop master**)
or `organization_id` (an organisation, department or grouping — `"ICAR - Indian
Council Of Agriculture Research"`, `"General"`, `"Pulses"`).

The row still has one **`crop`** field holding whichever name applies, plus
`crop_kind: "crop" | "organization"`, `crop_id` and `organization_id` (one of
the two is null).

**Label that column "Folder", not "Crop"** — the table reads *State / Folder*,
because the value is a crop or an organisation. Only the UI label changes: the
API field is still `crop`, and its filters are still `filter[crop]`,
`filter[crop_id]`, `filter[organization_id]`, `filter[crop_kind]`. `crop_kind`
is there if you want to badge organisations.

### The Folder dropdown follows the advisory type

Wherever a Folder is picked — the **Add Document** form and the table's
**Folder column filter** — the options depend on the Advisory Type:

| Advisory Type | Folder options |
|---|---|
| Comprehensive | crops (crop master) |
| Crop Advisory | crops (crop master) |
| Non-Crop Advisory | organisations |
| General | everything — crops and organisations |
| blank | everything |

Don't reimplement the rule: **`GET /folders?advisory_type=<the selected type>`**
returns exactly those options, each with its `kind`. Send an option's `id` as
`crop_ids`/`organization_ids` (upload) or `crop_id`/`organization_id` (PATCH,
filters) according to `kind`. Add `&state=`/`&state_id=` to narrow to folders
used under a state, as in the upload form. The type is matched on letters only,
so `"Crop Advisory"` and `"crop-advisory"` are the same; anything unrecognised
behaves like General. When the Advisory Type changes in the form, re-fetch and
clear a selected folder that is no longer offered. In the table, filter the
Folder options by the Advisory Type column filter when one is set, otherwise use
General.

**Every existing document has Advisory Type `General`** (all 8,748, set
2026-09-15), so by default every folder is offered.

One option has an empty `name`: the organisation for the 10 files sitting
directly in a state folder with no crop folder. Render it as "(no folder)".

The backend does not refuse a folder that does not match the advisory type —
the rule lives in the dropdown.

**Crops are read-only.** They come from the crop master, which another
application edits. There is no add/rename/merge/delete for crops here — hide
those controls. Pesticides the master also lists are never returned.

**States and organisations are ours and editable:**

- **Rename** changes the name on every row at once, stored exactly as sent. The
  old name is kept in `raw_names`. Renaming onto a name another entry already has
  (any letter case) is a **409 whose message says to merge instead**.
- **Merge** moves the absorbed entries' placements (and any pending uploads) to
  the survivor, keeps their names in its `raw_names`, and deletes them. No row is
  deleted.
- **Delete** only succeeds for an entry nothing uses; otherwise 409 naming how
  many placements and pending uploads still use it — offer merge.
- Names are **unique regardless of letter case**; posting `"icar - x"` returns the
  existing `"ICAR - X"`. Don't dedupe on the client.

For every lookup, `document_count` is computed on read, and `raw_names` are other
spellings that resolve to the entry — for a state its original folder name
(`"State Karnataka"`), for a crop our older names before the master
(`"Ground Nut"` → `Groundnut`). Fine as "also known as" on a management screen;
not in dropdowns.

**Language is a dropdown, never free text.** English, the 22 Eighth Schedule
languages (`tessdata_best` says which can be OCR'd) plus `non_english`. Two fields matter together:

- `language` — the code.
- `language_source` — a closed vocabulary of three. `detected` (5,183: **known**
  — the OCR pass read it off the file), `manual` (1: **known** — a person chose
  it on upload) and `state` (3,565: **inferred from the state, plausible but not
  measured**). Only `state` means "a guess"; the other two differ in where the
  answer came from, not in how much to trust it. Setting a language through
  PATCH flips the row to `detected` automatically.

**Give the team a way to filter `language_source=state`.** Those 3,565 are
guesses and are the review queue — 2,462 of them are `Central Advisories`
documents assigned Hindi, many of which are really English. A "Language"
column that does not distinguish a guess from a measurement will be trusted, and
it should not be.

## Downloading a file

**Do not link the browser at `workdrive.zoho.in` directly** — cross-origin, and a
plain `<a download>` is ignored for it. The backend proxies instead, and it sends
`Access-Control-Allow-Origin: *`:

```
GET /dashboard/files/{zoho_file_id}/download    attachment
GET /dashboard/files/{zoho_file_id}/link        inline (view in a tab)
```

### The three file ids

Every downloadable file is a Zoho file id fed to that same proxy. There are
three per document, and all three are on **both** the main-table row and the
unique document, so the icons render without a second fetch:

| field | the file | non-null when |
|---|---|---|
| `representative_file_id` | the original (the anchor copy) | always |
| `translation_file_id` | the translated copy | `translation_status: "done"` |
| `review_file_id` | the reviewed copy | `review_status: "done"` |

```js
const fileId = kind === "translation" ? row.translation_file_id
             : kind === "review"      ? row.review_file_id
             :                          row.representative_file_id;
if (fileId) open(`/dashboard/files/${fileId}/download`);
```

Every other copy of the document is in `duplicate_links[].zoho_file_id`.

`translation_shareable_link` / `review_shareable_link` are also returned, but
they are raw `workdrive.zoho.in` URLs — they open Zoho's own viewer and demand a
Zoho login, so they are **not** what a download button should use. Use the
`*_file_id` fields and the proxy. (Until 2026-09-07 the two `*_file_id` fields
were stripped from responses as "internal", which is why translation and review
downloads did nothing at all while the original worked.)

### Large files

Purely server-side; no frontend change is needed, and in particular **do not add
a longer fetch timeout** — that was not the problem.

Zoho caps throughput at ~2.2-2.5 MB/s *per TCP connection*, so the old
single-connection pass-through made a few hundred MB crawl and then die
mid-transfer, with no `Content-Length` for the browser to show progress against.
The proxy now probes the size up front and assembles the body from 8 concurrent
Range requests. What that changes for you:

- `Content-Length` is always set, so a progress bar is real rather than a spinner.
- `Accept-Ranges: bytes` is sent, and a `Range` request returns `206` with
  `Content-Range` — so a browser download resumes instead of restarting.
- The `Content-Type` is the file's real type (`application/pdf`), not
  `application/octet-stream`, so `?inline=1` previews in a tab correctly.
- A Zoho-side failure is now `502`, and an unknown id `404`, instead of a 500
  with a traceback.

A 3.6 MB PDF served `206 … content-range: bytes 0-1023/3599161` on a 1 KiB Range
request, so the mechanism is confirmed live.

## Translation

```
POST   /documents/{row_id}/translate       → {"job_id": "…"}    202
GET    /translation-jobs[?status=running]
POST   /translation-jobs/{id}/cancel
DELETE /documents/{row_id}/translation
POST   /documents/{row_id}/translation     multipart, a translation made elsewhere
POST   /documents/{row_id}/review          multipart, a reviewed DOCX
GET    /config                             {"translation_available": bool}
```

```
DELETE /translation-jobs/{job_id}          // 204; 409 while queued/running
```

The queue is a view of what is translating **now**, not a record of what was
translated — that lives on the document (`translation_status`,
`translation_file_id`) and `DELETE` does not touch it. So give each stopped job
(`done`, `failed`, `cancelled`) a remove button. A `queued` or `running` job is
refused with 409: cancel it first, or the worker would go on writing progress to
a row that no longer exists. The default listing shows only `queued`/`running`,
so pass `?status=done` for the ones a person can clear.

**Jobs are per DOCUMENT, not per placement.** The row-addressed routes resolve to
the document and delegate; `/unique-documents/{id}/translate` is the same thing.
Translating from any of a document's 66 rows translates it once, and all 66 rows
then show `translation_status: "done"`. Make that obvious, or people will queue
it 66 times.

Hide the button when `/config` says `translation_available` is false.

**A translation can also be attached by hand.** `POST .../translation` takes a
file the same way review does, and reaches the same end state the pipeline
does — the file lands in the translations folder, and the document gets
`translation_status: "done"` plus `translation_file_id`. It answers "we compared
two translations elsewhere and want to keep this one". It returns
`{"translation_file_id": "…"}`.

Two rules on it: it is **409 while a job for that document is queued or
running** (that job would overwrite the upload when it finished — cancel it
first), and replacing an existing translation deletes the old file from
WorkDrive, so a document does not collect copies nobody can reach.

When a job finishes, the document's `translation_status` becomes `"done"` and
`translation_file_id` is filled in — poll `/translation-jobs` or refetch the row,
then render the download icon from that id (see "The three file ids").
`POST /documents/{row_id}/review` returns `{"review_file_id": "…"}` for the file
it just stored, and sets `review_status` to `"done"` on the document.

## Things that will bite

1. **`document_id` and `row_id` are different ids.** `ANNAM_08748` is a
   document; `POP_09814` is a placement. They are not interchangeable and their
   numbers are unrelated.
2. **`files` in `/stats` means documents, `documents` means placements.** Label
   them for humans; the raw key names read backwards.
3. **`sha256` is unique per document** — that is what makes `can_create_new`
   false on an exact match. It is a storage constraint, not a preference.
4. **`chunk_embeddings` is never returned** by any endpoint and is always empty.
   Ignore it.
5. **`subpath`** is always `""` today. Reserved for a WorkDrive folder level
   deeper than state/crop; show it only if non-empty.
