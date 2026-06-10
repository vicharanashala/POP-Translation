# Deployment

---

## Environment variables

All secrets are read from environment or a `.env` file (loaded via `python-dotenv` at startup).

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes (unless `skip_translation=true`) | Google Gemini API key |
| `ZOHO_CLIENT_ID` | Yes | Zoho OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | Yes | Zoho OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | Yes | Zoho long-lived refresh token |
| `ZOHO_ROOT_FOLDER_ID` | Yes | ID of the root WorkDrive folder (parent of `Data/` and `Workdir/`) |

The server fails to connect to Zoho on startup if these are missing, but it does not crash — a warning is logged and the Zoho singleton is retried on first request.

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
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - ZOHO_CLIENT_ID=${ZOHO_CLIENT_ID}
      - ZOHO_CLIENT_SECRET=${ZOHO_CLIENT_SECRET}
      - ZOHO_REFRESH_TOKEN=${ZOHO_REFRESH_TOKEN}
      - ZOHO_ROOT_FOLDER_ID=${ZOHO_ROOT_FOLDER_ID}
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

`network_mode: host` is used so the container shares the host network stack (common for internal deployments without a reverse proxy).

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
GEMINI_API_KEY=your_key
ZOHO_CLIENT_ID=your_id
ZOHO_CLIENT_SECRET=your_secret
ZOHO_REFRESH_TOKEN=your_token
ZOHO_ROOT_FOLDER_ID=your_folder_id
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
