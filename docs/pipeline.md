# Translation Pipeline

The pipeline lives in `scripts/run_pop_to_docx_updated_pagewise_docx.py`. It is both importable (used by `pop_server.py`) and runnable as a standalone CLI.

---

## Overview

```
Source PDF
   ↓  Stage 1 — split_pdf_to_page_folders
Per-page PDFs  (page_001.pdf … page_NNN.pdf)
   ↓  Stage 2 — translate_pages
Per-page HTML  (translated.html)
   ↓  Stage 3 — inject_images_for_pages
Per-page HTML with images  (final_with_images.html)
   ↓  Stage 4 — convert_pages_to_individual_docx
Per-page DOCX  (page_docx/<stem>_page_NNN.docx)
   ↓  Stage 5 — merge_docx_files_in_order
Final DOCX  (final_output/<stem>_translated_pages_001_to_NNN.docx)
```

Each stage writes its own summary JSON to the workdir. All stages check a cancellation event (`ctl.check_cancel()`) between pages.

---

## Stage 1 — PDF splitting (`split_pdf_to_page_folders`)

**Input:** `source_pdf_path` (temp file streamed from Zoho)  
**Output:** list of `Path` objects — one per page PDF

Uses **PyMuPDF** (`fitz`) to extract each page as a single-page PDF:

```
workdir/<stem>/
├── page_001/page_001.pdf
├── page_002/page_002.pdf
└── ...
```

Behaviour:
- If a page PDF already exists and `overwrite=False`, that page is skipped (`reused` count in log).
- Validates `start_page` / `end_page` bounds against the actual PDF page count.
- Logs `selected_pages=N` — this value is parsed by `_parse_log_pages()` to show progress in the job status API.

---

## Stage 2 — Translation (`translate_pages` / `process_translation_for_page`)

**Input:** list of page PDFs  
**Output:** `translated.html` written to each page folder

### Gemini call

```python
client.models.generate_content_stream(
    model=req.model,          # default: "gemini-3.1-pro-preview"
    contents=[
        types.Content(role="user", parts=[
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        ])
    ],
    config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
        tools=[types.Tool(googleSearch=types.GoogleSearch())],
    ),
)
```

Key points:
- **Streaming**: chunks are collected and joined; progress is logged every 30 seconds.
- **Google Search** is enabled by default (helps with agricultural term accuracy). Disable with `disable_google_search=true`.
- **Thinking level HIGH** gives more accurate structured output at the cost of speed.
- **Retry loop**: up to `max_retries` (default 5) attempts with `retry_wait_seconds` (default 15) between each.
- **Empty response** from Gemini triggers a retry.
- On success, `clean_html()` strips accidental markdown code fences and saves the HTML.

### Concurrency modes

| `concurrency` | Behaviour |
|---|---|
| `1` (default) | Sequential; pages processed one at a time. Progress counters are exact. |
| `>1` | `ThreadPoolExecutor` — N pages in parallel. Useful for large documents on fast networks. Rate-limit errors more likely. |

### Skip mode

If `skip_translation=true`, the stage is bypassed entirely and existing `translated.html` files are used. This is useful for re-running image injection or DOCX conversion without calling Gemini.

### Output summary

`translation_summary.json` — list of per-page results:

```json
[
  {
    "page": "page_001",
    "status": "success",          // "success" | "skipped" | "failed"
    "output_file": "...translated.html",
    "duration_seconds": 42.3,
    "finished_at": "2026-06-08 10:05:00"
  }
]
```

---

## Stage 3 — Image injection (`inject_images_for_pages`)

**Input:** page PDFs + `translated.html`  
**Output:** `final_with_images.html` in each page folder; `images/image_N.ext` files

### Image extraction

Uses `fitz.page.get_image_info(xrefs=True)` (rendered occurrences, not the global resource list) to find images actually displayed on the page. Images smaller than 30×30 px are ignored as artifacts.

### Placeholder matching

Gemini is instructed to write `<figure class="image-placeholder">[IMAGE]</figure>` wherever an image appeared in the source. The injector:

1. Finds all `<figure class="image-placeholder">` elements.
2. Inserts an `<img src="images/image_N.ext">` tag at the start of each figure.
3. Removes the `image-placeholder` class.

**Fallback:** If Gemini returned plain `[IMAGE]` instead of a `<figure>`, the injector replaces them in order with `<figure><img ...></figure>` blocks.

**Storage guard:** If `translated.html` contains no image placeholder at all, the stage writes `final_with_images.html` as a copy of `translated.html` and skips image extraction entirely — preventing disk blowup on PDFs with shared global image resource tables.

### Skip mode

If `skip_image_injection=true`, existing `final_with_images.html` files are reused.

### Output summary

`image_injection_summary.json` — list of per-page results:

```json
[
  {
    "page": "page_003",
    "status": "success",
    "embedded_image_count": 2,
    "placeholder_count": 2,
    "matched_count": 2,
    "output_file": "...final_with_images.html",
    "duration_seconds": 0.8
  }
]
```

---

## Stage 4 — Page-wise DOCX conversion (`convert_pages_to_individual_docx`)

**Input:** page PDFs (used only for metadata/ordering)  
**Output:** one `.docx` per page in `final_output/page_docx/`

For each page:

1. `build_single_page_docx_html()` wraps `final_with_images.html` in a complete `<!DOCTYPE html>` document with embedded CSS (Noto Sans / Noto Sans Devanagari fonts for Indic script compatibility).
2. Writes `page_NNN_single_page_docx.html` into the page folder (relative image paths stay valid).
3. Runs **Pandoc** via subprocess:
   ```
   pandoc page_NNN_single_page_docx.html -f html -t docx -o <stem>_page_NNN.docx
   ```
   `cwd` is set to the page folder so relative `images/` paths resolve correctly.

`check_pandoc_available()` is called once at the start of the stage — fails fast with a clear message if Pandoc is missing.

### Output summary

`page_docx_summary.json` — per-page results with `status`, timing, and output file path.

---

## Stage 5 — DOCX merge (`merge_docx_files_in_order`)

**Input:** list of page DOCX files (in page order)  
**Output:** `final_output/<stem>_translated_pages_001_to_NNN.docx`

Uses **docxcompose** (`Composer`): opens the first page DOCX as the master document, then appends all subsequent pages via `composer.append()`. Progress is logged every 25 files.

---

## Prompt

`prompts/page_to_pdf.txt` (15 rules):

The prompt instructs Gemini to:
- Return **only HTML** (no Markdown, no code fences).
- Preserve all structure: headings, tables, lists, captions, reading order.
- Preserve technical terms exactly: chemical names, crop names, pest names, doses, units, percentages.
- Never summarize, paraphrase, or add content.
- Use `<figure class="image-placeholder">[IMAGE]</figure>` for images.

---

## CLI usage

```bash
python scripts/run_pop_to_docx_updated_pagewise_docx.py \
  --source-pdf "path/to/document.pdf" \
  --workdir-root "Workdir/Karnataka/Ginger/document_stem" \
  --doc-name "document_stem" \
  --prompt-file "prompts/page_to_pdf.txt" \
  --model "gemini-3.1-pro-preview" \
  --start-page 1 \
  --end-page 10 \
  --concurrency 1 \
  --overwrite
```

| Argument | Default | Description |
|---|---|---|
| `--source-pdf` | (required) | Path to source PDF |
| `--workdir-root` | (required) | Working/output directory |
| `--doc-name` | PDF stem | Base name for output files |
| `--prompt-file` | `prompts/page_to_pdf.txt` | Translation prompt |
| `--provider` | `LLM_PROVIDER` env var, else `gemini` | `gemini` or `minimax` |
| `--model` | `gemini-3.1-pro-preview` / `MiniMax-M3` per provider | Model name |
| `--start-page` | `1` | First page (1-indexed) |
| `--end-page` | last page | Last page (1-indexed) |
| `--concurrency` | `1` | Parallel translation workers |
| `--overwrite` | false | Reprocess already-completed pages |
| `--max-retries` | `5` | Gemini retries per page |
| `--retry-wait-seconds` | `15` | Wait between retries |
| `--disable-google-search` | false | Turn off Google Search tool |
| `--skip-translation` | false | Reuse existing `translated.html` |
| `--skip-image-injection` | false | Reuse existing `final_with_images.html` |
| `--reference-docx` | none | Pandoc reference style DOCX |

---

## Resume behaviour

The pipeline is fully resumable. Each stage checks `overwrite_existing` before doing work:

- Stage 1: skips pages where `page_NNN.pdf` already exists.
- Stage 2: skips pages where `translated.html` already exists.
- Stage 3: skips pages where `final_with_images.html` already exists.
- Stage 4: skips pages where the per-page DOCX already exists.

Re-run the same command (without `--overwrite`) to pick up only failed or missing pages.

---

## Runtime log

`pipeline_runtime.log` is appended each time the pipeline runs (separated by a `===` banner). It records:

- Configuration
- Page split totals
- Per-page Gemini request timing, chunk counts, retry attempts
- Translation, image injection, DOCX conversion summaries
- Aggregate stats: avg page time, pages/hour

`_parse_log_pages()` in `pop_server.py` reads this file to drive real-time job progress:
- `selected_pages=N` → total page count
- `Translation progress | completed N/M` → pages done so far
