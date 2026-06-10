# POP Translation — Documentation Index

This directory contains the complete technical documentation for the POP Translation service.

---

## Documents

| File | What it covers |
|------|---------------|
| [architecture.md](architecture.md) | System overview, component map, data-flow diagrams, storage layout |
| [pipeline.md](pipeline.md) | The 5-stage PDF-to-DOCX translation pipeline in depth |
| [api.md](api.md) | Full REST API reference — every endpoint, request/response shape, error codes |
| [zoho.md](zoho.md) | Zoho WorkDrive client internals, auth lifecycle, retry/upload strategy |
| [jobs.md](jobs.md) | Background job system — submission, lifecycle, progress tracking, cancellation |
| [deployment.md](deployment.md) | Docker, environment variables, CI/CD pipeline, running locally |

---

## One-paragraph summary

POP Translation is a FastAPI service that translates agricultural Package-of-Practices (POP) PDF documents from regional Indian languages into English and produces editable `.docx` files. Source PDFs and output files live in **Zoho WorkDrive**; local disk is used only as a temporary scratch space during an active pipeline run. A **master.json** file cached in Zoho acts as the single source of truth for document state. Translation is performed page-by-page using the **Gemini API** (default model `gemini-3.1-pro-preview`, `thinking_level=HIGH`, Google Search enabled). The final DOCX is built by converting each translated HTML page with **Pandoc** and merging the per-page DOCX files with **docxcompose**.
