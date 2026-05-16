# POP Translation Pipeline

This repository contains a production-oriented pipeline for translating agricultural POP (Package of Practices) PDF documents into English using the Gemini API and generating editable Word (`.docx`) outputs for review by agriculture experts.

The pipeline was designed for POP documents that contain agricultural instructions, tables, chemical names, crop names, pest/disease names, formulation codes, units, doses, percentages, images, and structured technical content.

---

## Objective

The objective of this project is to translate priority agricultural POP documents into English while preserving the document structure as much as possible.

The final output is generated as an editable `.docx` file so that agriculture experts can easily:

- review translations,
- correct technical terms,
- edit table values,
- add missing points,
- add comments or suggestions,
- and finalize the document for downstream use.

---

## Current Pipeline

The current pipeline processes a source PDF page-by-page.

```text
Source PDF
   ↓
Split into page-wise PDFs
   ↓
Send each page PDF to Gemini
   ↓
Generate translated HTML per page
   ↓
Extract and inject original page images
   ↓
Merge translated HTML pages
   ↓
Convert combined HTML to DOCX using Pandoc
   ↓
Final editable Word file
```

The pipeline is implemented in:
```scripts/run_pop_to_docx.py```

The translation prompt is stored separately in:
```prompts/page_to_pdf.txt```

## Repository Structure
```
POP-Translation/
├── prompts/
│   └── page_to_pdf.txt
│
├── scripts/
│   └── run_pop_to_docx.py
│
├── requirements.txt
├── README.md
└── .gitignore
```

Recommended local working structure:
```
POP_Work/
├── Data/
│   └── Karnataka/
│       ├── Ginger/
│       ├── Onion/
│       ├── Bengal gram/
│       ├── Sugarcane/
│       └── ...
│
├── Workdir/
│   └── Karnataka/
│       └── <Crop>/
│           └── <Document_Output_Folder>/
│
├── prompts/
├── scripts/
├── requirements.txt
└── README.md
```

## Main Features

- Page-wise PDF splitting
- Gemini-based translation
- HTML output generation
- Original image extraction from PDF pages
- Image injection into translated HTML
- Combined HTML generation
- DOCX generation using Pandoc
- Resume support
- Page-wise intermediate outputs
- Runtime logging
- Parallel page translation using concurrency
- Optional page range processing
- Skip translation and rebuild DOCX from existing HTML
- Safe reruns without overwriting completed pages

## Gemini Translation Configuration

Current configuration:
```
Model: gemini-3.1-pro-preview
Thinking level: HIGH
Google Search: Enabled
Input: Page-wise PDF bytes
Output: Clean translated HTML
```

## Requirements

Recommended Python version:
```
Python 3.11+
```

Install required Python packages using:
```
pip install -r requirements.txt
```

Pandoc is required for converting combined HTML into .docx.
Install Pandoc separately and check installation:
```
pandoc --version
```

## Environment Setup

1. Clone the repository:
```
git clone https://github.com/vicharanashala/POP-Translation.git
cd POP-Translation
```
2. Create virtual environment:
```
python -m venv venv
venv\Scripts\activate
```
3. Install dependencies:
```
pip install -r requirements.txt
```
4. Set Gemini API key:
```
set GEMINI_API_KEY=PASTE_YOUR_GEMINI_API_KEY_HERE
```

## Input Folder

Place source PDFs inside the Data/ folder.
Example:
```
Data/
└── Karnataka/
    └── Ginger/
        └── Rhizome rot management in ginger_KVK Krishi Vigyana Kendra, Shivamogga, Karnataka.pdf
```

## Basic Usage

Run the pipeline from the project root.
Example:
```
python scripts\run_pop_to_docx.py --source-pdf "Data\Karnataka\Ginger\Rhizome rot management in ginger_KVK Krishi Vigyana Kendra, Shivamogga, Karnataka.pdf" --workdir-root "Workdir\Karnataka\Ginger\Rhizome rot management in ginger_KVK Krishi Vigyana Kendra, Shivamogga, Karnataka" --doc-name "Rhizome rot management in ginger_KVK Krishi Vigyana Kendra, Shivamogga, Karnataka" --prompt-file "prompts\page_to_pdf.txt" --start-page 1 --end-page 1 --concurrency 1
```

## Important Command Arguments

Argument	Purpose:

- --source-pdf :	Path to input PDF
- --workdir-root :	Working/output folder for that document
- --doc-name :	Base name used for combined HTML and DOCX
- --prompt-file :	Path to translation prompt
- --start-page :	First page to process
- --end-page :	Last page to process
- --concurrency :	Number of pages translated in parallel
- --overwrite :	Reprocess existing outputs
- --skip-translation :	Skip Gemini translation and reuse existing translated.html
- --skip-image-injection :	Skip image extraction/injection and reuse existing final_with_images.html

## Resume Behaviour

The pipeline is resumable.

Each page has its own folder:
```
page_001/
├── page_001.pdf
├── translated.html
├── images/
└── final_with_images.html
```
If the script stops midway, rerun the same command without --overwrite.

The script will skip completed pages:
```
translated.html already exists
final_with_images.html already exists
```
Only missing pages will be processed again.

## Output Structure

For each processed document, the output folder contains:
```
Workdir/<State>/<Crop>/<Document_Name>/
├── page_001/
│   ├── page_001.pdf
│   ├── translated.html
│   ├── images/
│   └── final_with_images.html
│
├── page_002/
│   ├── page_002.pdf
│   ├── translated.html
│   ├── images/
│   └── final_with_images.html
│
├── translation_summary.json
├── image_injection_summary.json
├── pipeline_runtime.log
├── <doc-name>_combined_pages_001_to_XXX.html
└── final_output/
    └── <doc-name>_translated_pages_001_to_XXX.docx
```

Final DOCX output is saved inside:
```
final_output/
```

## Runtime Logs

The script creates a runtime log:
```
pipeline_runtime.log
```

This log records:

- pipeline configuration,
- page splitting status,
- translation progress,
- Gemini request timing,
- retry attempts,
- image injection progress,
- combined HTML generation,
- DOCX conversion,
- final output path.

## Parallel Processing

The pipeline supports concurrent page translation.
Example:
```
--concurrency 2
```
Higher concurrency may increase speed, but can also increase the chance of API rate-limit errors or stuck requests.

## Known Limitations

1. Pandoc may produce incomplete DOCX files for some very large or complex HTML documents.
2. Some documents may need chunk-wise DOCX generation.
3. A few problematic pages may need to be generated separately. 
4. The script currently assumes Pandoc is installed and available in system PATH.
5. API rate limits may affect concurrency. 
6. Output quality depends on the source PDF quality and Gemini response.
