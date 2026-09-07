"""Integration tests for the split schema: `documents` + `unique_documents`.

Live-server tests, same style as the rest of tests/: they hit a REAL running
pop_server over HTTP rather than importing the app, because pop_server's
module-level side effects (Zoho singleton, thread pools, DB init via lifespan)
make in-process import awkward.

Deliberately does NOT use tests/conftest.py's `client` fixture: that one pins
localhost:8032 and the previous schema's database. This file brings its own
client so the two schemas' suites can run against different servers at once.

    .venv/bin/uvicorn pop_server:app --host 127.0.0.1 --port 8047
    POP_RENDER_BASE_URL=http://127.0.0.1:8047 .venv/bin/python3 -m pytest tests/test_render_main_table.py -v

Every test that writes creates its own rows and deletes them afterwards. None
of them mutate a migrated corpus row or document -- the assertions below read
those, but the only writes go to fixtures this file created.
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("POP_RENDER_BASE_URL", "http://127.0.0.1:8047")


@pytest.fixture(scope="session")
def client():
    c = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        resp = c.get("/dashboard/stats")
        if resp.status_code != 200:
            pytest.skip(f"pop_server not healthy at {BASE_URL} (status {resp.status_code})")
    except httpx.ConnectError:
        pytest.skip(f"pop_server not running at {BASE_URL} -- see this file's docstring")
    yield c
    c.close()


@pytest.fixture(scope="session")
def db():
    """Direct handle, for creating throwaway rows and for the assertions that
    are about storage rather than about the API -- 'is this field on the
    placement or on the document' cannot be asked over HTTP."""
    from dotenv import load_dotenv

    load_dotenv(".env")
    from dashboard.db import get_database

    return get_database()


@pytest.fixture
def scratch(db):
    """A throwaway document with two placements, cleaned up afterwards.

    Yields (document, [rows]). Ids are drawn from the live allocators, so this
    also exercises the fact that the two series are independent.
    """
    from dashboard.display_id import free_display_ids, free_row_ids
    from dashboard.models import (
        COLL_DOCUMENTS,
        COLL_UNIQUE_DOCUMENTS,
        link_placement,
        new_copy_link,
        new_document,
        new_unique_document,
    )

    doc = new_unique_document(
        display_id=free_display_ids(db, 1)[0],
        sha256="scratch" + "0" * 57,
        shareable_name="scratch-test.pdf",
        num_pages=3,
    )
    doc["_id"] = db[COLL_UNIQUE_DOCUMENTS].insert_one(doc).inserted_id

    rows = []
    for row_id, crop in zip(free_row_ids(db, 2), ("Scratch Alpha", "Scratch Beta")):
        row = new_document(row_id=row_id, unique_document_id=doc["_id"],
                           state="State Scratchland", crop=crop, source="upload")
        row["_id"] = db[COLL_DOCUMENTS].insert_one(row).inserted_id
        link_placement(db, doc["_id"], row_obj_id=row["_id"], row_id=row_id,
                       copy_link=new_copy_link(zoho_file_id=f"fid{row_id}",
                                               shareable_link=f"http://example/{row_id}",
                                               shareable_name="scratch-test.pdf",
                                               state="Scratchland", crop=crop))
        rows.append(row)
    yield db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": doc["_id"]}), rows

    db[COLL_DOCUMENTS].delete_many({"unique_document_id": doc["_id"]})
    db[COLL_UNIQUE_DOCUMENTS].delete_one({"_id": doc["_id"]})


# -- shape of the split --------------------------------------------------------


def test_placements_outnumber_documents(client):
    """The whole reason for two collections: one document can be filed in many
    folders, so there are more rows than documents."""
    s = client.get("/dashboard/stats").json()
    assert s["documents"] > s["files"] > 0
    assert s["states"] > 0 and s["crops"] > 0


def test_every_placement_resolves_to_a_document(db):
    from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS

    known = {d["_id"] for d in db[COLL_UNIQUE_DOCUMENTS].find({}, {"_id": 1})}
    dangling = [r["row_id"] for r in db[COLL_DOCUMENTS].find({}, {"row_id": 1, "unique_document_id": 1})
                if r["unique_document_id"] not in known]
    assert dangling == []


def test_link_lists_match_the_placements(db):
    """main_row_ids mirrors the placements exactly: one entry per row.
    If they ever disagree, something wrote one without the other."""
    from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS

    rows = db[COLL_DOCUMENTS].count_documents({})
    totals = next(db[COLL_UNIQUE_DOCUMENTS].aggregate([{"$group": {
        "_id": None,
        "ids": {"$sum": {"$size": "$main_row_ids"}},
    }}]))
    assert totals["ids"] == rows


def test_copy_links_count_files_not_placements(db):
    """duplicate_links is the list of PHYSICAL files, so a file is listed once.

    The corpus has one file per placement (WorkDrive copies a file into every
    folder), so there the two counts coincide. A dashboard upload writes one
    file and files it in several places, so it has fewer copies than
    placements -- what must never happen is the same file listed twice, which
    would show a document as having identical duplicates of itself and offer a
    choice of anchor where there is only one file.
    """
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    repeated, more_links_than_rows, stray_anchor = [], [], []
    for d in db[COLL_UNIQUE_DOCUMENTS].find(
        {}, {"display_id": 1, "duplicate_links": 1, "main_row_ids": 1,
             "representative_row_id": 1, "representative_file_id": 1}
    ):
        links = d.get("duplicate_links") or []
        file_ids = [c.get("zoho_file_id") for c in links]
        if len(file_ids) != len(set(file_ids)):
            repeated.append(d["display_id"])
        if len(links) > len(d.get("main_row_ids") or []):
            more_links_than_rows.append(d["display_id"])
        # The anchor has to be one of the copies actually listed, or nothing
        # can mark it in a list of copies.
        anchor_row = d.get("representative_row_id")
        if links and anchor_row is not None and anchor_row not in {c.get("row_id") for c in links}:
            stray_anchor.append(d["display_id"])

    assert repeated == [], f"same file listed twice: {repeated[:5]}"
    assert more_links_than_rows == [], f"more copies than placements: {more_links_than_rows[:5]}"
    assert stray_anchor == [], f"anchor is not one of the listed copies: {stray_anchor[:5]}"


def test_main_table_carries_no_metadata_and_no_links(db):
    """A row is a pure association. Metadata living on it again would mean an
    edit has to be applied in up to 66 places."""
    from dashboard.models import COLL_DOCUMENTS

    for field in ("sha256", "shareable_link", "zoho_file_id", "num_pages",
                  "language", "translation_status", "advisory_type", "shareable_name"):
        assert db[COLL_DOCUMENTS].count_documents({field: {"$exists": True}}) == 0, field


def test_two_id_series_are_independent(client):
    row = client.get("/dashboard/documents?page=1").json()["items"][0]
    assert row["row_id"].startswith("POP_")
    assert row["document_id"].startswith("ANNAM_")


# -- listing -------------------------------------------------------------------


def test_list_is_paginated_at_100(client):
    page = client.get("/dashboard/documents?page=1").json()
    assert page["page_size"] == 100
    assert len(page["items"]) == 100
    assert page["total"] > 100


def test_pages_do_not_overlap_or_skip(client):
    """The sort is (created_at DESC, _id ASC). Without the _id tiebreak the
    corpus load's identical timestamps make skip/limit repeat and drop rows."""
    one = client.get("/dashboard/documents?page=1").json()["items"]
    two = client.get("/dashboard/documents?page=2").json()["items"]
    assert not ({r["id"] for r in one} & {r["id"] for r in two})


def test_row_is_served_joined(client):
    """Stored as an association, served complete -- the frontend renders a row
    without a second request."""
    row = client.get("/dashboard/documents?page=1").json()["items"][0]
    for field in ("state", "crop", "row_id", "document_id", "unique_document_id",
                  "shareable_name", "shareable_link", "sha256", "placement_count"):
        assert field in row, field
    assert row["placement_count"] >= 1


@pytest.mark.parametrize("query,where", [
    ("filter[state]=Karnataka", "placement"),
    ("filter[crop]=Paddy", "placement"),
    ("filter[translation_status]=done", "document"),
    ("filter[language]=eng", "document"),
    ("filter[format_original]=pdf", "document"),
])
def test_filters_hit_the_right_collection(client, query, where):
    """Placement filters run before the $lookup, document filters after it.
    Both must return rows, which is what proves the prefixing is right."""
    page = client.get(f"/dashboard/documents?{query}").json()
    assert page["total"] > 0, (query, where)


def test_filters_combine_across_the_join(client):
    combined = client.get(
        "/dashboard/documents?filter[state]=Karnataka&filter[translation_status]=not_started"
    ).json()
    state_only = client.get("/dashboard/documents?filter[state]=Karnataka").json()
    assert 0 < combined["total"] <= state_only["total"]


def test_multi_select_filters_are_comma_separated(client):
    """A repeated `filter[state]=A&filter[state]=B` silently keeps only the LAST
    value in Starlette, so a two-state selection filtered on one state. Comma is
    the one form with an unambiguous reading."""
    a = client.get("/dashboard/documents?filter[state]=Karnataka").json()["total"]
    b = client.get("/dashboard/documents?filter[state]=Kerala").json()["total"]
    both = client.get("/dashboard/documents?filter[state]=Karnataka,Kerala").json()["total"]
    assert a > 0 and b > 0
    assert both == a + b, "a state belongs to exactly one placement, so these add up"
    # Whitespace and a trailing comma are a person's input, not an error.
    assert client.get("/dashboard/documents?filter[state]=Karnataka, Kerala,").json()["total"] == both


def test_range_filters(client):
    """_min/_max on numbers, _from/_to on dates. Either end may be omitted --
    "at least 10 pages" is as reasonable a filter as a bounded range."""
    low = client.get("/dashboard/documents?filter[num_pages_min]=100").json()
    high = client.get("/dashboard/documents?filter[num_pages_max]=2").json()
    band = client.get("/dashboard/documents?filter[num_pages_min]=10&filter[num_pages_max]=20").json()
    assert low["total"] > 0 and high["total"] > 0 and band["total"] > 0
    assert all(10 <= r["num_pages"] <= 20 for r in band["items"])

    dated = client.get("/dashboard/documents"
                       "?filter[date_of_collection_from]=2026-08-01"
                       "&filter[date_of_collection_to]=2026-08-31").json()
    assert dated["total"] > 0
    # Ranges work on the documents list too, not just the main table.
    assert client.get("/dashboard/unique-documents?filter[num_pages_min]=100").json()["total"] > 0


def test_date_and_month_fields_are_filterable(client):
    """These were absent from the whitelist entirely, so the frontend's date and
    month controls had nothing to talk to."""
    for name, value in (("month_of_collection", "8"), ("date_of_collection", "2026-08-24")):
        assert client.get(f"/dashboard/documents?filter[{name}]={value}").json()["total"] > 0
    # The release fields are filterable and legitimately empty -- no corpus pass
    # ever wrote them. Zero is the right answer, not a broken filter.
    assert client.get("/dashboard/documents?filter[year_of_release]=2024").json()["total"] == 0


def test_a_range_on_a_text_column_matches_nothing(client):
    """Better than silently ignoring it: the caller sees an empty result and
    knows the filter did not mean what they thought."""
    assert client.get("/dashboard/documents?filter[state_from]=x").json()["total"] == 0


def test_unparseable_filter_returns_an_empty_page(client):
    page = client.get("/dashboard/documents?filter[num_pages]=not-a-number").json()
    assert page["total"] == 0 and page["items"] == []


def test_siblings_are_the_other_placements(client, scratch):
    doc, rows = scratch
    first = str(rows[0]["_id"])
    sib = client.get(f"/dashboard/documents/{first}/siblings").json()
    assert [s["id"] for s in sib] == [str(rows[1]["_id"])]


# -- the document behind the row -----------------------------------------------


def test_unique_documents_list_is_paginated_and_filterable(client):
    """The counterpart of GET /documents. A caller cannot derive this by
    de-duplicating a page of placements -- pagination is server-side, so a page
    of 100 placements collapses to an unpredictable number of documents."""
    page = client.get("/dashboard/unique-documents?page=1").json()
    assert page["page_size"] == 100 and len(page["items"]) == 100
    placements = client.get("/dashboard/documents?page=1").json()
    assert page["total"] < placements["total"], "documents must be fewer than placements"
    assert page["total"] == client.get("/dashboard/stats").json()["files"]

    two = client.get("/dashboard/unique-documents?page=2").json()
    assert not ({d["id"] for d in page["items"]} & {d["id"] for d in two["items"]})

    row = page["items"][0]
    for field in ("document_id", "shareable_name", "shareable_link", "language",
                  "placement_count", "duplicate_links", "representative_file_id"):
        assert field in row, field


def test_unique_documents_filters_match_the_main_tables(client):
    """The same filter name must mean the same thing from either direction, or
    the two tabs quietly disagree."""
    for name, value in (("translation_status", "done"), ("language", "kan"),
                        ("format_original", "pdf")):
        docs = client.get(f"/dashboard/unique-documents?filter[{name}]={value}").json()
        rows = client.get(f"/dashboard/documents?filter[{name}]={value}").json()
        assert docs["total"] > 0, name
        # Every matching document has at least one placement, so placements can
        # only be >= documents.
        assert rows["total"] >= docs["total"], name
    exact = client.get("/dashboard/unique-documents?filter[document_id]=ANNAM_00321").json()
    assert exact["total"] == 1
    assert client.get("/dashboard/unique-documents?filter[num_pages]=nope").json()["total"] == 0


def test_multi_placement_filter_finds_the_grouped_documents(client, db):
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    page = client.get("/dashboard/unique-documents?filter[multi_placement]=true").json()
    assert page["total"] == db[COLL_UNIQUE_DOCUMENTS].count_documents(
        {"main_row_ids.1": {"$exists": True}})
    assert all(d["placement_count"] > 1 for d in page["items"])
    single = client.get("/dashboard/unique-documents?filter[multi_placement]=false").json()
    assert single["total"] + page["total"] == client.get("/dashboard/stats").json()["files"]


def test_get_unique_document_and_its_placements(client, scratch):
    doc, rows = scratch
    out = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()
    assert out["placement_count"] == 2
    assert len(out["duplicate_links"]) == 2
    # Every physical copy stays reachable -- that is why links live here.
    assert all(l["shareable_link"] for l in out["duplicate_links"])
    placements = client.get(f"/dashboard/unique-documents/{doc['_id']}/placements").json()
    assert {p["id"] for p in placements} == {str(r["_id"]) for r in rows}


def test_every_copy_link_points_at_its_own_file(db):
    """The reason duplicate_links exists is that WorkDrive keeps a separate file
    per folder. A link built from the DOCUMENT rather than the COPY silently
    sends every placement to the same physical file -- which is what happened
    for 1,000 of 9,811 placements, because pops.csv and report_true are keyed by
    sha256 and hold one link per document."""
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    mismatched = [
        (d["_id"], link["zoho_file_id"])
        for d in db[COLL_UNIQUE_DOCUMENTS].find({}, {"duplicate_links": 1})
        for link in (d.get("duplicate_links") or [])
        if link.get("zoho_file_id") and link["zoho_file_id"] not in (link.get("shareable_link") or "")
    ]
    assert mismatched == []


def test_every_document_is_anchored_to_one_of_its_own_copies(db):
    """The anchor is what translation acts on. It must always name a file this
    document actually owns -- otherwise the pipeline works on bytes the
    document's metadata does not describe."""
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    broken = []
    for d in db[COLL_UNIQUE_DOCUMENTS].find({}, {"representative_file_id": 1, "duplicate_links": 1}):
        anchor = d.get("representative_file_id")
        owned = {l.get("zoho_file_id") for l in (d.get("duplicate_links") or [])}
        if not owned:
            continue  # a document with no copies has nothing to anchor to
        if anchor not in owned:
            broken.append(d["_id"])
    assert broken == []


def test_translation_uses_the_anchor_not_the_first_copy(db):
    """Today every copy is byte-identical so this is invisible. Once the merge
    endpoint absorbs NEAR-duplicates it stops being, and translating 'whichever
    copy is first' would work on a re-scan instead of the document."""
    from dashboard.routes_translation import _source_file_id

    doc = {
        "representative_file_id": "anchor-file",
        "duplicate_links": [
            {"zoho_file_id": "some-other-copy"},
            {"zoho_file_id": "anchor-file"},
        ],
    }
    assert _source_file_id(doc) == "anchor-file"
    # Unset or stale anchor: fall back rather than refuse to translate at all.
    assert _source_file_id({"duplicate_links": [{"zoho_file_id": "only-copy"}]}) == "only-copy"
    assert _source_file_id({"representative_file_id": "gone",
                            "duplicate_links": [{"zoho_file_id": "only-copy"}]}) == "only-copy"
    assert _source_file_id({"duplicate_links": []}) is None


def test_reanchoring_is_restricted_to_the_documents_own_copies(client, scratch):
    doc, _rows = scratch
    owned = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()["duplicate_links"]
    other = owned[1]["zoho_file_id"]
    assert client.patch(f"/dashboard/unique-documents/{doc['_id']}",
                        json={"representative_file_id": "not-a-file-of-mine"}).status_code == 400
    out = client.patch(f"/dashboard/unique-documents/{doc['_id']}",
                       json={"representative_file_id": other}).json()
    assert out["representative_file_id"] == other
    assert out["representative_row_id"] == owned[1]["row_id"]


def test_merge_does_not_move_the_anchor(client, db, scratch):
    """Absorbed copies become reachable, never promoted."""
    from dashboard.display_id import free_display_ids, free_row_ids
    from dashboard.models import (
        COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS, link_placement,
        new_copy_link, new_document, new_unique_document,
    )

    survivor, _rows = scratch
    before = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": survivor["_id"]})["representative_file_id"]
    other = new_unique_document(display_id=free_display_ids(db, 1)[0],
                                sha256="anchor" + "0" * 58, shareable_name="other.pdf",
                                representative_file_id="fidOTHER")
    other["_id"] = db[COLL_UNIQUE_DOCUMENTS].insert_one(other).inserted_id
    row_id = free_row_ids(db, 1)[0]
    row = new_document(row_id=row_id, unique_document_id=other["_id"],
                       state="State Scratchland", crop="Scratch Delta", source="upload")
    row["_id"] = db[COLL_DOCUMENTS].insert_one(row).inserted_id
    link_placement(db, other["_id"], row_obj_id=row["_id"], row_id=row_id,
                   copy_link=new_copy_link(zoho_file_id="fidOTHER", shareable_link="http://example/o",
                                           shareable_name="other.pdf",
                                           state="Scratchland", crop="Scratch Delta"))
    try:
        client.post(f"/dashboard/unique-documents/{survivor['_id']}/merge",
                    json={"absorb": [str(other["_id"])]})
        after = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": survivor["_id"]})
        assert after["representative_file_id"] == before
        assert "fidOTHER" in [l["zoho_file_id"] for l in after["duplicate_links"]]
    finally:
        db[COLL_DOCUMENTS].delete_one({"_id": row["_id"]})
        db[COLL_UNIQUE_DOCUMENTS].delete_one({"_id": other["_id"]})


def test_embeddings_are_never_serialised(client, scratch):
    """Always empty today, thousands of floats later. A page of 100 rows must
    not carry them."""
    doc, _rows = scratch
    assert "chunk_embeddings" not in client.get(f"/dashboard/unique-documents/{doc['_id']}").json()


def test_patch_metadata_goes_to_the_document(client, scratch):
    """One write, not one per placement."""
    doc, rows = scratch
    resp = client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"season": "Rabi", "domain": "Agri"})
    assert resp.status_code == 200
    out = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()
    assert out["season"] == "Rabi" and out["domain"] == "Agri"
    # ...and therefore visible from the OTHER placement too.
    assert client.get(f"/dashboard/documents/{rows[1]['_id']}").status_code == 200


def test_patch_placement_stays_on_the_row(client, db, scratch):
    doc, rows = scratch
    resp = client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"crop": "moved crop"})
    assert resp.json()["crop"] == "Moved Crop"  # normalised
    # The other placement is untouched.
    assert client.get(f"/dashboard/documents/{rows[1]['_id']}").json()["crop"] == "Scratch Beta"
    # The copy entry on the document records the folder, so it moves too.
    out = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()
    moved = [l for l in out["duplicate_links"] if l["row_id"] == rows[0]["row_id"]]
    assert moved and moved[0]["crop"] == "Moved Crop"


def test_patch_keeps_the_raw_name(db, client, scratch):
    """The OCR language keys off the ORIGINAL state name, so a move must keep
    state_raw in step or non-English OCR silently breaks."""
    from dashboard.models import COLL_DOCUMENTS

    _doc, rows = scratch
    client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"state": "State Karnataka"})
    stored = db[COLL_DOCUMENTS].find_one({"_id": rows[0]["_id"]})
    assert stored["state"] == "Karnataka" and stored["state_raw"] == "State Karnataka"


def test_language_must_come_from_the_collection(client, scratch):
    """The whole point of the lookup: the team picks, it cannot free-type."""
    _doc, rows = scratch
    assert client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"language": "Klingon"}).status_code == 400
    assert client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"language": "kan"}).status_code == 200


def test_setting_language_by_hand_marks_it_known(client, scratch):
    """A person's choice is a known language, not a guess, so it joins
    "detected" -- which is also what makes it survive a re-run of
    scripts/fill_language_from_state.py."""
    doc, rows = scratch
    client.patch(f"/dashboard/documents/{rows[0]['_id']}", json={"language": "tam"})
    out = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()
    assert out["language"] == "tam" and out["language_source"] == "detected"


def test_language_source_is_a_closed_vocabulary(db):
    """Three values, and no fourth. `state` is the one that means "a guess";
    `detected` was read off the file and `manual` was chosen by a person on
    upload -- both are known, they just came from different places."""
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    seen = set(db[COLL_UNIQUE_DOCUMENTS].distinct("language_source"))
    assert seen <= {"detected", "state", "manual"}, seen
    assert db[COLL_UNIQUE_DOCUMENTS].count_documents({"language_source": None}) == 0


def test_the_fill_script_never_overwrites_a_known_language(db):
    """Its overwrite set must exclude "detected", or a re-run replaces every
    correction the agri team has made with a guess from the state."""
    from scripts.fill_language_from_state import _OVERWRITABLE

    assert "detected" not in _OVERWRITABLE


def test_every_document_has_a_language(client):
    """The rule: a document the OCR pass read as English is English; everything
    else takes its state's language. Nothing is left blank -- a plausible value
    the agri team can correct beats a blank they cannot see."""
    from dashboard.languages import LANGUAGES

    for row in client.get("/dashboard/documents?page=1").json()["items"]:
        assert row["language"] in LANGUAGES, row
        assert row["language_source"] in ("detected", "state", "manual")


def test_no_document_is_left_without_a_language(db):
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    assert db[COLL_UNIQUE_DOCUMENTS].count_documents({"language": None}) == 0


def test_state_language_inference_is_marked_as_such(client):
    """Most of the corpus is inferred from the state, not measured. If that
    distinction is ever lost, a guess becomes indistinguishable from a
    detection -- which is how the wrong OCR model gets trusted."""
    inferred = client.get("/dashboard/documents?filter[language_source]=state").json()
    detected = client.get("/dashboard/documents?filter[language_source]=detected").json()
    assert inferred["total"] > 0 and detected["total"] > 0
    assert all(r["language_source"] == "state" for r in inferred["items"])


def test_state_language_table_covers_every_state_and_only_tessdata(client):
    from dashboard.languages import LANGUAGES, STATE_LANG, language_for_state

    assert set(STATE_LANG.values()) <= set(LANGUAGES)
    # Every state the catalogue actually uses must map, or its documents fall
    # back to no language at all.
    for state in client.get("/dashboard/states").json():
        assert any(language_for_state(raw) for raw in state["raw_names"]), state["name"]


def test_delete_document_takes_its_placements_with_it(client, db, scratch):
    """The destructive one: document, placements and files all go.

    The scratch document's copy links point at ids WorkDrive does not have, so
    this also covers the guard -- a file that cannot be deleted is a 502 and
    the database is left exactly as it was, retryable.
    """
    from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS

    doc, rows = scratch
    resp = client.delete(f"/dashboard/unique-documents/{doc['_id']}")
    if resp.status_code == 502:
        assert "nothing was removed" in resp.json()["detail"]
        assert db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": doc["_id"]}) is not None
        assert db[COLL_DOCUMENTS].count_documents({"unique_document_id": doc["_id"]}) == len(rows)
        # Take the files out of the way; the cascade itself is what is under test.
        db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": doc["_id"]}, {"$set": {"duplicate_links": []}})
        resp = client.delete(f"/dashboard/unique-documents/{doc['_id']}")

    assert resp.status_code == 204, resp.text
    assert db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": doc["_id"]}) is None
    assert db[COLL_DOCUMENTS].count_documents({"unique_document_id": doc["_id"]}) == 0
    assert client.delete(f"/dashboard/unique-documents/{doc['_id']}").status_code == 404


def test_delete_document_is_refused_while_translating(client, db, scratch):
    from dashboard.models import (
        COLL_TRANSLATION_JOBS,
        COLL_UNIQUE_DOCUMENTS,
        TranslationJobKind,
        new_translation_job,
    )

    doc, _rows = scratch
    job = new_translation_job(document_id=doc["_id"], kind=TranslationJobKind.translate)
    job["status"] = "running"
    job_id = db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id
    try:
        resp = client.delete(f"/dashboard/unique-documents/{doc['_id']}")
        assert resp.status_code == 409, resp.text
        assert db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": doc["_id"]}) is not None
    finally:
        db[COLL_TRANSLATION_JOBS].delete_one({"_id": job_id})


def test_delete_placement_keeps_the_document(client, db, scratch):
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    doc, rows = scratch
    assert client.delete(f"/dashboard/documents/{rows[0]['_id']}").status_code == 204
    after = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": doc["_id"]})
    assert after is not None, "deleting a placement must not delete the document"
    assert len(after["main_row_ids"]) == 1
    assert len(after["duplicate_links"]) == 1


# -- duplicate review ----------------------------------------------------------


def test_find_duplicates_is_empty_but_says_why(client, scratch):
    """The button exists so the frontend can be built. It must not imply that
    no duplicates exist."""
    _doc, rows = scratch
    out = client.post(f"/dashboard/documents/{rows[0]['_id']}/find-duplicates").json()
    assert out["candidates"] == []
    assert out["note"] and "not connected yet" in out["note"]
    assert out["document_id"].startswith("ANNAM_")


def test_merge_repoints_placements_and_never_deletes_a_row(client, db, scratch):
    """A merge changes which document a folder entry is understood to hold. It
    does not claim the folder entry stopped existing."""
    from dashboard.display_id import free_display_ids, free_row_ids
    from dashboard.models import (
        COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS, link_placement,
        new_copy_link, new_document, new_unique_document,
    )

    survivor, survivor_rows = scratch
    other = new_unique_document(display_id=free_display_ids(db, 1)[0],
                                sha256="mergeme" + "0" * 57, shareable_name="merge-me.pdf")
    other["_id"] = db[COLL_UNIQUE_DOCUMENTS].insert_one(other).inserted_id
    row_id = free_row_ids(db, 1)[0]
    row = new_document(row_id=row_id, unique_document_id=other["_id"],
                       state="State Scratchland", crop="Scratch Gamma", source="upload")
    row["_id"] = db[COLL_DOCUMENTS].insert_one(row).inserted_id
    link_placement(db, other["_id"], row_obj_id=row["_id"], row_id=row_id,
                   copy_link=new_copy_link(zoho_file_id="fidX", shareable_link="http://example/x",
                                           shareable_name="merge-me.pdf",
                                           state="Scratchland", crop="Scratch Gamma"))
    rows_before = db[COLL_DOCUMENTS].count_documents({})
    try:
        result = client.post(f"/dashboard/unique-documents/{survivor['_id']}/merge",
                             json={"absorb": [str(other["_id"])]}).json()
        assert result["placements_repointed"] == 1
        assert result["placement_count"] == 3

        after = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": survivor["_id"]})
        assert len(after["main_row_ids"]) == 3
        # Every physical copy of the absorbed document is still reachable.
        assert len(after["duplicate_links"]) == 3
        assert "fidX" in [l["zoho_file_id"] for l in after["duplicate_links"]]
        assert after["merged_from"]  # audit trail, so a bad merge is undoable by hand

        assert db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": other["_id"]}) is None
        assert db[COLL_DOCUMENTS].count_documents({}) == rows_before
        assert db[COLL_DOCUMENTS].find_one({"_id": row["_id"]})["unique_document_id"] == survivor["_id"]
    finally:
        db[COLL_DOCUMENTS].delete_one({"_id": row["_id"]})
        db[COLL_UNIQUE_DOCUMENTS].delete_one({"_id": other["_id"]})


def test_merge_validates_before_it_writes(client, scratch):
    """A typo in the fifth id must not leave the first four already merged."""
    doc, _rows = scratch
    assert client.post(f"/dashboard/unique-documents/{doc['_id']}/merge",
                       json={"absorb": []}).status_code == 400
    assert client.post(f"/dashboard/unique-documents/{doc['_id']}/merge",
                       json={"absorb": [str(doc["_id"])]}).status_code == 400
    assert client.post(f"/dashboard/unique-documents/{doc['_id']}/merge",
                       json={"absorb": ["ANNAM_49999"]}).status_code == 404


# -- lookups -------------------------------------------------------------------


def test_states_and_crops_are_vocabularies(client):
    states = client.get("/dashboard/states").json()
    assert len(states) > 10
    entry = next(s for s in states if s["name"] == "Karnataka")
    # raw_names keeps the lookup reversible -- the OCR language keys off the raw
    # name, not the normalised one.
    assert "State Karnataka" in entry["raw_names"]
    assert entry["document_count"] > 0
    assert all(not c["name"].startswith("State ") for c in client.get("/dashboard/crops").json())


def test_a_new_crop_can_be_added_and_appears_in_the_dropdown(client, db):
    """The upload form lets the team type a crop nobody has used before; it has
    to be in their own dropdown afterwards."""
    from dashboard.models import COLL_CROPS

    name = "zz test crop for schema check"
    try:
        created = client.post("/dashboard/crops", json={"name": name}).json()
        assert created["name"] == "Zz Test Crop For Schema Check"  # normalised
        assert name in created["raw_names"]                        # ...reversibly
        assert any(c["name"] == created["name"] for c in client.get("/dashboard/crops").json())
        # Idempotent: asking twice is not an error.
        assert client.post("/dashboard/crops", json={"name": name}).status_code == 201
        assert client.post("/dashboard/crops", json={}).status_code == 400
    finally:
        db[COLL_CROPS].delete_one({"name": "Zz Test Crop For Schema Check"})


def test_upload_registers_an_unseen_crop(client, db):
    """create_placements() is what a decision calls, so a crop introduced by an
    upload joins the vocabulary without a separate step."""
    from dashboard.db import get_database
    from dashboard.models import COLL_CROPS, COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS
    from dashboard.display_id import free_display_ids
    from dashboard.models import new_unique_document
    from dashboard.queue_worker import create_placements

    live = get_database()
    doc = new_unique_document(display_id=free_display_ids(live, 1)[0], sha256="vocab" + "0" * 59)
    doc["_id"] = live[COLL_UNIQUE_DOCUMENTS].insert_one(doc).inserted_id
    try:
        rows = create_placements(live, document=doc,
                                 placements=[{"state": "Karnataka", "crop": "Zz Brand New Crop"}],
                                 copy={"zoho_file_id": "fidV", "shareable_link": "http://example/v",
                                       "shareable_name": "v.pdf"})
        assert len(rows) == 1
        assert live[COLL_CROPS].find_one({"name": "Zz Brand New Crop"}) is not None
    finally:
        live[COLL_DOCUMENTS].delete_many({"unique_document_id": doc["_id"]})
        live[COLL_UNIQUE_DOCUMENTS].delete_one({"_id": doc["_id"]})
        live[COLL_CROPS].delete_one({"name": "Zz Brand New Crop"})


def test_document_carries_its_own_shareable_link(client, db):
    """"Shareable Link" is one of the required metadata fields, so it has to be
    ON the document -- not only inside duplicate_links. It is the anchor's."""
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    assert db[COLL_UNIQUE_DOCUMENTS].count_documents({"shareable_link": None}) == 0
    mismatched = [
        d["_id"] for d in db[COLL_UNIQUE_DOCUMENTS].find(
            {}, {"shareable_link": 1, "representative_file_id": 1})
        if d.get("representative_file_id")
        and d["representative_file_id"] not in (d.get("shareable_link") or "")
    ]
    assert mismatched == []
    row = client.get("/dashboard/documents?page=1").json()["items"][0]
    assert row["shareable_link"].startswith("https://workdrive.zoho.in/file/")


def test_reanchoring_moves_the_documents_link(client, scratch):
    doc, _rows = scratch
    owned = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()["duplicate_links"]
    out = client.patch(f"/dashboard/unique-documents/{doc['_id']}",
                       json={"representative_file_id": owned[1]["zoho_file_id"]}).json()
    assert out["shareable_link"] == owned[1]["shareable_link"]


def test_crops_can_be_narrowed_by_state(client):
    everything = client.get("/dashboard/crops").json()
    narrowed = client.get("/dashboard/crops?state=Karnataka").json()
    assert 0 < len(narrowed) < len(everything)


def test_languages_include_the_verdicts_and_the_tessdata_pack(client):
    langs = client.get("/dashboard/languages").json()
    codes = {x["code"] for x in langs}
    assert {"eng", "kan", "hin", "ori", "tam"} <= codes
    # "Non-English" is not a language, but it IS what the OCR pass concluded for
    # hundreds of documents, so the team needs it to refine them from.
    assert "non_english" in codes


# -- uploads -------------------------------------------------------------------
# Everything up to the decision. The `new` path is exercised only where it is
# refused: taking it for real pushes a file into Zoho WorkDrive, which a test run
# should not do.


@pytest.mark.parametrize("data", [
    {"placements_json": '[{"state":"State Karnataka","crops":["Paddy"]}]', "language": "xyz"},
    {"placements_json": '[{"state":"State Karnataka","crops":[]}]', "language": "eng"},
    {"placements_json": "notjson", "language": "eng"},
    {"placements_json": '[{"crops":["Paddy"]}]', "language": "eng"},
    {"states_json": "[]", "crops_json": '["Paddy"]', "language": "eng"},
    {"states_json": '["State Karnataka"]', "crops_json": "[]", "language": "eng"},
])
def test_upload_validation(client, data):
    resp = client.post("/dashboard/uploads",
                       files={"file": ("t.pdf", b"%PDF-1.4 fake", "application/pdf")}, data=data)
    assert resp.status_code == 400, resp.text


_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


def _submit(client, **data):
    resp = client.post("/dashboard/uploads",
                       files={"file": ("render_test.pdf", _MINIMAL_PDF, "application/pdf")},
                       data={"language": "eng", **data})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _await_review(client, item_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = client.get(f"/dashboard/uploads/{item_id}").json()
        if item["status"] in ("awaiting_review", "failed"):
            return item
        time.sleep(0.3)
    pytest.fail(f"upload {item_id} never left {item['status']}")


def test_upload_takes_per_state_crop_groups(client):
    """A state with its crops, then another state with different crops -- not a
    cross product of one crop list over every state."""
    item_id = _submit(client, placements_json=json.dumps([
        {"state": "State Karnataka", "crops": ["Paddy", "Ragi"]},
        {"state": "State Kerala", "crops": ["Coconut"]},
    ]))
    try:
        item = client.get(f"/dashboard/uploads/{item_id}").json()
        assert [(p["state"], p["crop"]) for p in item["placements"]] == [
            ("Karnataka", "Paddy"), ("Karnataka", "Ragi"), ("Kerala", "Coconut")]
    finally:
        client.post(f"/dashboard/uploads/{item_id}/cancel")


def test_states_and_crops_still_work_as_a_cross_product(client):
    item_id = _submit(client, states_json='["State Karnataka","State Kerala"]',
                      crops_json='["Paddy"]')
    try:
        item = client.get(f"/dashboard/uploads/{item_id}").json()
        assert [(p["state"], p["crop"]) for p in item["placements"]] == [
            ("Karnataka", "Paddy"), ("Kerala", "Paddy")]
    finally:
        client.post(f"/dashboard/uploads/{item_id}/cancel")


def test_duplicate_placements_are_collapsed(client):
    """A person listing the same crop under one state twice is normal; it must
    not become two rows for one folder."""
    item_id = _submit(client, placements_json=json.dumps([
        {"state": "State Karnataka", "crops": ["Paddy", "paddy"]},
        {"state": "state karnataka", "crops": ["Paddy"]},
    ]))
    try:
        assert len(client.get(f"/dashboard/uploads/{item_id}").json()["placements"]) == 1
    finally:
        client.post(f"/dashboard/uploads/{item_id}/cancel")


def test_upload_pauses_for_review_and_cancels_cleanly(client):
    """No document and no placement exists until a person decides."""
    before = client.get("/dashboard/stats").json()
    item_id = _submit(client, placements_json='[{"state":"State Karnataka","crops":["Paddy"]}]')
    try:
        item = _await_review(client, item_id)
        assert item["status"] == "awaiting_review", item
        assert item["sha256"] and len(item["sha256"]) == 64
        assert item["note"]
        assert client.get("/dashboard/stats").json() == before
    finally:
        assert client.post(f"/dashboard/uploads/{item_id}/cancel").status_code in (204, 404)
    assert client.get(f"/dashboard/uploads/{item_id}").status_code == 404
    assert client.get("/dashboard/stats").json() == before


def test_candidates_report_what_is_new_and_what_is_not(client, db):
    """The three-way decision is driven by the placements, not by yes/no: `add`
    is offered only when this upload names a place the document is not in."""
    from dashboard.db import get_database
    from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS
    from dashboard.queue_worker import find_candidates, new_placements_for

    live = get_database()
    known = live[COLL_UNIQUE_DOCUMENTS].find_one({"sha256": {"$ne": None},
                                                  "main_row_ids.1": {"$exists": True}})
    placed = live[COLL_DOCUMENTS].find_one({"unique_document_id": known["_id"]})
    asked = [{"state": placed["state"], "crop": placed["crop"]},
             {"state": "Nowhere", "crop": "Nothing"}]

    candidates = find_candidates(live, known["sha256"], asked)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["match_type"] == "sha" and c["score"] == 1.0
    assert c["document_code"].startswith("ANNAM_")
    # The pair it already has is filtered out; the unknown one survives.
    assert [(p["state"], p["crop"]) for p in c["new_placements"]] == [("Nowhere", "Nothing")]
    assert c["can_add"] is True
    # Byte-identical content cannot become a second document: sha256 is unique.
    assert c["can_create_new"] is False

    # Asking only for places it already has leaves nothing to add.
    assert new_placements_for(live, known, [asked[0]]) == []
    assert find_candidates(live, "0" * 64, asked) == []


def test_new_is_refused_for_an_exact_duplicate(client, db):
    """Not a policy -- sha256 is unique on unique_documents, so a second
    document for the same bytes cannot be stored at all."""
    from dashboard.models import COLL_UPLOAD_QUEUE_ITEMS, COLL_UNIQUE_DOCUMENTS

    known = db[COLL_UNIQUE_DOCUMENTS].find_one({"sha256": {"$ne": None}})
    item_id = _submit(client, placements_json='[{"state":"State Karnataka","crops":["Paddy"]}]')
    try:
        _await_review(client, item_id)
        # Stand the item's candidates up against a real document without
        # uploading its bytes: the endpoints read the stored candidates.
        db[COLL_UPLOAD_QUEUE_ITEMS].update_one({"_id": __import__("bson").ObjectId(item_id)}, {"$set": {
            "candidates": [{"document_id": str(known["_id"]),
                            "document_code": f"ANNAM_{known['display_id']:05d}",
                            "match_type": "sha", "score": 1.0, "can_add": False,
                            "can_create_new": False, "new_placements": []}]}})
        assert client.post(f"/dashboard/uploads/{item_id}/new").status_code == 409
        # ...and `add` is refused too when there is nothing new to file.
        assert client.post(f"/dashboard/uploads/{item_id}/add",
                           json={"document_id": f"ANNAM_{known['display_id']:05d}"}).status_code == 409
        # An id that is not one of the candidates cannot be attached to.
        assert client.post(f"/dashboard/uploads/{item_id}/add",
                           json={"document_id": "ANNAM_49999"}).status_code == 400
    finally:
        client.post(f"/dashboard/uploads/{item_id}/cancel")


def test_add_is_refused_when_nothing_matched(client):
    item_id = _submit(client, placements_json='[{"state":"State Karnataka","crops":["Paddy"]}]')
    try:
        item = _await_review(client, item_id)
        if item["candidates"]:
            pytest.skip("this fixture PDF is already in the catalogue")
        assert client.post(f"/dashboard/uploads/{item_id}/add").status_code == 409
    finally:
        client.post(f"/dashboard/uploads/{item_id}/cancel")


def test_upload_queue_lists(client):
    items = client.get("/dashboard/uploads").json()
    assert isinstance(items, list)
    # The queue is work in flight or waiting on someone. A finished upload
    # deletes itself, so a `done` row here means _finish stopped cleaning up.
    assert [i for i in items if i["status"] == "done"] == []


def test_a_completed_upload_leaves_the_queue(client, db, scratch):
    """The decision is the end of the item's life.

    Filing the upload onto an existing document creates the placement and then
    removes the queue entry -- the result is the row, not a `done` line someone
    has to clear by hand.
    """
    from bson import ObjectId

    from dashboard.models import COLL_DOCUMENTS, COLL_UPLOAD_QUEUE_ITEMS

    doc, rows = scratch
    before = db[COLL_DOCUMENTS].count_documents({"unique_document_id": doc["_id"]})

    # A place the scratch document is not in, using vocabulary that already
    # exists so the run does not leave a new state or crop behind.
    item_id = _submit(client, placements_json='[{"state":"State Karnataka","crops":["Paddy"]}]')
    _await_review(client, item_id)
    db[COLL_UPLOAD_QUEUE_ITEMS].update_one({"_id": ObjectId(item_id)}, {"$set": {
        "candidates": [{"document_id": str(doc["_id"]),
                        "document_code": f"ANNAM_{doc['display_id']:05d}",
                        "match_type": "sha", "score": 1.0, "can_add": True,
                        "can_create_new": False,
                        "new_placements": [{"state": "Karnataka", "crop": "Paddy"}]}]}})

    assert client.post(f"/dashboard/uploads/{item_id}/add").status_code == 200

    deadline = time.time() + 30
    while time.time() < deadline:
        if client.get(f"/dashboard/uploads/{item_id}").status_code == 404:
            break
        time.sleep(0.3)
    else:
        pytest.fail(f"upload {item_id} is still in the queue after finishing")

    assert db[COLL_DOCUMENTS].count_documents({"unique_document_id": doc["_id"]}) == before + 1
    assert [i for i in client.get("/dashboard/uploads").json() if i["id"] == item_id] == []


def test_a_manual_translation_upload_is_refused_while_a_job_runs(client, db, scratch):
    """A person can attach a translation they made elsewhere -- but not while
    the pipeline is about to write one, or the job would overwrite it."""
    from dashboard.models import COLL_TRANSLATION_JOBS, TranslationJobKind, new_translation_job

    doc, _rows = scratch
    job = new_translation_job(document_id=doc["_id"], kind=TranslationJobKind.translate)
    job_id = db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id
    try:
        resp = client.post(f"/dashboard/unique-documents/{doc['_id']}/translation",
                           files={"file": ("t.docx", b"not really a docx", "application/octet-stream")})
        assert resp.status_code == 409, resp.text
        assert "cancel" in resp.json()["detail"]
    finally:
        db[COLL_TRANSLATION_JOBS].delete_one({"_id": job_id})

    # Nothing was uploaded, so the document is untouched.
    out = client.get(f"/dashboard/unique-documents/{doc['_id']}").json()
    assert out["translation_status"] == "not_started"
    assert out["translation_file_id"] is None

    missing = "0" * 24
    assert client.post(f"/dashboard/unique-documents/{missing}/translation",
                       files={"file": ("t.docx", b"x")}).status_code == 404


def test_a_finished_translation_job_can_be_cleared(client, db):
    """The queue shows what is translating now; the result lives on the
    document. So a stopped job can be removed, and a live one cannot."""
    from dashboard.models import COLL_TRANSLATION_JOBS, TranslationJobKind, new_translation_job

    made = []
    try:
        for status in ("done", "failed", "cancelled"):
            job = new_translation_job(document_id="0" * 24, kind=TranslationJobKind.translate)
            job["status"] = status
            made.append(db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id)
            assert client.delete(f"/dashboard/translation-jobs/{made[-1]}").status_code == 204
            assert db[COLL_TRANSLATION_JOBS].find_one({"_id": made[-1]}) is None

        # Still live: it has to be cancelled first, or the worker would keep
        # writing progress to a row that no longer exists.
        job = new_translation_job(document_id="0" * 24, kind=TranslationJobKind.translate)
        made.append(db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id)
        assert client.delete(f"/dashboard/translation-jobs/{made[-1]}").status_code == 409
    finally:
        db[COLL_TRANSLATION_JOBS].delete_many({"_id": {"$in": made}})

    assert client.delete(f"/dashboard/translation-jobs/{'0' * 24}").status_code == 404
    assert client.delete("/dashboard/translation-jobs/not-an-id").status_code == 422


def test_decisions_reject_unknown_items(client):
    missing = "0" * 24
    for path in ("add", "new", "cancel"):
        assert client.post(f"/dashboard/uploads/{missing}/{path}").status_code == 404, path
    assert client.post("/dashboard/uploads/not-an-id/new").status_code == 422


# -- misc ----------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/dashboard/documents/not-an-object-id", 422),
    ("/dashboard/documents/000000000000000000000000", 404),
    ("/dashboard/unique-documents/not-an-object-id", 422),
    ("/dashboard/unique-documents/000000000000000000000000", 404),
])
def test_bad_ids(client, path, expected):
    assert client.get(path).status_code == expected


def test_config_reports_translation_availability(client):
    assert isinstance(client.get("/dashboard/config").json()["translation_available"], bool)


def test_old_flat_endpoints_are_gone(client):
    """The dedup-review flow of the previous schema, and its embedding routes."""
    for path in ("/dashboard/dedup/groups", "/dashboard/dedup/stats"):
        assert client.get(path).status_code == 404, path


def test_nothing_reads_the_previous_schema(db):
    """This schema is self-contained. `legacy_document_code` used to carry the
    old database's ANNAM code and the loader used to open that database to read
    it -- both are gone, so nothing here depends on `pop_dashboard` existing."""
    from dashboard import migrate_from_corpus
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    assert db[COLL_UNIQUE_DOCUMENTS].count_documents(
        {"legacy_document_code": {"$exists": True}}) == 0
    assert not hasattr(migrate_from_corpus, "legacy_codes_by_sha")


def test_documents_carry_only_the_agreed_fields(db):
    """The metadata set was specified explicitly. Anything else on a document is
    a field nobody asked for, and drifts into the dashboard's forms and exports
    unnoticed."""
    from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS

    allowed_doc = {
        "_id", "display_id", "sha256", "num_pages",
        "format_original", "shareable_name", "shareable_link",
        "language", "language_source",
        "advisory_type", "advisory_scope", "season", "edition_revision_volume",
        "date_of_release", "month_of_release", "year_of_release",
        "date_of_collection", "month_of_collection", "year_of_collection",
        "advisory_name", "advisory_released_org", "advisory_org_address",
        "live_source_link", "domain", "verification_status", "verified_by",
        "document_status",
        "translation_status", "translation_zoho_file_id", "translation_shareable_link",
        "review_status", "review_zoho_file_id", "review_shareable_link",
        "chunk_embeddings", "main_row_ids", "duplicate_links", "merged_from",
        "representative_file_id", "representative_row_id",
        "created_at", "updated_at",
    }
    allowed_row = {
        "_id", "row_id", "unique_document_id", "state", "state_raw",
        "crop", "crop_raw", "subpath", "source_key", "created_at", "updated_at",
    }
    allowed_copy = {"zoho_file_id", "shareable_link", "shareable_name",
                    "state", "crop", "row_id"}

    for d in db[COLL_UNIQUE_DOCUMENTS].find().limit(200):
        assert set(d) <= allowed_doc, set(d) - allowed_doc
        for link in d.get("duplicate_links") or []:
            assert set(link) <= allowed_copy, set(link) - allowed_copy
    for r in db[COLL_DOCUMENTS].find().limit(200):
        assert set(r) <= allowed_row, set(r) - allowed_row
