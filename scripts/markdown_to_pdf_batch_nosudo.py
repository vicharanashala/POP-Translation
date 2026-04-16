import argparse
from pathlib import Path
from markdown_pdf import MarkdownPdf, Section

DEFAULT_CSS = """
body {
  font-family: Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.35;
}
table, th, td {
  border: 1px solid black;
  border-collapse: collapse;
}
th, td {
  padding: 4px;
  vertical-align: top;
}
h1, h2, h3, h4, h5, h6 {
  margin-top: 14px;
  margin-bottom: 8px;
}
pre, code {
  white-space: pre-wrap;
}
"""

def convert_one(md_file: Path, pdf_file: Path):
    text = md_file.read_text(encoding="utf-8")
    pdf = MarkdownPdf(toc_level=0, optimize=True)
    pdf.meta["title"] = md_file.stem
    pdf.add_section(Section(text), user_css=DEFAULT_CSS)
    pdf_file.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(pdf_file))

def main():
    parser = argparse.ArgumentParser(description="Convert markdown files to PDF without pandoc/sudo.")
    parser.add_argument("--input_root", required=True, help="Folder containing translated markdown files")
    parser.add_argument("--output_root", required=True, help="Folder where PDFs will be saved")
    parser.add_argument("--pattern", default="*_raw_gemma_en.md", help="Glob pattern for markdown files")
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    md_files = sorted(input_root.glob(args.pattern))
    if not md_files:
        print("No matching markdown files found.")
        return

    for md_file in md_files:
        pdf_file = output_root / f"{md_file.stem}.pdf"
        print(f"Converting: {md_file.name} -> {pdf_file.name}")
        try:
            convert_one(md_file, pdf_file)
        except Exception as e:
            print(f"Failed: {md_file.name} -> {e}")

    print("Done.")

if __name__ == "__main__":
    main()
