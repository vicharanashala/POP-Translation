# Deployment

---

## Environment variables

All secrets are read from environment or a `.env` file (loaded via `python-dotenv` at startup).

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | No | Translation backend: `gemini` (default) or `minimax` |
| `GEMINI_API_KEY` | Yes, if `LLM_PROVIDER=gemini` (unless `skip_translation=true`) | Google Gemini API key |
| `MINIMAX_API_KEY` | Yes, if `LLM_PROVIDER=minimax` (unless `skip_translation=true`) | MiniMax API key |
| `ZOHO_CLIENT_ID` | Yes | Zoho OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | Yes | Zoho OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | Yes | Zoho long-lived refresh token |
| `ZOHO_ROOT_FOLDER_ID` | Yes | ID of the root WorkDrive folder (parent of `Data/` and `Workdir/`) |
| `DB_URL` | Yes, for the dashboard (`/dashboard/*` routes) | MongoDB Atlas connection string, e.g. `mongodb+srv://<user>:<password>@cluster0.xxxxx.mongodb.net/` |
| `DASHBOARD_DB_NAME` | No (defaults to `pop_dashboard`) | Database on that cluster. Note `DB_NAME` is a *different* application's database on the same cluster and is deliberately not used by the dashboard |
| `ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID` / `ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID` / `ZOHO_DASHBOARD_REVIEWS_FOLDER_ID` | Yes, for the dashboard | Three fixed, pre-created WorkDrive subfolder IDs the dashboard uploads into directly — separate from `ZOHO_ROOT_FOLDER_ID`. **Not** a single root folder ID — `dashboard/config.py` reads these three names specifically (fixed 2026-08-30: `docker-compose.yml` previously passed through an unused `ZOHO_DASHBOARD_ROOT_FOLDER_ID` instead, so these three never reached the container and every upload's `/new` step failed with `"... is not set in .env"`) |
| `DUPLICATE_EMBEDDING_THRESHOLD` | No | Cosine similarity cutoff for flagging a duplicate upload (default `0.95`) |
| `TRANS` | No | `on` (default) or `off` — kill switch for the dashboard's per-document translate button, independent of whether an LLM key is set. Current production deploy uses `off`: document management only, translate button reads "out of order" (see `docs/dashboard_backend_plan.md` §7) |

The server fails to connect to Zoho on startup if these are missing, but it does not crash — a warning is logged and the Zoho singleton is retried on first request. The same applies to the dashboard's MongoDB connection: `dashboard/db.py` only connects lazily, on first actual use, so a missing `DB_URL` doesn't prevent the rest of the server from starting — only `/dashboard/*` routes fail until it's set. See `docs/dashboard_backend_plan.md` for the full dashboard design.

### Database

The dashboard runs on **hosted MongoDB Atlas** — there is no database container to
operate, back up, or upgrade. `init_db()` creates every collection index at
startup, including the **Atlas Vector Search** index that powers duplicate
detection; that one takes roughly 25 seconds to build the first time and reports
`PENDING` until it is queryable.

Atlas limits how many search indexes a cluster may hold (this cluster allows
**two**, and another application already uses one). If duplicate detection ever
reports no candidates on a non-empty catalogue, check that
`unique_documents.embedding_vector_index` still exists and is `queryable`.

---

## Docker

### Image

```
vicharanashala/pop-translation:latest
```

Published to Docker Hub via GitHub Actions on every push to `main`.

### Dockerfile summary

```dockerfile
FROM python:3.11-slim
RUN apt-get install -y pandoc
WORKDIR /app
COPY requirements.lock .
RUN pip install -r requirements.lock
COPY . .
RUN mkdir -p pop-data/POP_Work/Data pop-data/POP_Work/Workdir
VOLUME ["/app/pop-data"]
EXPOSE 8032
CMD ["uvicorn", "pop_server:app", "--host", "0.0.0.0", "--port", "8032"]
```

Key points:
- **Pandoc** is installed as a system package (required for HTML→DOCX conversion).
- **`pop-data/`** is declared as a Docker volume — pipeline scratch files survive container restarts mid-job.
- Python **3.11-slim** base (the `pyproject.toml` targets 3.11+; the lock file uses 3.12 locally).

### docker-compose.yml

```yaml
# The dashboard's database is hosted MongoDB Atlas (DB_URL) rather than a
# container: the previous pgvector/Postgres `db` service and its `pgdata`
# volume are gone, along with DATABASE_URL/POSTGRES_USER/POSTGRES_PASSWORD.
#
# Duplicate detection now runs on an Atlas Vector Search index, which is
# created by the application at startup (dashboard/db.py::init_db) and takes
# ~25s to build the first time.
services:
  pipeline:
    image: vicharanashala/pop-translation:latest
    network_mode: host
    expose:
      - "8032"
    volumes:
      - pop-data:/app/pop-data
    environment:
      - LLM_PROVIDER=${LLM_PROVIDER:-gemini}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - MINIMAX_API_KEY=${MINIMAX_API_KEY}
      - ZOHO_CLIENT_ID=${ZOHO_CLIENT_ID}
      - ZOHO_CLIENT_SECRET=${ZOHO_CLIENT_SECRET}
      - ZOHO_REFRESH_TOKEN=${ZOHO_REFRESH_TOKEN}
      - ZOHO_ROOT_FOLDER_ID=${ZOHO_ROOT_FOLDER_ID}
      # MongoDB Atlas. DB_URL is the cluster connection string; the dashboard
      # uses its own database on it (DB_NAME in .env belongs to a different
      # application on the same cluster and is deliberately not used here).
      - DB_URL=${DB_URL}
      - DASHBOARD_DB_NAME=${DASHBOARD_DB_NAME:-pop_dashboard}
      - ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID=${ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID}
      - ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID=${ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID}
      - ZOHO_DASHBOARD_REVIEWS_FOLDER_ID=${ZOHO_DASHBOARD_REVIEWS_FOLDER_ID}
      - DUPLICATE_EMBEDDING_THRESHOLD=${DUPLICATE_EMBEDDING_THRESHOLD:-0.95}
      - TRANS=${TRANS:-on}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8032/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    restart: unless-stopped

volumes:
  pop-data:
```

`network_mode: host` is used so the container shares the host network stack (common for internal deployments without a reverse proxy). There is only one service now — the database is hosted Atlas, reached over the internet via `DB_URL`.

## Database setup (first run on a new environment)

The dashboard's catalogue is loaded from the CSVs it was originally built from,
so setting up a fresh environment needs no database dump and no Postgres. There
was never any dashboard-authored data in Postgres beyond what these CSVs
contain, so this load is complete, not a partial restore.

**1. Load the catalogue** (~7,500 documents, ~8,600 placements, a few minutes):

```bash
DASHBOARD_DB_NAME=pop_dashboard .venv/bin/python3 -m dashboard.migrate_from_pops_csv
```

Reads `results/pops.csv`, `results/main.csv` and
`results/report_true_embeddings.json`. Resumable — documents already present by
`sha256`, and placements already present by (document, state, crop), are skipped
rather than duplicated, so it is safe to re-run.

**2. Confirm the vector index is queryable.** `init_db()` creates it at startup,
but it takes ~25 seconds to build and reports `PENDING` until ready:

```bash
DASHBOARD_DB_NAME=pop_dashboard .venv/bin/python3 -c \
  "from dashboard.db import get_database, vector_index_ready; \
   print('queryable:', vector_index_ready(get_database()))"
```

> **M0 caps the number of Atlas Search indexes (3 on this tier), and they are
> shared cluster-wide with other applications.** All three slots are currently
> in use — `pop_dashboard`, `pop_dashboard_test`, and another application's
> `prod_copy_db.questions` — so creating a fourth will fail with
> `"The maximum number of FTS indexes has been reached for this instance size"`.
> If that happens, drop one you no longer need:
>
> ```bash
> DASHBOARD_DB_NAME=<db> .venv/bin/python3 -c \
>   "from dashboard.db import get_database; \
>    get_database().unique_documents.drop_search_index('embedding_vector_index')"
> ```
>
> Note that dropping a collection drops its search index with it, so a
> drop-and-reload cycle frees the slot automatically.

Without a queryable index, exact-SHA duplicate detection still works but
embedding similarity silently returns no candidate. Check this before opening
uploads.

**3. Deploy** with `DB_URL` and `DASHBOARD_DB_NAME` set in `.env`.

### Cluster capacity — read before loading a second copy

The Atlas cluster is an **M0 free tier: 512 MB total, shared with unrelated
applications** (`prod_copy_db` alone uses ~310 MB, roughly 60% of the quota).
Exceeding it blocks writes **cluster-wide**, including for those other
applications — not just for the dashboard.

Two things keep the dashboard inside its share:

- **Embeddings are stored as Atlas float32 binary vectors**, not BSON arrays of
  doubles: ~3,095 bytes per document instead of ~9,895. `new_unique_document()`
  applies this on every write path, so the migration and the upload queue both
  produce the compact form. It is not a precision change — pgvector stored
  float4 (32-bit), so this is exactly what Postgres held.
  `scripts/convert_embeddings_to_float32.py` converts a legacy collection in
  place (it refuses to run against any database outside the dashboard's own).
- **A full catalogue is ~35 MB** (30 MB data + ~5 MB indexes). Two copies
  (`pop_dashboard` + `pop_dashboard_test`) fit with roughly 110 MB to spare.

**Freed space is not returned automatically.** WiredTiger keeps it, and Atlas
blocks the `compact` command on M0, so deleting or rewriting a lot of documents
leaves `storageSize` high even though `dataSize` has dropped. The only way to
reclaim it on this tier is to drop the collection (or database) and re-run the
migration — which is cheap, since the load is reproducible from the CSVs.

Check usage with:

```bash
.venv/bin/python3 -c "
from dashboard.config import DB_URL; from pymongo import MongoClient
c = MongoClient(DB_URL)
for n in sorted(c.list_database_names()):
    if n in ('admin','local','config'): continue
    s = c[n].command('dbStats')
    print(f\"{n:24} {s['storageSize']/1e6:7.1f}M storage  {s['indexSize']/1e6:6.1f}M idx\")"
```


### Starting with docker-compose

```bash
# create .env with secrets, then:
docker-compose up -d
docker-compose logs -f
```

---

## Running locally (development)

```bash
# 1. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.lock

# 3. Install Pandoc (system package)
# Ubuntu/Debian:
sudo apt-get install pandoc
# macOS:
brew install pandoc

# 4. Create .env
cat > .env <<EOF
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key
MINIMAX_API_KEY=your_key
ZOHO_CLIENT_ID=your_id
ZOHO_CLIENT_SECRET=your_secret
ZOHO_REFRESH_TOKEN=your_token
ZOHO_ROOT_FOLDER_ID=your_folder_id
DB_URL="mongodb+srv://<user>:<password>@cluster0.xxxxx.mongodb.net/"
DASHBOARD_DB_NAME=pop_dashboard
ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID=your_originals_subfolder_id
ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID=your_translations_subfolder_id
ZOHO_DASHBOARD_REVIEWS_FOLDER_ID=your_reviews_subfolder_id
EOF

# 5. Start server
uvicorn pop_server:app --host 0.0.0.0 --port 8032 --reload
```

---

## CI/CD pipeline

`.github/workflows/build-pipeline.yml` — triggers on pushes to `main`, `feature/**`, or `fix/**` branches when any of these paths change:

```
pop_server.py
_job_ctl.py
scripts/**
prompts/**
Dockerfile
requirements.lock
requirements.txt
pyproject.toml
```

Steps:
1. **Checkout** code
2. **Set up Docker Buildx**
3. **Log in to Docker Hub** (secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`)
4. **Build and push** two tags:
   - `vicharanashala/pop-translation:latest`
   - `vicharanashala/pop-translation:<git-sha>`
5. GitHub Actions **layer cache** (`cache-from: type=gha`) speeds up rebuilds.

---

## Startup sequence

When the server starts (`lifespan` context manager):

1. Creates `pop-data/POP_Work/Workdir/` and `/tmp/pop_chunks/` directories.
2. Initializes the Zoho WorkDrive singleton (token refresh, session setup).
3. Spawns a daemon thread to **load or build master.json**:
   - If `master.json` exists in Zoho root: downloads and parses it (`_master_ready` event set).
   - If not (or parse fails): walks the entire `Data/` and `Workdir/` trees to rebuild it.
   - `GET /state-table` returns `{"status": "loading"}` until this completes.

---

## Dependencies

Core runtime:

| Package | Purpose |
|---|---|
| `fastapi` | HTTP framework |
| `uvicorn[standard]` | ASGI server (with uvloop, websockets) |
| `pydantic` | Request/response models |
| `python-multipart` | Multipart form uploads |
| `google-genai` | Gemini API client |
| `PyMuPDF` (`fitz`) | PDF splitting and image extraction |
| `beautifulsoup4` | HTML parsing for image injection |
| `python-docx` | DOCX base document creation |
| `docxcompose` | Merging multiple DOCX files |
| `pandoc` (system) | HTML → DOCX conversion |
| `requests` | Zoho WorkDrive HTTP client |
| `python-dotenv` | `.env` file loading |

Pin versions in `requirements.lock` for reproducible builds. `requirements.txt` is the loose top-level list used as a reference.

---

## Port

The server listens on **port 8032**. This is hardcoded in `Dockerfile` and `docker-compose.yml`. To change it, update both files and the `uvicorn` CMD arguments.

---

## Volume mount

The `pop-data/` directory is mounted as a Docker named volume. This ensures:

- In-progress pipeline scratch directories survive container restarts.
- The `POP_Work/Workdir/` tree is not lost if the container is updated mid-job.

In production, after each document finishes, the local scratch is deleted (`shutil.rmtree`) and all persistent data lives in Zoho — so the volume only needs to hold data for actively running jobs.
