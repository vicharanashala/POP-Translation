"""Login, tokens and request hardening for the Source Mapper app (see INDEX.md, "Auth and exposure").

    .venv/bin/python3 anara/auth.py genusers [--force]   1 admin + 2 viewers, random passwords -> data/initial_passwords.txt
    AUTH_PASSWORD=... .venv/bin/python3 anara/auth.py adduser <name> <admin|viewer>   (or type the password on stdin)

Users: data/users.json [{username, role, password_hash}], scrypt hashes (stdlib: no argon2/bcrypt/PyJWT in the venv).
Tokens: HS256 JWT signed with data/jwt_secret. Access 15 min (cookie `sm_access`, path /), refresh 7 days (cookie
`sm_refresh`, path /auth), both HttpOnly + SameSite=Strict (+ Secure unless AUTH_COOKIE_SECURE=0). Refresh tokens are
single use: data/refresh_tokens.json keeps their sha256 per family; using a rotated one revokes the whole family.
`Authorization: Bearer <access>` is accepted for API clients. Cookie-authenticated POSTs need an X-Requested-With header.
Every route needs a valid access token except /login, /health, /auth/login, /auth/refresh, /auth/logout.
Admin only: every request that is not GET/HEAD (start/cancel a labelled test). Env: AUTH_ACCESS_TTL, AUTH_REFRESH_TTL (s),
AUTH_REFRESH_GRACE (s, see rotate), AUTH_API_RATE (expensive /api requests/min/user, 30), AUTH_API_RATE_ALL (all /api, 300),
AUTH_COOKIE_SECURE, AUTH_ALLOWED_HOSTS (comma list; default localhost, 127.0.0.1, [::1]), AUTH_ALLOWED_HOST_SUFFIXES (comma list of
".example.com", explicit opt-in), AUTH_DIR (where users.json, jwt_secret, refresh_tokens.json and allowed_hosts.txt live; default
data/), AUTH_HEAVY_TIMEOUT (s). allowed_hosts.txt (one host per line, re-read when it changes) lets expose.sh add a tunnel hostname.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import asyncio
import ipaddress
import re
import secrets
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
STATE = Path(os.environ["AUTH_DIR"]) if os.environ.get("AUTH_DIR") else DATA     # AUTH_DIR: a separate writable folder (container)
USERS_FILE, SECRET_FILE = STATE / "users.json", STATE / "jwt_secret"
REFRESH_FILE, INITIAL_FILE = STATE / "refresh_tokens.json", STATE / "initial_passwords.txt"
HOSTS_FILE = STATE / "allowed_hosts.txt"

ACCESS_TTL = int(os.environ.get("AUTH_ACCESS_TTL", 15 * 60))
REFRESH_TTL = int(os.environ.get("AUTH_REFRESH_TTL", 7 * 86400))
GRACE = float(os.environ.get("AUTH_REFRESH_GRACE", 5))   # a just-rotated token used again within this many seconds (two tabs refreshing at once) is refused without revoking the family
HEAVY_RATE = int(os.environ.get("AUTH_API_RATE", 30))        # expensive /api requests (HEAVY) per user per minute
API_RATE = int(os.environ.get("AUTH_API_RATE_ALL", 300))     # all /api requests per user per minute
HEAVY_PER_USER, HEAVY_GLOBAL = 2, 6                          # expensive requests running at once, per user and in all
HEAVY_TIMEOUT = float(os.environ.get("AUTH_HEAVY_TIMEOUT", 60))
COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "1") != "0"
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"} | {h.strip().lower() for h in os.environ.get("AUTH_ALLOWED_HOSTS", "").split(",") if h.strip()}
HOST_SUFFIXES = tuple(h.strip().lower() for h in os.environ.get("AUTH_ALLOWED_HOST_SUFFIXES", "").split(",") if h.strip().startswith("."))
MAX_BODY = 64 * 1024
LOGIN_FAILS, LOGIN_WINDOW, LOGIN_LOCK, LOGIN_LOCK_MAX = 5, 15 * 60, 15 * 60, 4 * 3600   # failures per (user, IP) before a lock; the lock doubles each time
IP_FAILS = 20            # failures from one IP (any users) before that IP is locked
USER_SOFT, SOFT_GAP = 20, 3   # failures against one user (all IPs) after which an IP that has no live session of that user may try once per SOFT_GAP s
MAX_FAILS = 50000        # hard cap on the failure-tracking tables
HEAVY = {"/api/search", "/api/groups", "/api/docs/export", "/api/known", "/api/link", "/api/random_question", "/api/test/report"}
ISS = "source-mapper"
ACCESS_COOKIE, REFRESH_COOKIE = "sm_access", "sm_refresh"
PUBLIC = {"/login", "/health", "/auth/login", "/auth/refresh", "/auth/logout"}
USERNAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
log = logging.getLogger("auth")


# ---- files ----

def _write_private(path: Path, text: str) -> None:
    """Atomic write, mode 600 from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _secret() -> bytes:
    if not SECRET_FILE.exists():
        _write_private(SECRET_FILE, base64.b64encode(secrets.token_bytes(64)).decode())
    return base64.b64decode(SECRET_FILE.read_text().strip())


# ---- passwords (scrypt) ----

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt$15$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, want = stored.split("$")
        h = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2 ** int(n), r=int(r), p=int(p),
                           maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(h, base64.b64decode(want))
    except Exception:
        return False


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))   # checked for unknown users so that they cost the same time as known ones

_users_cache: dict = {"mtime": None, "users": {}}


def users() -> dict[str, dict]:
    """{username: {username, role, password_hash}}, re-read when data/users.json changes."""
    try:
        mt = USERS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    if _users_cache["mtime"] != mt:
        _users_cache.update(mtime=mt, users={u["username"]: u for u in json.loads(USERS_FILE.read_text())})
    return _users_cache["users"]


# ---- JWT (HS256 only) ----

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_HEADER = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())


def _sign(msg: str) -> str:
    return _b64(hmac.new(_secret(), msg.encode(), hashlib.sha256).digest())


def encode(claims: dict) -> str:
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{_HEADER}.{body}.{_sign(_HEADER + '.' + body)}"


def decode(token: str, typ: str, check_exp: bool = True) -> dict | None:
    """Claims of a token we issued, else None. The header must be exactly ours (so alg none / other algs fail), the
    signature is compared in constant time, and issuer, type, subject and expiry are checked."""
    try:
        head, body, sig = token.split(".")
        if head != _HEADER or not hmac.compare_digest(sig, _sign(head + "." + body)):
            return None
        c = json.loads(_unb64(body))
        if c.get("iss") != ISS or c.get("typ") != typ or not isinstance(c.get("sub"), str) or not isinstance(c.get("fam"), str):
            return None
        if check_exp and not (isinstance(c.get("exp"), (int, float)) and c["exp"] > time.time()):
            return None
        return c
    except Exception:
        return None


# ---- refresh-token store (families) ----

_lock = threading.RLock()
_store: dict = {"families": {}, "tokens": {}}     # families: {fam: {user, revoked, exp}}; tokens: {sha256(jti): {fam, exp, used_at}}


def _load_store() -> None:
    try:
        _store.update(json.loads(REFRESH_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        pass


def _save_store() -> None:
    now = time.time()
    _store["tokens"] = {k: v for k, v in _store["tokens"].items() if v["exp"] > now}
    _store["families"] = {k: v for k, v in _store["families"].items() if v["exp"] > now}
    _write_private(REFRESH_FILE, json.dumps(_store))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _mint(user: str, role: str, fam: str, tv: int = 0, ip: str | None = None) -> tuple[str, str]:
    """A fresh access/refresh pair of family `fam`; the refresh token's jti is registered. Caller holds the lock."""
    now = int(time.time())
    jti = secrets.token_urlsafe(24)
    access = encode({"iss": ISS, "typ": "access", "sub": user, "role": role, "fam": fam, "tv": tv, "iat": now, "exp": now + ACCESS_TTL,
                     "jti": secrets.token_urlsafe(12)})
    refresh = encode({"iss": ISS, "typ": "refresh", "sub": user, "fam": fam, "tv": tv, "iat": now, "exp": now + REFRESH_TTL, "jti": jti})
    _store["tokens"][_sha(jti)] = {"fam": fam, "exp": now + REFRESH_TTL, "used_at": None}
    _store["families"].setdefault(fam, {"user": user, "revoked": False, "exp": 0, "ip": ip})["exp"] = now + REFRESH_TTL
    return access, refresh


def new_session(user: dict, ip: str | None = None) -> tuple[str, str]:
    with _lock:
        pair = _mint(user["username"], user["role"], secrets.token_urlsafe(16), user.get("tv", 0), ip)
        _save_store()
        return pair


def revoke_family(fam: str) -> None:
    with _lock:
        if fam in _store["families"]:
            _store["families"][fam]["revoked"] = True
            _save_store()


def rotate(token: str) -> tuple[str, str, dict] | str | None:
    """Exchange a refresh token for a new access/refresh pair (the old one is spent). None when the token is invalid,
    expired, unknown, its family revoked or the user's password has changed since. A spent token presented again revokes
    its whole family, except within GRACE seconds of its use (two tabs refreshing at once): then it is only refused and
    "grace" is returned (the caller must not clear the cookies: the other tab's response has just set new ones)."""
    c = decode(token, "refresh")
    if not c:
        return None
    with _lock:
        t, fam, user = _store["tokens"].get(_sha(str(c.get("jti")))), _store["families"].get(c["fam"]), users().get(c["sub"])
        if not t or not fam or fam["revoked"] or t["fam"] != c["fam"] or fam["user"] != c["sub"]:
            return None
        if not user:
            revoke_family(c["fam"])
            return None
        if c.get("tv", 0) != user.get("tv", 0):          # the password was changed after this session started
            revoke_family(c["fam"])
            return None
        if t["used_at"] is not None:
            if time.time() - t["used_at"] > GRACE:
                log.warning("refresh token reuse: family of %s revoked", c["sub"])
                revoke_family(c["fam"])
                return None
            return "grace"
        t["used_at"] = time.time()
        access, refresh = _mint(user["username"], user["role"], c["fam"], user.get("tv", 0))
        _save_store()
        return access, refresh, user


def user_of_access(token: str) -> dict | None:
    c = decode(token, "access")
    if not c:
        return None
    fam, user = _store["families"].get(c["fam"]), users().get(c["sub"])
    if not fam or fam["revoked"] or not user or c.get("tv", 0) != user.get("tv", 0):
        return None
    return {"username": user["username"], "role": user["role"]}      # the role in users.json, not the one in the token


# ---- rate limits (in memory) ----
# Login: a failure slot is reserved under the lock BEFORE the (slow) password check and given back on success, so parallel
# guesses cannot get past the limit. Locks are per (user, IP) and per IP, never per user alone, so an attacker cannot lock
# a real account out for everybody; each repeated lock doubles (up to LOGIN_LOCK_MAX, strikes forgotten after a day).
# Against one user from many IPs there is only a soft limit (USER_SOFT), and it spares IPs with a live session of that user.

_fails: dict[str, dict] = {}        # key ("pair:user|ip" / "ip:ip") -> {t: [failure times], until, strikes, last}
_user_soft: dict[str, dict] = {}    # user -> {t: [attempt times], last}
_hits: dict[str, deque] = {}
_login_slots = threading.BoundedSemaphore(4)    # password checks running at once (each ~70 ms and 32 MB)
_heavy_now: dict[str, int] = {}
_heavy_total = [0]


def _norm_ip(ip: str) -> str:
    """The key of a client address: IPv6 by its /64 (a host owns the whole prefix), IPv4-mapped IPv6 as IPv4."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "?"
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


def _prune(now: float) -> None:
    """Called when a new key arrives: only at MAX_FAILS entries does it do work (so a flood of new keys costs O(1) each, amortised):
    drop idle entries, then the least recently used (unlocked first) down to 75% of the cap."""
    for tab in (_fails, _user_soft):
        if len(tab) < MAX_FAILS:
            continue
        for k in [k for k, v in tab.items() if v.get("until", 0) < now and all(now - x > LOGIN_WINDOW for x in v["t"])
                  and now - v.get("last", 0) > LOGIN_WINDOW]:
            del tab[k]
        if len(tab) >= MAX_FAILS * 0.9:
            for k in sorted(tab, key=lambda k: (tab[k].get("until", 0) > now, tab[k].get("last", 0)))[:len(tab) - MAX_FAILS * 3 // 4]:
                del tab[k]


def _entry(key: str, now: float) -> dict:
    f = _fails.get(key)
    if f is None:
        _prune(now)
        f = _fails[key] = {"t": [], "until": 0.0, "strikes": 0, "last": now}
    f["last"] = now
    return f


def _known_ip(name: str, ip: str) -> bool:
    now = time.time()
    return any(not f["revoked"] and f["exp"] > now and f["user"].lower() == name and f.get("ip") == ip
               for f in _store["families"].values())


def _reserve(ip: str, name: str):
    """(wait_seconds, ticket): wait > 0 refuses the attempt; otherwise the failure slot is taken and `ticket` goes to _settle."""
    now = time.time()
    with _lock:
        fp, fi = _entry(f"pair:{name}|{ip}", now), _entry(f"ip:{ip}", now)
        for f in (fp, fi):
            if f["until"] > now:
                return int(f["until"] - now) + 1, None
        us = _user_soft.get(name)
        if us is None:
            _prune(now)
            us = _user_soft[name] = {"t": [], "last": 0.0}
        us["t"] = [x for x in us["t"] if now - x < LOGIN_WINDOW]
        if len(us["t"]) >= USER_SOFT and not _known_ip(name, ip):
            if now - us["last"] < SOFT_GAP:
                return int(SOFT_GAP - (now - us["last"])) + 1, None
            us["last"] = now
        ticket = []
        for f, limit in ((fp, LOGIN_FAILS), (fi, IP_FAILS)):
            snap = list(f["t"])
            f["t"] = [x for x in f["t"] if now - x < LOGIN_WINDOW] + [now]
            locked = False
            if len(f["t"]) >= limit:
                if f["strikes"] and now - f["until"] > 86400:
                    f["strikes"] = 0
                f["until"], f["t"], locked = now + min(LOGIN_LOCK_MAX, LOGIN_LOCK * 2 ** f["strikes"]), [], True
                f["strikes"] += 1
            ticket.append((f, snap, locked))
        us["t"].append(now)
        return 0, (ticket, us, now)


def _settle(ticket, ok: bool) -> None:
    """A successful login gives its failure slot back (and undoes a lock it triggered); a failed one keeps it."""
    if ok:
        ticket_, us, now = ticket
        with _lock:
            for n, (f, snap, locked) in enumerate(ticket_):
                if locked:
                    f["until"], f["strikes"], f["t"] = 0.0, f["strikes"] - 1, snap
                elif now in f["t"]:
                    f["t"].remove(now)
                if n == 0:
                    f["t"] = []                 # the (user, IP) pair is cleared by a success
            if now in us["t"]:
                us["t"].remove(now)


def _hit(key: str, limit: int) -> bool:
    """One request of `key` within `limit` per minute?"""
    now = time.time()
    with _lock:
        q = _hits.get(key)
        if q is None:
            if len(_hits) > MAX_FAILS:
                _hits.clear()
            q = _hits[key] = deque()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _heavy_acquire(user: str) -> bool:
    with _lock:
        if _heavy_now.get(user, 0) >= HEAVY_PER_USER or _heavy_total[0] >= HEAVY_GLOBAL:
            return False
        _heavy_now[user] = _heavy_now.get(user, 0) + 1
        _heavy_total[0] += 1
        return True


def _heavy_release(user: str) -> None:
    with _lock:
        _heavy_now[user] = _heavy_now.get(user, 1) - 1
        _heavy_total[0] -= 1
        if _heavy_now[user] <= 0:
            _heavy_now.pop(user, None)


_hosts_cache: dict = {"mtime": None, "hosts": set()}


def _host_ok(header: str) -> bool:
    """Is the Host header one of ours: localhost, AUTH_ALLOWED_HOSTS, a line of allowed_hosts.txt, or (opt-in) a suffix?"""
    h = header.strip().lower()
    if h.startswith("["):
        h = h[:h.find("]") + 1]
    elif h.count(":") == 1:
        h = h.split(":")[0]
    if h in ALLOWED_HOSTS or (HOST_SUFFIXES and h.endswith(HOST_SUFFIXES)):
        return True
    try:
        mt = HOSTS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return False
    if _hosts_cache["mtime"] != mt:
        _hosts_cache.update(mtime=mt, hosts={x.strip().lower() for x in HOSTS_FILE.read_text().splitlines()
                                             if x.strip() and not x.lstrip().startswith("#")})
    return h in _hosts_cache["hosts"]


def safe_next(n: str) -> bool:
    """A same-site path for the login redirect: one leading slash, no backslash, no control character or space."""
    return n.startswith("/") and not n.startswith(("//", "/\\")) and not re.search(r"[\x00-\x20\\\x7f]", n)


# ---- helpers used by app.py ----

_PATH = re.compile(r"(?<![\w:])/(?:[\w.\-]+/)+[\w.\-]*")


def scrub(text) -> str:
    """Error text with filesystem paths replaced, for responses."""
    return _PATH.sub("<path>", str(text))


CLIENT_JS = r"""(()=>{const f=window.fetch.bind(window);let rp=null;
const call=(u,o)=>{o=Object.assign({},o||{});const h=new Headers(o.headers||{});h.set('X-Requested-With','sm');o.headers=h;o.credentials='same-origin';return f(u,o);};
const refresh=()=>rp||(rp=f('/auth/refresh',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'sm'}}).then(r=>r.ok).catch(()=>false).finally(()=>{rp=null;}));
window.fetch=async(u,o)=>{let r=await call(u,o);
 if(r.status===401&&!String(u).startsWith('/auth/')){await refresh();r=await call(u,o);   // retry once: also covers another tab having just refreshed
  if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname+location.search);}}
 return r;};
document.addEventListener('click',async e=>{const a=e.target.closest&&e.target.closest('#logout');if(!a)return;e.preventDefault();
 await f('/auth/logout',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'sm'}});location.href='/login';});
})();"""


def page(name: str, request: Request, client: bool = True) -> HTMLResponse:
    """An HTML file of this folder with the request's CSP nonce on every <script>, and the fetch wrapper (adds the CSRF
    header, refreshes the access token once on a 401, handles #logout) in front of the first one."""
    html = (HERE / name).read_text()
    n = request.state.nonce
    if client:
        i = html.index("<script")
        html = html[:i] + f"<script>{CLIENT_JS}</script>" + html[i:]
    return HTMLResponse(re.sub(r"<script(?=[\s>])", f'<script nonce="{n}"', html))


# ---- endpoints ----

router = APIRouter()


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    return_tokens: bool = False


class RefreshBody(BaseModel):
    refresh_token: str | None = Field(None, max_length=4096)


def _cookies(resp: Response, access: str, refresh: str) -> None:
    kw = dict(httponly=True, secure=COOKIE_SECURE, samesite="strict")
    resp.set_cookie(ACCESS_COOKIE, access, max_age=ACCESS_TTL, path="/", **kw)
    resp.set_cookie(REFRESH_COOKIE, refresh, max_age=REFRESH_TTL, path="/auth", **kw)


def _clear(resp: Response) -> None:
    kw = dict(httponly=True, secure=COOKIE_SECURE, samesite="strict")
    resp.delete_cookie(ACCESS_COOKIE, path="/", **kw)
    resp.delete_cookie(REFRESH_COOKIE, path="/auth", **kw)


@router.post("/auth/login")
def login(body: Login, request: Request):
    ip = _norm_ip(request.client.host if request.client else "?")
    name = body.username.lower()[:64]
    if not _login_slots.acquire(blocking=False):
        return JSONResponse({"detail": "Busy, try again"}, 429, headers={"Retry-After": "2"})
    try:
        wait, ticket = _reserve(ip, name)
        if wait:
            return JSONResponse({"detail": "Too many attempts, try again later"}, 429, headers={"Retry-After": str(wait)})
        u = users().get(body.username) if USERNAME.fullmatch(body.username) else None
        ok = verify_password(body.password, u["password_hash"] if u else DUMMY_HASH) and u is not None
        _settle(ticket, ok)
    finally:
        _login_slots.release()
    if not ok:
        return JSONResponse({"detail": "Invalid username or password"}, 401)
    access, refresh = new_session(u, ip)
    resp = JSONResponse({"username": u["username"], "role": u["role"],
                         **({"access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL} if body.return_tokens else {})})
    _cookies(resp, access, refresh)
    return resp


@router.post("/auth/refresh")
def refresh(request: Request, body: RefreshBody | None = None):
    tok = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE) or ""
    res = rotate(tok)
    if res == "grace":                  # another tab has just rotated this token: its response sets the new cookies
        return JSONResponse({"detail": "Not authenticated"}, 401)
    if not res:
        resp = JSONResponse({"detail": "Not authenticated"}, 401)
        _clear(resp)
        return resp
    access, new_refresh, u = res
    resp = JSONResponse({"username": u["username"], "role": u["role"],
                         **({"access_token": access, "refresh_token": new_refresh, "expires_in": ACCESS_TTL} if body and body.refresh_token else {})})
    _cookies(resp, access, new_refresh)
    return resp


@router.post("/auth/logout")
def logout(request: Request):
    for cookie, typ in ((REFRESH_COOKIE, "refresh"), (ACCESS_COOKIE, "access")):
        c = decode(request.cookies.get(cookie, ""), typ, check_exp=False)
        if c:
            revoke_family(c["fam"])
    resp = JSONResponse({"ok": True})
    _clear(resp)
    return resp


@router.get("/auth/me")
def me(request: Request):
    return request.state.user


@router.get("/health")
def health():
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _user_of_request(request)[0]:
        return RedirectResponse("/", 303)
    return page("login.html", request, client=False)


# ---- middleware ----

def _user_of_request(request: Request) -> tuple[dict | None, bool]:
    """(user, via_bearer). An Authorization header is used alone; otherwise the access cookie."""
    h = request.headers.get("authorization", "")
    if h:
        return (user_of_access(h[7:].strip()) if h[:7].lower() == "bearer " else None), True
    return user_of_access(request.cookies.get(ACCESS_COOKIE, "")), False


def _deny(request: Request, status: int, detail: str) -> Response:
    p = request.url.path
    if status == 401 and request.method in ("GET", "HEAD") and not p.startswith(("/api/", "/auth/")) \
            and "text/html" in request.headers.get("accept", ""):
        nxt = p + ("?" + request.url.query if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe="") if safe_next(nxt) else "/login", 303)
    return JSONResponse({"detail": detail}, status)


def _authorize(request: Request) -> Response | None:
    """None when the request may go on (request.state.user set), else the refusal."""
    p, method = request.url.path, request.method
    unsafe = method not in ("GET", "HEAD", "OPTIONS")
    if not _host_ok(request.headers.get("host", "")):
        return JSONResponse({"detail": "Not found"}, 404)
    if p.startswith("/auth/") and unsafe and not request.headers.get("x-requested-with"):
        return JSONResponse({"detail": "Missing X-Requested-With header"}, 403)
    if p in PUBLIC:
        return None
    user, bearer = _user_of_request(request)
    if not user:
        return _deny(request, 401, "Not authenticated")
    if unsafe and not bearer and not request.headers.get("x-requested-with"):
        return JSONResponse({"detail": "Missing X-Requested-With header"}, 403)
    if unsafe and user["role"] != "admin":
        return JSONResponse({"detail": "Admin role required"}, 403)
    if p.startswith("/api/") and not _hit("a:" + user["username"], API_RATE):
        return JSONResponse({"detail": "Too many requests"}, 429, headers={"Retry-After": "30"})
    if p in HEAVY and not _hit("h:" + user["username"], HEAVY_RATE):
        return JSONResponse({"detail": "Too many requests"}, 429, headers={"Retry-After": "30"})
    request.state.user = user
    return None


async def guard(request: Request, call_next):
    request.state.nonce = secrets.token_urlsafe(16)
    request.state.user = None
    heavy = None
    try:
        resp = _authorize(request)
        if resp is None and request.url.path in HEAVY:
            heavy = request.state.user["username"]
            if not _heavy_acquire(heavy):
                heavy, resp = None, JSONResponse({"detail": "Too many requests running, try again shortly"}, 429, headers={"Retry-After": "5"})
        if resp is None:
            try:
                resp = await asyncio.wait_for(call_next(request), HEAVY_TIMEOUT if heavy else None)
            except asyncio.TimeoutError:        # the worker thread finishes by itself; the thread pool is capped in app.py
                resp = JSONResponse({"detail": "The request took too long"}, 504)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        resp = JSONResponse({"detail": "Internal error"}, 500)
    finally:
        if heavy:
            _heavy_release(heavy)
    n = request.state.nonce
    resp.headers.update({
        "Content-Security-Policy": f"default-src 'self'; script-src 'self' 'nonce-{n}'; style-src 'self' 'unsafe-inline'; "
                                   "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                                   "form-action 'self'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000", "Cache-Control": "no-store",
        "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
    return resp


class _TooBig(Exception):
    pass


class BodyLimit:
    """Pure ASGI: refuses a request body over MAX_BODY bytes (declared or streamed) with 413."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, total = False, 0

        async def send2(m):
            nonlocal started
            started = started or m["type"] == "http.response.start"
            await send(m)

        async def receive2():
            nonlocal total
            m = await receive()
            if m["type"] == "http.request":
                total += len(m.get("body", b""))
                if total > MAX_BODY:
                    raise _TooBig
            return m

        cl = dict(scope["headers"]).get(b"content-length", b"0")
        try:
            if cl.isdigit() and int(cl) > MAX_BODY:
                raise _TooBig
            await self.app(scope, receive2, send2)
        except _TooBig:
            if not started:
                await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)


def install(app) -> None:
    """Routes, the guard on every request, body limit, generic error bodies. App must be created with docs_url=None,
    redoc_url=None, openapi_url=None."""
    if not USERS_FILE.exists():
        raise SystemExit("data/users.json is missing: run  .venv/bin/python3 anara/auth.py genusers")
    _secret()
    _load_store()
    app.include_router(router)
    app.middleware("http")(guard)
    app.add_middleware(BodyLimit)

    async def invalid(request, exc):
        return JSONResponse({"detail": "Invalid request"}, 422)

    async def failure(request, exc):
        return JSONResponse({"detail": "Internal error"}, 500)

    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(Exception, failure)


# ---- command line ----

def _add(name: str, role: str, password: str) -> None:
    if not USERNAME.fullmatch(name) or role not in ("admin", "viewer"):
        raise SystemExit("usage: adduser <name: letters, digits, _ . -> <admin|viewer>")
    if len(password) < 12:
        raise SystemExit("password must be at least 12 characters")
    cur = {u["username"]: u for u in json.loads(USERS_FILE.read_text())} if USERS_FILE.exists() else {}
    # "tv" (token version) goes up when a user is replaced, so every session and token of the old password stops working
    cur[name] = {"username": name, "role": role, "password_hash": hash_password(password),
                 "tv": cur[name].get("tv", 0) + 1 if name in cur else 0}
    _write_private(USERS_FILE, json.dumps(list(cur.values()), indent=1))


def main(argv: list[str]) -> None:
    if argv[:1] == ["adduser"] and len(argv) == 3:
        pw = os.environ.get("AUTH_PASSWORD") or (__import__("getpass").getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n"))
        _add(argv[1], argv[2], pw)
        print(f"user {argv[1]} ({argv[2]}) saved")
    elif argv[:1] == ["genusers"]:
        if USERS_FILE.exists() and "--force" not in argv:
            raise SystemExit("data/users.json exists; pass --force to replace all users")
        if USERS_FILE.exists():
            USERS_FILE.unlink()
        REFRESH_FILE.unlink(missing_ok=True)       # sessions of the replaced users must not survive: a running app still holds the
        SECRET_FILE.unlink(missing_ok=True)        # old store in memory, so the signing key is replaced too (the app makes a new one)
        lines = []
        for name, role in (("admin", "admin"), ("viewer1", "viewer"), ("viewer2", "viewer")):
            pw = secrets.token_urlsafe(18)
            _add(name, role, pw)
            lines.append(f"{name}\t{role}\t{pw}")
        _write_private(INITIAL_FILE, "\n".join(lines) + "\n")
        print(f"3 users written to {USERS_FILE.name}; their passwords are in {INITIAL_FILE} (mode 600). Delete that file once stored.")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
