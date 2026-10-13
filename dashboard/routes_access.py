"""The signed-in user's permissions, and the two lists an admin manages.

    GET    /me                                  what THIS caller may do
    GET    /user-management/{edit|upload}       a list's members
    POST   /user-management/{edit|upload}       add one   {"user_id", "name", "email"}
    DELETE /user-management/{edit|upload}/{uid} remove one

`/me` exists so the frontend gates its buttons on the same answer the backend
enforces, rather than re-deriving it from the role and a second list fetch.

The lists are admin-only, reads included: who may approve is not something an
expert needs to see. Members are users of the main application (agriai.users),
picked from the frontend's user dropdown. Their id is checked only for SHAPE:
production cannot read agriai.users, so there is nothing to check it against,
and the name and email are stored as sent for the same reason -- see
dashboard/access.py.
"""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from dashboard.access import LISTS, Caller, caller, require_admin
from dashboard.db import get_db
from dashboard.models import COLL_APPROVERS, utcnow
from dashboard.schemas import ApproverIn, ApproverOut, MeOut

router = APIRouter()


@router.get("/me", response_model=MeOut)
def me(who: Caller = Depends(caller)):
    return MeOut(user_id=who.user_id, role=who.role, is_admin=who.is_admin,
                 can_edit=who.can_edit, can_approve_uploads=who.can_approve_uploads,
                 can_translate=who.can_translate)


def _list_name(list_name: str) -> str:
    if list_name not in LISTS:
        raise HTTPException(404, f"no such list -- use one of {', '.join(LISTS)}")
    return list_name


def _out(row: dict) -> ApproverOut:
    return ApproverOut(**{k: row.get(k) for k in ApproverOut.model_fields})


@router.get("/user-management/{list_name}", response_model=list[ApproverOut])
def list_members(list_name: str, _: Caller = Depends(require_admin), db=Depends(get_db)):
    rows = db[COLL_APPROVERS].find({"list": _list_name(list_name)}).sort(
        [("added_at", ASCENDING), ("_id", ASCENDING)])
    return [_out(row) for row in rows]


@router.post("/user-management/{list_name}", response_model=ApproverOut, status_code=201)
def add_member(list_name: str, body: ApproverIn, who: Caller = Depends(require_admin),
               db=Depends(get_db)):
    user_id = body.user_id.strip()
    if not ObjectId.is_valid(user_id):
        raise HTTPException(400, "user_id must be the user's agriai.users id (24 hex characters)")
    row = {"list": _list_name(list_name), "user_id": user_id,
           "name": (body.name or "").strip() or None, "email": (body.email or "").strip() or None,
           "added_by": who.user_id, "added_at": utcnow()}
    try:
        db[COLL_APPROVERS].insert_one(row)
    except DuplicateKeyError:
        raise HTTPException(409, f"that user is already in the {list_name} list")
    return _out(row)


@router.delete("/user-management/{list_name}/{user_id}", status_code=204)
def remove_member(list_name: str, user_id: str, _: Caller = Depends(require_admin),
                  db=Depends(get_db)):
    result = db[COLL_APPROVERS].delete_one({"list": _list_name(list_name), "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(404, f"that user is not in the {list_name} list")
