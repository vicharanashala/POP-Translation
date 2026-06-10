# Zoho WorkDrive Integration

`helpers/zoho_workdrive.py` — self-contained REST client for Zoho WorkDrive India (`workdrive.zoho.in`).

---

## Authentication

Zoho uses OAuth 2.0 with a **refresh token** flow. No user-facing login is required at runtime.

### Credentials (env vars)

| Variable | Description |
|---|---|
| `ZOHO_CLIENT_ID` | OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | Long-lived refresh token (obtained once during setup) |
| `ZOHO_ROOT_FOLDER_ID` | Zoho folder ID of the root folder (found in the WorkDrive URL) |

### Token lifecycle

1. On `ZohoWorkDrive.__init__()`, `_refresh_access_token()` is called immediately — gets a fresh access token from `https://accounts.zoho.in/oauth/v2/token`.
2. The token is stored in-memory only (`self._access_token`). Never written to disk.
3. A **proactive refresh daemon thread** (`zoho-token-refresh`) fires every 45 minutes.
4. Every HTTP method calls `_ensure_token_fresh()` first — proactively refreshes if the token is >50 minutes old.
5. Any `401` response triggers an **on-demand refresh** and one retry.

```
Request → _ensure_token_fresh() → GET/POST/PATCH/DELETE
                                     │
                                  401? → _refresh_access_token() → retry once
```

### Session

A `requests.Session` is created with:
- `Authorization: Zoho-oauthtoken <token>` header
- `urllib3.util.retry.Retry(total=3, backoff_factor=1, status_forcelist=[429, 502, 503])` — automatic retry on transient failures

---

## Core operations

### `list_folder(folder_id) → list[dict]`

Paginates through `GET /api/v1/files/{folder_id}/files` (50 items per page).

Each item: `{ "id": "...", "name": "...", "type": "folder"|"file", "size": N }`

### `find_child(parent_id, name) → dict | None`

Linear scan of `list_folder()` for an exact name match. Used as a building block for path resolution and folder creation.

### `resolve_path(path) → (id, type) | None`

Walks a slash-separated path from `ZOHO_ROOT_FOLDER_ID`, resolving each segment with `find_child()`. Returns `(file_id, type_string)` or `None` if any segment is missing.

```python
result = zwd.resolve_path("Data/Karnataka/Ginger/doc1.pdf")
# → ("6a2b3c...", "file")  or  None
```

### `ensure_path(path) → folder_id`

Like `resolve_path`, but creates any missing folders along the way using `get_or_create_folder()`. Returns the ID of the final folder.

### `get_or_create_folder(name, parent_id) → folder_id`

Checks for an existing child folder first. If the `POST /api/v1/files` (create folder) call returns `422` (already exists — common race), falls back to `find_child()`.

---

## File upload

### Small files (≤ 50 MB) — `upload_file_stream`

`POST /api/v1/upload` with multipart form:
- `filename`, `parent_id`, `override-name-exist: true`
- `content` — file handle streamed directly

### Large files (> 50 MB) — `upload_file_resumable`

Three-step chunked upload:
1. `POST /api/v1/upload/resumable` — initiate, receive `upload_id`
2. `POST /api/v1/upload/resumable/{upload_id}` — upload 10 MB chunks with `Content-Range` headers
3. `POST /api/v1/upload/resumable/{upload_id}/commit` — finalize

### `upload_local_file(local_path, zoho_folder_path)`

High-level helper: calls `ensure_path()` then routes to streaming or resumable upload based on file size.

### ID extraction quirk

When `override-name-exist=true` is used, Zoho sometimes omits the file ID from the response. The client falls back to `list_folder(parent_id)` and takes the **last** matching entry (most recent upload).

### `upload_file(filename, content_bytes, parent_id)`

Uploads bytes directly (in-memory). Used for `master.json` and small audit files. Falls back to `list_folder()` for ID if the response is ambiguous.

---

## File download

### `download_file(file_id) → bytes`

Tries two URLs in order:
1. `GET /api/v1/download/{file_id}`
2. `GET /api/v1/files/{file_id}/content`

Skips responses where `Content-Type` is `json` or `html` (redirect pages rather than actual file content).

### `download_file_stream(file_id) → requests.Response`

Same URL order, same Content-Type check. Returns the raw streaming response. Caller must close it. Used by `/download/{path}` and `/output` endpoints.

---

## Delete / rename / move

All use `PATCH /api/v1/files/{file_id}` with JSON:API payload.

| Operation | Payload attribute |
|---|---|
| `delete(id)` | `"status": "51"` (moves to Zoho trash) |
| `rename(id, name)` | `"name": "<new_name>"` |
| `move(id, parent_id)` | `"parent_id": "<new_parent_id>"` |

`delete()` returns `True` on `200` or `204`.

---

## Tree builder and caching

`_build_zoho_tree(zoho_path)` in `pop_server.py` recursively walks a Zoho folder and returns a nested dict:

```json
{
  "name": "Data",
  "path": "Data",
  "type": "directory",
  "children": [ ... ]
}
```

Results are cached with a **60-second TTL** (`_tree_cache`). Separate caches for `Data/` and `Workdir/`. Mutations that affect these trees call `_invalidate_data_cache()` or `_invalidate_workdir_cache()`.

---

## Singleton pattern

`pop_server.py` holds one `ZohoWorkDrive` instance (`_zoho_instance`) protected by `_zoho_lock`. All routes call `_get_zoho()`, which initializes the singleton lazily on first call using double-checked locking.

---

## Required OAuth scopes

`WorkDrive.files.ALL`

Obtaining the refresh token (one-time setup):
1. Create a Self Client app at `https://api-console.zoho.in`
2. Generate an authorization code with scope `WorkDrive.files.ALL`
3. Exchange the code for tokens via the Zoho token endpoint
4. Store `refresh_token`, `client_id`, `client_secret`, and `root_folder_id` in `.env`
