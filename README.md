# POP Translation Pipeline

A notebook-based pipeline for translating agricultural POP (Package of Practices) document pages directly from PDF into English while preserving structure as closely as possible.

This project uses a **PDF-page translation workflow** with Gemini. In testing, this method produced stronger overall results and better structure preservation, especially when paired with the **Gemini Pro** model for difficult pages containing complex tables.

---

## Overview

The pipeline is designed to process a source POP document in a controlled, page-range-based workflow.

### Final workflow

1. Split the source POP document into **page-wise PDF files**
2. Send each page PDF directly to **Gemini** for English translation
3. Save the translated output as **Markdown**
4. Convert translated Markdown into **page-wise PDF files**
5. Merge all translated page PDFs into one **final translated PDF**

---

## Repository Structure

```text
POP-Translation/
├── notebooks/
│   ├── 01_document_pdf_to_pagewise_pdf.ipynb
│   ├── 02_page_pdf_to_translated_output.ipynb
│   ├── 03_translated_output_to_page_pdf.ipynb
│   └── 04_merge_page_pdfs_to_final_pdf.ipynb
├── prompts/
│   └── gemini_direct_pdf_translation_prompt.txt
├── .gitignore
└── README.md
```

---

## Notebook Pipeline

1. ```01_document_pdf_to_pagewise_pdf.ipynb```
Splits the source POP document into page-wise PDF files for a selected page range.

2. ```02_page_pdf_to_translated_output.ipynb```
Reads each page-wise PDF and sends it directly to Gemini for English translation while attempting to preserve structure.

3. ```03_translated_output_to_page_pdf.ipynb```
Converts each translated Markdown file into a page-wise PDF.

4. ```04_merge_page_pdfs_to_final_pdf.ipynb```
Merges all page-wise translated PDFs into one final translated PDF.

---

## Environment Setup

Create a virtual environment and install the required packages:
```
cd ~/POP-Translation
python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install google-genai pymupdf markdown-pdf ipykernel
```
Register the environment as a Jupyter kernel:
```
python -m ipykernel install --user \
  --name pop_translation_pipeline \
  --display-name "Python (pop_translation_pipeline)"
```
In Jupyter, switch the notebook kernel to:
```
Python (pop_translation_pipeline)
```

---

## Required Dependencies

This project uses:

1. google-genai
2. pymupdf
3. markdown-pdf
4. ipykernel

---

## Input Requirements

Place the source POP document PDF locally before running the notebooks.
Example source file:
```
input/Kannada POP.pdf
```
The notebooks are designed so that the source PDF path, page range, job name, and model configuration can be changed from the config cell.

---

## API Key Setup

Set your Gemini API key before running the translation notebook.
Recommended method: environment variable
```
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
```
The translation notebook reads the key from the environment.

---

## Output Structure

A typical local working structure looks like this:

```
workdir/<job_name>/
├── pages_pdf/
│   ├── page_16/
│   │   ├── page_16.pdf
│   │   ├── translated_en.md
│   │   └── translated_en.pdf
│   ├── page_17/
│   │   ├── page_17.pdf
│   │   ├── translated_en.md
│   │   └── translated_en.pdf
│   └── ...
└── final_output/
    └── <job_name>_translated_pages_<start>_to_<end>.pdf
```
