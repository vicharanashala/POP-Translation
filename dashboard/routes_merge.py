"""Duplicate review: propose, then merge on a person's say-so.

The find-duplicates button, the merge, and taking files back out of a
document's duplicate_links (delete one copy; unlink a merged group).

    POST /documents/{row_id}/find-duplicates
        The "find duplicates" button. Returns an EMPTY candidate list and says
        why -- the chunk-match algorithm is not wired up here. It exists now so
        the frontend can be built against its real shape, and so the seam is
        obvious when the algorithm lands: fill in `_find_candidates()` and
        nothing else moves.

    POST /unique-documents/{id}/merge
        The team's decision. Absorbs the named documents into this one.

WHAT A MERGE DOES, precisely (the team's rule, 2026-10-17). Document B is
merged into document A:

  1. PLACEMENTS. Each of B's placements whose state + folder A already has is
     DELETED -- A is filed there already, and a second row would show the same
     document twice in one folder. Every other one is MOVED to A and keeps its
     POP id. Two merged documents filed in the same new folder: the first
     moves, the second is dropped.
  2. FILES. Every one of B's copy links joins A's `duplicate_links`, tagged
     with `merged_document` (B's file name -- not its ANNAM id, because ANNAM
     numbers are handed out again once B is gone), `merged_by` and `merged_at`.
     Nothing is deleted from WorkDrive: the raw files are the audit trail.
     B's fingerprint goes on each of B's copies (`merged_sha256`) and, with
     any B had collected, into A's `merged_sha256s` -- but only a fingerprint
     some ADDED copy carries, so the list never holds one nothing shows; so the upload duplicate
     check still finds A when B's file is uploaded again. A second merged
     document with the same name is labelled "<name> (2)". A
     link B had itself received in an earlier merge keeps ITS tags, so a chain
     of merges still groups each file under the document it came from. The
     merged entries name no placement (row_id null) -- placements and files are
     shown separately, and an entry naming a moved row would be pulled or
     re-pointed by unlink_placement as though it were the row's own copy.
     A's `representative_file_id` is not touched: translation keeps acting on
     A's own bytes.
  3. TRANSLATION AND REVIEW, as a PAIR. A review is a review of one particular
     translation, so the two never travel separately. If A has neither, it
     takes the first merged document's pair that has one; every other merged
     translation and review -- and the finished pages of a failed translation
     job -- go to the WorkDrive trash.
  4. B and its translation jobs are deleted from the database.

Refused with 409, before anything is written, while A or any B has a
translation queued or running -- the job would land on a document that no
longer exists, or into the middle of a merge.

`merged_from` (ANNAM ids) is gone. It was never filled on either database, and
a reused ANNAM number would have made it name a different, live document
(UM-007). `duplicate_links` is the record now.

The WorkDrive trash comes LAST and is best-effort: by then the database is
consistent, so a file Zoho refuses is only left over, reported in
`not_trashed`, rather than a merge half done.
"""
from __future__ import annotations

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException

from dashboard.access import Caller, require_editor
from dashboard.db import get_db
from dashboard.display_id import allocate_and_insert, format_display_id, parse_display_id
from dashboard.models import (
    COLL_DOCUMENTS, COLL_TRANSLATION_JOBS, COLL_UNIQUE_DOCUMENTS, MANUAL_METADATA_FIELDS,
    new_copy_link, new_unique_document, utcnow,
)
from dashboard.schemas import (
    DuplicateCandidatesOut, MergeRequest, MergeResult, UniqueDocumentOut, UnlinkRequest,
)

router = APIRouter()

_NOT_IMPLEMENTED_NOTE = (
    "The duplicate-finding algorithm is not connected yet, so this is an empty "
    "list rather than a statement that no duplicates exist. Byte-identical "
    "files are already grouped -- every placement of the same bytes shares one "
    "document. What is still to come is near-duplicates: a re-scan or a "
    "re-export of the same advisory, which has a different sha256 and will be "
    "found by comparing chunk embeddings."
)


def _resolve_document(db, value: str) -> dict | None:
    """Accept either an ObjectId hex or an ANNAM id, because the team works in
    ANNAM ids and the frontend works in ObjectIds."""
    display_id = parse_display_id(value)
    if display_id is not None and not _looks_like_object_id(value):
        return db[COLL_UNIQUE_DOCUMENTS].find_one({"display_id": display_id})
    try:
        return db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": ObjectId(value)})
    except (InvalidId, TypeError):
        return None


def _looks_like_object_id(value: str) -> bool:
    """A 24-char hex string. Checked explicitly because "000000000000000000000042"
    parses as both an ObjectId and (via parse_display_id) an integer."""
    value = str(value).strip()
    if len(value) != 24:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _to_object_id(value: str, what: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(422, f"malformed {what} id")


def _find_candidates(db, document: dict) -> list:
    """THE SEAM. Returns documents that may be the same as `document`.

    Empty today. When the chunk-match algorithm lands it goes here: read this
    document's chunk vectors from local disk by sha256 (they are NOT in Mongo --
    the cluster is a 512 MB free tier), compare against the rest, and return
    DuplicateCandidate entries with a real score. Everything downstream of this
    function -- the endpoint, the response shape, the merge -- already works.
    """
    return []


@router.post("/documents/{row_id}/find-duplicates", response_model=DuplicateCandidatesOut, dependencies=[Depends(require_editor)])
def find_duplicates_for_row(row_id: str, db=Depends(get_db)):
    """The button on a main-table row. Resolves the row to its document and
    asks for candidates."""
    row = db[COLL_DOCUMENTS].find_one({"_id": _to_object_id(row_id, "document")})
    if row is None:
        raise HTTPException(404, "document not found")
    document = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": row["unique_document_id"]})
    if document is None:
        raise HTTPException(404, "the row's unique document is missing")
    return DuplicateCandidatesOut(
        document_id=format_display_id(document.get("display_id")) or "",
        candidates=_find_candidates(db, document),
        note=_NOT_IMPLEMENTED_NOTE,
    )


@router.post("/unique-documents/{document_id}/find-duplicates", response_model=DuplicateCandidatesOut, dependencies=[Depends(require_editor)])
def find_duplicates_for_document(document_id: str, db=Depends(get_db)):
    """The same search, started from the document rather than a placement."""
    document = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id, "unique document")})
    if document is None:
        raise HTTPException(404, "unique document not found")
    return DuplicateCandidatesOut(
        document_id=format_display_id(document.get("display_id")) or "",
        candidates=_find_candidates(db, document),
        note=_NOT_IMPLEMENTED_NOTE,
    )




# The translation pair: moved together or not at all.
_TRANSLATION_FIELDS = (
    "translation_zoho_file_id", "translation_shareable_link", "translation_status",
    "translated_by", "translated_at",
)
_REVIEW_FIELDS = (
    "review_zoho_file_id", "review_shareable_link", "review_status",
    "reviewed_by", "reviewed_at",
)


def _has_translation(doc: dict) -> bool:
    return bool(doc.get("translation_zoho_file_id") or doc.get("review_zoho_file_id"))


def _folder_key(row: dict) -> tuple:
    """What "already filed there" means: the same state and the same folder."""
    return (row.get("state_id"), row.get("crop_id"), row.get("organization_id"))


@router.post("/unique-documents/{document_id}/merge", response_model=MergeResult)
def merge_documents(document_id: str, body: MergeRequest, db=Depends(get_db),
                    who: Caller = Depends(require_editor)):
    """Merge the named documents into this one. See the module docstring.

    Every id in `absorb` must resolve and must not be the survivor itself; the
    whole request is validated before anything is written, so a typo in the
    fifth id does not leave the first four already merged.
    """
    survivor = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id, "unique document")})
    if survivor is None:
        raise HTTPException(404, "unique document not found")
    if not body.absorb:
        raise HTTPException(400, "nothing to absorb")

    absorbed: list[dict] = []
    seen: set = {survivor["_id"]}
    for value in body.absorb:
        other = _resolve_document(db, value)
        if other is None:
            raise HTTPException(404, f"unique document not found: {value}")
        if other["_id"] == survivor["_id"]:
            raise HTTPException(400, "a document cannot absorb itself")
        if other["_id"] in seen:
            continue  # the same document named twice -- harmless, absorb once
        seen.add(other["_id"])
        absorbed.append(other)

    absorbed_ids = [d["_id"] for d in absorbed]
    # A job whose server died is not "being translated" -- turn it failed first,
    # or a dead job would block the merge until someone opened the queue.
    from dashboard.routes_translation import _expire_lost_jobs

    _expire_lost_jobs(db)
    live = db[COLL_TRANSLATION_JOBS].find_one(
        {"document_id": {"$in": [survivor["_id"], *absorbed_ids]},
         "status": {"$in": ["queued", "running"]}},
        {"document_id": 1},
    )
    if live is not None:
        busy = next((d for d in [survivor, *absorbed] if d["_id"] == live["document_id"]), {})
        raise HTTPException(
            409,
            f"{format_display_id(busy.get('display_id')) or 'a document'} is being translated "
            "-- wait for it to finish or cancel it, then merge",
        )

    now = utcnow()
    merged_by = (body.merged_by or "").strip() or who.user_id

    # 1. Placements: drop where A is already filed, move everywhere else.
    filed = {_folder_key(r) for r in db[COLL_DOCUMENTS].find(
        {"unique_document_id": survivor["_id"]},
        {"state_id": 1, "crop_id": 1, "organization_id": 1})}
    moved: list = []
    dropped: list = []
    for row in db[COLL_DOCUMENTS].find(
            {"unique_document_id": {"$in": absorbed_ids}},
            {"state_id": 1, "crop_id": 1, "organization_id": 1, "row_id": 1}).sort("row_id", 1):
        key = _folder_key(row)
        if key in filed:
            dropped.append(row["_id"])
        else:
            filed.add(key)
            moved.append(row["_id"])

    # 2. Every copy link of every merged document, tagged with where it came
    #    from. A link already tagged by an earlier merge keeps its tags. A file
    #    A already lists is not listed twice. Each copy carries its document's
    #    fingerprint (`merged_sha256`), which is what unlink gives back.
    #    A group's name must be unique on A -- it is what unlink is asked for --
    #    so a second "Paddy POP.pdf" is labelled "Paddy POP.pdf (2)".
    listed = {l.get("zoho_file_id") for l in survivor.get("duplicate_links") or []}
    taken = {l.get("merged_document") for l in survivor.get("duplicate_links") or []} - {None}
    new_links: list[dict] = []
    for d in absorbed:
        labels: dict = {}

        def label(name):
            if name not in labels:
                unique, n = name, 1
                while unique in taken:
                    n += 1
                    unique = f"{name} ({n})"
                taken.add(unique)
                labels[name] = unique
            return labels[name]

        for link in d.get("duplicate_links") or []:
            if link.get("zoho_file_id") and link["zoho_file_id"] in listed:
                continue
            listed.add(link.get("zoho_file_id"))
            if link.get("merged_document"):
                tagged = {**link, "row_id": None, "merged_document": label(link["merged_document"])}
            else:
                tagged = {**link, "row_id": None,
                          "merged_document": label(d.get("shareable_name") or format_display_id(d.get("display_id"))),
                          "merged_by": merged_by, "merged_at": now, "merged_sha256": d.get("sha256")}
            new_links.append(tagged)
    # A fingerprint is kept only while a copy carries it -- that is what the
    # modal shows and what unlink can take back. So the list is read off the
    # copies actually added: a merged document whose files A already lists
    # adds neither copy nor fingerprint (those bytes are already accounted for).
    merged_shas = list(dict.fromkeys(l["merged_sha256"] for l in new_links if l.get("merged_sha256")))

    # 3. The translation pair. Taken from the first merged document that has
    #    one, only if A has none; everything else is trashed after the writes.
    taken_from = None
    update_set: dict = {"updated_at": now}
    if not _has_translation(survivor):
        taken_from = next((d for d in absorbed if _has_translation(d)), None)
        if taken_from is not None:
            for f in (*_TRANSLATION_FIELDS, *_REVIEW_FIELDS):
                update_set[f] = taken_from.get(f)
    to_trash: list[str] = []
    for d in absorbed:
        if d is not taken_from:
            to_trash += [d.get("translation_zoho_file_id"), d.get("review_zoho_file_id")]
    for job in db[COLL_TRANSLATION_JOBS].find({"document_id": {"$in": absorbed_ids}}, {"page_file_ids": 1}):
        to_trash += job.get("page_file_ids") or []
    to_trash = list(dict.fromkeys(f for f in to_trash if f))

    # The writes. Rows first: if anything after fails, a moved row points at A
    # and a dropped one is already gone -- never a row pointing at nothing.
    if dropped:
        db[COLL_DOCUMENTS].delete_many({"_id": {"$in": dropped}})
    if moved:
        db[COLL_DOCUMENTS].update_many(
            {"_id": {"$in": moved}},
            {"$set": {"unique_document_id": survivor["_id"], "updated_at": now}},
        )
    # The merged fingerprints are unique to one document, so B lets go of the
    # ones it collected before A takes them.
    db[COLL_UNIQUE_DOCUMENTS].update_many({"_id": {"$in": absorbed_ids}}, {"$set": {"merged_sha256s": []}})
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": survivor["_id"]},
        {
            "$push": {"duplicate_links": {"$each": new_links}},
            "$addToSet": {"main_row_ids": {"$each": moved}, "merged_sha256s": {"$each": merged_shas}},
            "$set": update_set,
        },
    )
    db[COLL_TRANSLATION_JOBS].delete_many({"document_id": {"$in": absorbed_ids}})
    db[COLL_UNIQUE_DOCUMENTS].delete_many({"_id": {"$in": absorbed_ids}})

    # 4. The trash, last and best-effort.
    not_trashed: list[str] = []
    if to_trash:
        from pop_server import _get_zoho

        wd = _get_zoho()
        for file_id in to_trash:
            try:
                if not wd.delete(file_id):
                    not_trashed.append(file_id)
            except Exception:  # noqa: BLE001
                not_trashed.append(file_id)

    after = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": survivor["_id"]})
    print(f"[merge] {format_display_id(survivor.get('display_id'))} <- "
          f"{[format_display_id(d.get('display_id')) for d in absorbed]} by {merged_by}: "
          f"{len(moved)} moved, {len(dropped)} dropped, {len(new_links)} link(s), "
          f"{len(to_trash) - len(not_trashed)}/{len(to_trash)} trashed", flush=True)
    return MergeResult(
        document_id=format_display_id(survivor.get("display_id")) or "",
        absorbed=[d.get("shareable_name") or format_display_id(d.get("display_id")) or "" for d in absorbed],
        placements_moved=len(moved),
        placements_dropped=len(dropped),
        links_added=len(new_links),
        translation_taken_from=(taken_from.get("shareable_name") if taken_from else None),
        placement_count=len(after.get("main_row_ids") or []),
        not_trashed=not_trashed,
    )


# -- Taking files back out of duplicate_links ----------------------------------
# Two ways, both on links and neither touching a placement or the merge above.
#
#   DELETE .../duplicate-links/{zoho_file_id}   one file, gone: out of the list
#                                               AND into the WorkDrive trash.
#   POST   .../unlink                           one merged GROUP (every link
#                                               sharing a `merged_document`)
#                                               out of the list and into a NEW
#                                               document of its own.
#
# THE ANCHOR is out of reach of both. It is the file translation reads and the
# "Original" download serves; deleting it would leave the document with nothing
# to translate, and it is a document's OWN copy so no merged group contains it.


@router.delete("/unique-documents/{document_id}/duplicate-links/{zoho_file_id}",
               response_model=UniqueDocumentOut, dependencies=[Depends(require_editor)])
def delete_duplicate_link(document_id: str, zoho_file_id: str, db=Depends(get_db)):
    """Delete one copy: trash its file in WorkDrive, then drop it from the list.

    Any entry but the anchor -- the document's own copies and merged ones
    alike, the team's call (2026-10-17). Deleting the LAST copy of a merged
    group also drops its fingerprint from `merged_sha256s`. A placement whose copy this was keeps
    working: placements are associations, and the "Original" download reads the
    document's anchor, not the row's copy.

    The file goes first, like DELETE /unique-documents/{id}: if WorkDrive
    refuses it this is a 502 and the list is untouched, so it can be retried.
    """
    oid = _to_object_id(document_id, "unique document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "unique document not found")
    if not any(l.get("zoho_file_id") == zoho_file_id for l in doc.get("duplicate_links") or []):
        raise HTTPException(404, "this document has no copy with that file id")
    if zoho_file_id == doc.get("representative_file_id"):
        raise HTTPException(400, "this is the document's own file (the one translated and downloaded) "
                                 "-- it cannot be deleted")

    from pop_server import _get_zoho

    try:
        trashed = _get_zoho().delete(zoho_file_id)
    except Exception:  # noqa: BLE001
        trashed = False
    if not trashed:
        raise HTTPException(502, "could not delete the file from WorkDrive -- nothing was removed, try again")

    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {"$pull": {"duplicate_links": {"zoho_file_id": zoho_file_id}}, "$set": {"updated_at": utcnow()}},
    )
    # The last copy carrying a merged fingerprint takes it along: a fingerprint
    # nothing shows could never be unlinked (seen on ANNAM_08755, 2026-10-17).
    sha = next((l.get("merged_sha256") for l in doc.get("duplicate_links") or []
                if l.get("zoho_file_id") == zoho_file_id), None)
    if sha:
        db[COLL_UNIQUE_DOCUMENTS].update_one(
            {"_id": oid, "duplicate_links.merged_sha256": {"$ne": sha}},
            {"$pull": {"merged_sha256s": sha}},
        )
    print(f"[copy-delete] {format_display_id(doc.get('display_id'))}: {zoho_file_id}", flush=True)
    from dashboard.routes_documents import _unique_one

    return _unique_one(db, db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid}))


@router.post("/unique-documents/{document_id}/unlink", response_model=UniqueDocumentOut,
             status_code=201, dependencies=[Depends(require_editor)])
def unlink_merged_group(document_id: str, body: UnlinkRequest, db=Depends(get_db)):
    """Split one merged group back out as a document of its own. Kept simple on
    purpose (the team's call, 2026-10-17):

      - the group is every link whose `merged_document` is the name sent; they
        leave this document's duplicate_links and become the new document's
        own copies (merged tags dropped, so they read as "system" there)
      - the new document's anchor is the group's FIRST link, and its name and
        link are that file's
      - its metadata is a copy of THIS document's -- the merged document's own
        was deleted by the merge, and they were judged duplicates
      - NO placements: the merge does not record which rows came from where, so
        none move back. It shows in the documents list and is filed again with
        POST .../placements
      - not translated: any translation stays here
      - its sha256 is the group's `merged_sha256`, taken off this document's
        `merged_sha256s` -- so a re-upload of that file finds the new document
        again (null for a merge made before copies carried one)

    No file is touched in WorkDrive.
    """
    oid = _to_object_id(document_id, "unique document")
    name = (body.merged_document or "").strip()
    if not name:
        raise HTTPException(400, "send merged_document -- the name of the merged group to unlink")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "unique document not found")
    group = [l for l in doc.get("duplicate_links") or [] if l.get("merged_document") == name]
    if not group:
        raise HTTPException(404, f"this document has no merged group named {name!r}")

    # The group's fingerprint goes with it: off this document's merged list,
    # onto the new document as its own. Null for a merge made before copies
    # carried one.
    sha = next((l["merged_sha256"] for l in group if l.get("merged_sha256")), None)

    # Taken out FIRST, and only if still there: two people unlinking the same
    # group at once must make one new document, not two.
    if not db[COLL_UNIQUE_DOCUMENTS].update_one(
            {"_id": oid, "duplicate_links.merged_document": name},
            {"$pull": {"duplicate_links": {"merged_document": name}, "merged_sha256s": sha},
             "$set": {"updated_at": utcnow()}},
    ).modified_count:
        raise HTTPException(409, "that group was just unlinked by someone else")

    links = [new_copy_link(zoho_file_id=l.get("zoho_file_id"), shareable_link=l.get("shareable_link"),
                           shareable_name=l.get("shareable_name")) for l in group]
    anchor = links[0]
    copied = {f: doc.get(f) for f in (*MANUAL_METADATA_FIELDS, "num_pages", "format_original",
                                      "language", "language_source")}
    try:
        created = allocate_and_insert(db, lambda display_id: new_unique_document(
            display_id=display_id,
            sha256=sha,
            district_id=doc.get("district_id"),
            kvk_id=doc.get("kvk_id"),
            **copied,
            shareable_name=anchor["shareable_name"],
            shareable_link=anchor["shareable_link"],
            representative_file_id=anchor["zoho_file_id"],
            duplicate_links=links,
        ))
    except Exception:
        # Put the group back rather than lose it.
        db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": oid}, {"$push": {"duplicate_links": {"$each": group}},
                                                            **({"$addToSet": {"merged_sha256s": sha}} if sha else {})})
        raise
    print(f"[unlink] {format_display_id(doc.get('display_id'))} -> "
          f"{format_display_id(created.get('display_id'))}: {len(links)} link(s) of {name!r}", flush=True)
    from dashboard.routes_documents import _unique_one

    return _unique_one(db, created)
