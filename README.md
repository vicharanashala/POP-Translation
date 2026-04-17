# POP-Translation

A Gemma-based translation pipeline for converting agricultural POP (Package of Practices) pages from source markdown into English markdown and PDF outputs while preserving document structure, table layout and technical agricultural terminology.

## Overview

`POP-Translation` is a page-oriented translation workflow designed for agricultural documentation. The pipeline operates on raw source markdown as the input, translates page content through a hosted Gemma model and then converts translated markdown into PDF deliverables.

The repository is intentionally focused on reusable pipeline code and prompt assets. It does **not** currently include source datasets, generated outputs, archives or local virtual environment files.

## Repository Structure

```text
POP-Translation/
├── prompts/
│   └── raw_markdown_prompt.txt
├── scripts/
│   ├── gemma_client.py
│   ├── run_raw_markdown_translation.py
│   └── markdown_to_pdf_batch_nosudo.py
└── .gitignore
```

## Core Components

`scripts/gemma_client.py` 

Reusable API client for the hosted Gemma model.

Responsibilities:
- Reads endpoint, model name and optional API key from environment variables
- Sends the translation prompt together with source markdown to the model
- Returns translated markdown output
- Keeps model communication logic separate from batch execution logic


`scripts/run_raw_markdown_translation.py`

Batch runner for markdown translation across page folders.

Responsibilities:
- Scans page-wise input folders
- Reads source markdown for each page
- Loads the translation prompt from file
- Calls the Gemma client
- Writes translated markdown outputs
- Records success and failure status in a summary file


`scripts/markdown_to_pdf_batch_nosudo.py`

Batch markdown-to-PDF conversion utility.

Responsibilities:
- Reads translated markdown outputs
- Converts markdown files into PDFs
- Uses a Python-based conversion approach


`prompts/raw_markdown_prompt.txt`

Primary prompt used by the raw markdown translation workflow.

Responsibilities:
- Preserves markdown structure
- Preserves table layout
- Maintains fidelity for technical agricultural terminology
- Reduces semantic substitution during translation

## Workflow

The pipeline follows a two-stage process:

1. Raw markdown translation
- Source POP pages are provided as page-wise markdown inputs
- Each page is translated independently using the hosted Gemma model
- Translated markdown is written to the output directory
- A summary log captures successes and failures

2. PDF generation
- Translated markdown outputs are used as the source for rendering
- Markdown files are converted into PDF deliverables in batch mode

This page-based workflow is intentionally designed to support easier validation, QA, and targeted correction of individual pages.

## Model and Configuration

### Hosted Model Endpoint
- Endpoint: http://100.100.108.44:8013/v1/chat/completions
- Model: google/gemma-4-26B-A4B-it

### Generation Settings
- temperature: 0.0
- max_tokens: 8192
- top_p: 0.95
- timeout: 300

## Usage

### Prerequisites

Before running the pipeline:
- Ensure the environment variables expected by `scripts/gemma_client.py` are configured
- Prepare page-wise source markdown inputs locally
- Use a Python environment with the dependencies required by the scripts

### Run Translation

```
python3 scripts/run_raw_markdown_translation.py \
  --input_root ./data/pages/Your_Folder_Name \
  --output_root ./outputs/Output_Folder_Name \
  --prompt_file ./prompts/raw_markdown_prompt.txt
```

This step:
- Reads source markdown page folders from `--input_root`
- Loads the translation prompt from `--prompt_file`
- Generates translated markdown under `--output_root`
- Writes execution status information to a summary log

### Generate PDFs from Translated Markdown

```
./pdf_env/bin/python scripts/markdown_to_pdf_batch_nosudo.py \
  --input_root ./outputs/Input_Folder_Name \
  --output_root ./outputs/Output_Folder_Name
```

This step:
- Reads translated markdown files from `--input_root`
- Converts them into PDFs
- Writes PDF outputs to `--output_root`

## Notes and Limitations

- The quality of translation depends on the hosted model behavior and the effectiveness of the prompt.
- Markdown preservation is a design goal but complex formatting may still require manual review.
- Table fidelity is prioritized but edge cases in malformed or highly irregular source markdown may need correction after translation.
- PDF generation is a separate downstream step and may surface formatting differences relative to the markdown source.
- The repository currently provides the pipeline implementation only; users are responsible for supplying their own input data and managing local output directories.
