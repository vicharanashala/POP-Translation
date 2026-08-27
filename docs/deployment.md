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
| `DATABASE_URL` | Yes, for the dashboard (`/dashboard/*` routes) | Postgres connection string, e.g. `postgresql+psycopg://pop_dashboard:<password>@localhost:5432/pop_dashboard` |
| `ZOHO_DASHBOARD_ROOT_FOLDER_ID` | Yes, for the dashboard | Root WorkDrive folder for the dashboard's own `originals/`/`translations/`/`reviews/` layout — separate from `ZOHO_ROOT_FOLDER_ID` |
| `DUPLICATE_EMBEDDING_THRESHOLD` | No | Cosine similarity cutoff for flagging a duplicate upload (default `0.95`) |
| `TRANS` | No | `on` (default) or `off` — kill switch for the dashboard's per-document translate button, independent of whether an LLM key is set. Current production deploy uses `off`: document management only, translate button reads "out of order" (see `docs/dashboard_backend_plan.md` §7) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | Yes, for the `db` compose service | Credentials for the containerized Postgres instance |

The server fails to connect to Zoho on startup if these are missing, but it does not crash — a warning is logged and the Zoho singleton is retried on first request. The same applies to the dashboard's Postgres connection: `dashboard/db.py` only connects lazily, on first actual use, so a missing `DATABASE_URL` doesn't prevent the rest of the server from starting — only `/dashboard/*` routes fail until it's set. See `docs/dashboard_backend_plan.md` for the full dashboard design.

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
      - DATABASE_URL=${DATABASE_URL}
      - ZOHO_DASHBOARD_ROOT_FOLDER_ID=${ZOHO_DASHBOARD_ROOT_FOLDER_ID}
      - DUPLICATE_EMBEDDING_THRESHOLD=${DUPLICATE_EMBEDDING_THRESHOLD:-0.95}
      - TRANS=${TRANS:-on}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8032/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    restart: unless-stopped

  db:
    image: pgvector/pgvector:pg16
    network_mode: host
    environment:
      - POSTGRES_DB=pop_dashboard
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  pop-data:
  pgdata:
```

`network_mode: host` is used so both containers share the host network stack (common for internal deployments without a reverse proxy) — `pipeline` reaches Postgres via `localhost:5432`, not the compose service name `db`, since host networking means there's no compose-internal DNS between them.

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
DATABASE_URL=postgresql+psycopg://pop_dashboard:your_password@localhost:5432/pop_dashboard
ZOHO_DASHBOARD_ROOT_FOLDER_ID=your_dashboard_folder_id
POSTGRES_USER=pop_dashboard
POSTGRES_PASSWORD=your_password
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
