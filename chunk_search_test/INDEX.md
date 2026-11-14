# chunk_search_test: index

Standalone local test: do chunk embeddings find the right document and page for a query?
Nothing here imports `dashboard/`. Run everything with `.venv/bin/python3 chunk_search_test/<script>`.

## Decisions
- Embedding model: `BAAI/bge-small-en-v1.5` (English, CPU, 384 dims). Not multilingual-e5.
- Source: `pop-documents-state-2026-11-12.xlsx` (709 Punjab placements, 533 unique documents).
- Original PDFs are used (no document is translated); only English-text ones are embedded.
- Mongo: the user's own Atlas account (not yet created). Collections `unique_documents`, `doc_embeddings`.
- Pause and tell the user at ~300k context.

## Files
| File | Purpose | Reusable API |
|---|---|---|
| `common.py` | paths, env loading, sheet to unique docs, Zoho client | `load_documents()`, `pdf_path(doc_id, fmt)`, `zoho()`, `DATA`, `PDF_DIR` |
| `download_pdfs.py` | Download each unique doc via `representative_file_id`; skips files on disk | `--limit N --workers N` |
| `chunker.py` | pages -> chunks of 180 words, 30 overlap, never across pages | `chunk_pages(pages)` |
| `embedder.py` | bge-small-en-v1.5, normalised, CPU; first load ~90 s, then ~30 chunks/s | `embed_passages(list)`, `embed_query(str)` |
| `db.py` | Atlas connection from `MONGO_URI` in `.env`; DB `chunk_search_test` | `get_db()`, `ensure_collections(db)` |
| `ingest.py` | english_text docs -> `unique_documents` + `doc_embeddings`; skips stored docs, `--redo`, `--limit` | script |
| `search.py` | vector index + query -> docs ranked by matching chunk count (ties by summed score) | `search(query, k, local)`, `create-index` |
| `textcheck.py` | Extractable text + English check per PDF, writes `data/text_report.csv` | `extract_pages(path)`, `latin_ratio`, `lang_of`, `check` |

## Data (gitignored)
`data/pdfs/<doc_id>.<fmt>`, `data/text_report.csv` (verdict: english_text, scanned_no_text, non_english, mixed, not_pdf).

## Findings
- Sheet language field: 436 eng, 97 pan. Formats: 455 pdf, 37 url, 23 printed, 11 png, 7 jpg.
- A scanned PDF has no text layer (example: Contact Number Bathinda KVK, 2 pages, 1 char); these need OCR or are skipped.

## Results (all 533 downloaded, 1.2 GB, 0 failures; textcheck takes ~45 s)
| verdict | docs |
|---|---|
| english_text | 306 (11,555 pages, ~23M chars, ~19k chunks at 200 words) |
| scanned_no_text | 135 (834 pages; would need OCR, `tesseract` is installed) |
| not_pdf (png/jpg/url/printed) | 78 |
| mixed / non_english (Gurmukhi) | 7 / 6 |
| unreadable | 1 |
- The catalogued `language` is unreliable: 36 docs tagged `pan` have English text.
- MuPDF prints corrupt-stream warnings on a few files; text still extracts.
- ~19k chunks x 384 dims fits the Atlas M0 limit (about 30 MB of vectors).

## Atlas state
Cluster `pop-retrieval-embedding-test` (M0), DB `chunk_search_test`, collections `unique_documents` (_id = POP unique doc id) and `doc_embeddings`.
Vector index `chunk_vector_index` (384 dims, cosine) is created. Usage: `search.py "question" [-k 50] [--local]`.
Ranking caveat: counting matching chunks favours long documents (a 100-page doc has more chunks to match). Check in evaluation.
