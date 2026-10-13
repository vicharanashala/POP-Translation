"""Who is calling, and what they may do.

THREE KINDS OF PERMISSION, and only one of them is a role:

    admin            agriai.users role == "admin". Everything, and the only
                     one who may translate or manage the two lists below.
    edit approver    a user id in the "edit" list. Everything an admin can do
                     in the documents / main table, except translation.
    upload approver  a user id in the "upload" list. Reviews what experts
                     upload before it reaches an admin (see routes_uploads).

Everyone else -- experts, moderators, gate keepers, any other role -- is an
expert here: they may browse, download and upload, and nothing else.

IDENTITY IS TAKEN FROM HEADERS, not verified. The frontend sends `X-User-Id`
(the user's agriai.users `_id`) and `X-User-Role` on every request. Checking a
Firebase token instead would make the claim unforgeable, but it would still
leave the role to look up, and this cluster's production login cannot read
agriai.users at all -- it is granted crop_master, states, districts, kvks and
organizations, nothing else. So the role is believed as sent, which is no
weaker than before this module existed (the backend checked nothing), and the
two LISTS are checked here against our own collection, which a caller cannot
forge its way into.

The lists live in `pop_approvers`, one row per (list, user). The user's name
and email are stored with the id, by whoever added them, for the same reason:
nothing here can look them up later.

Reads stay open. A write, or a read whose answer depends on who is asking (the
upload queue, the translation queue, /me), without an `X-User-Id` is a 401 --
not treated as an anonymous expert, which would hand back an empty queue and
look like a working answer.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from dashboard.db import get_db
from dashboard.models import COLL_APPROVERS

EDIT_LIST = "edit"
UPLOAD_LIST = "upload"
LISTS = (EDIT_LIST, UPLOAD_LIST)


@dataclass(frozen=True)
class Caller:
    user_id: str
    role: str
    can_edit: bool
    can_approve_uploads: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_translate(self) -> bool:
        return self.is_admin


def in_list(db, list_name: str, user_id: str) -> bool:
    return db[COLL_APPROVERS].count_documents({"list": list_name, "user_id": user_id}, limit=1) > 0


def caller(request: Request, db=Depends(get_db)) -> Caller:
    user_id = (request.headers.get("x-user-id") or "").strip()
    if not user_id:
        raise HTTPException(401, "X-User-Id header is required -- send the signed-in user's id")
    role = (request.headers.get("x-user-role") or "").strip().lower()
    is_admin = role == "admin"
    return Caller(
        user_id=user_id,
        role=role,
        can_edit=is_admin or in_list(db, EDIT_LIST, user_id),
        can_approve_uploads=in_list(db, UPLOAD_LIST, user_id),
    )


def require_admin(who: Caller = Depends(caller)) -> Caller:
    if not who.is_admin:
        raise HTTPException(403, "only an admin can do this")
    return who


def require_editor(who: Caller = Depends(caller)) -> Caller:
    """Main-table edits: an admin, or someone in the edit list."""
    if not who.can_edit:
        raise HTTPException(403, "only an admin or an edit approver can change documents")
    return who
