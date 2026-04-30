# POP Translation Pipeline

A notebook-based pipeline for translating agricultural POP (Package of Practices) document pages directly from PDF into English while preserving structure as closely as possible.

This project uses a **PDF-page translation workflow** with Gemini. In testing, this method produced stronger overall results and better structure preservation, especially when paired with the **Gemini Pro** model for difficult pages containing complex tables.

---

## Overview

The pipeline is designed to process a source POP document in a controlled, page-range-based workflow.

### Final workflow

1. Split the source POP document into **page-wise PDF files**
2. Send each page PDF directly to **Gemini** for English translation to get output in HTML file
3. Extract images from the original PDF and then inject them back into the **Translated HTML File**
4. Convert translated HTML files into **page-wise PDF files**
5. Merge all translated page PDFs into one **final translated PDF**

---

## Repository Structure

```text
POP-Translation/
├── notebooks/
│   ├── 01_document_to_pagewise_pdf.ipynb
│   ├── 02_page_pdf_to_translated_html.ipynb
│   ├── 03_extract_and_inject_images.ipynb
│   ├── 04_final_html_to_page_pdf.ipynb
│   └── 05_merge_page_pdfs.ipynb
├── prompts/
│   └── page_to_pdf.txt
├── .gitignore
└── README.md
```

---

## Notebook Pipeline

1. ```01_document_to_pagewise_pdf.ipynb```
Splits the source POP document into page-wise PDF files for a selected page range and document.

2. ```02_page_pdf_to_translated_html.ipynb```
Reads each page-wise PDF and sends it directly to Gemini for English translation while attempting to preserve structure by converting it into an HTML file.

3. ```03_extract_and_inject_images.ipynb```
Extracts the images from the original page-wise PDF and injects them back into the translated HTML File.

4. ```04_final_html_to_page_pdf.ipynb```
Converts the HTML files into PDF page-wise files.

5. ```05_merge_page_pdfs.ipynb```
Merges all the page-wise PDF files into one final translated PDF file.

---

## Environment Setup

Create a virtual environment and install the required packages:
```
cd ~/POP-Translation
python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install google-genai pymupdf beautifulsoup4 weasyprint ipykernel
```
Register the environment as a Jupyter kernel:
```
python -m ipykernel install --user \
  --name  gemini_html_output \
  --display-name "Python (gemini_html_output)"
```
In Jupyter, switch the notebook kernel to:
```
Python (gemini_html_output)
```

---

## Required Dependencies

This project uses:

1. google-genai
2. pymupdf
3. beautifulsoup4
4. weasyprint
5. ipykernel

---

## Input Requirements

Place the source POP document PDF locally before running the notebooks.
Example source file:
```
Hindi/POP.pdf
```
The notebooks are designed so that the source PDF path, page range and model configuration can be changed from the config cell.

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
Workdir/
└── Hindi/
    └── source/
        ├── page_001/
        │   ├── page_001.pdf
        │   ├── translated.html
        │   ├── images/
        │   │   ├── image_1.png
        │   │   └── image_2.png
        │   ├── final_with_images.html
        │   ├── final_with_images_preview.html
        │   └── final_page.pdf
        ├── page_002/
        │   ├── page_002.pdf
        │   ├── translated.html
        │   ├── images/
        │   ├── final_with_images.html
        │   ├── final_with_images_preview.html
        │   └── final_page.pdf
        ├── page_003/
        │   └── ...
        └── final_output/
             └── source_translated_pages_001_to_227.pdf
```
