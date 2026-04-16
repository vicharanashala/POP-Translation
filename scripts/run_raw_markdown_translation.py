import argparse
import json
from pathlib import Path
from gemma_client import gemma_translate_markdown


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_root", required=True, help="Root folder containing page_* folders")
    parser.add_argument("--output_root", required=True, help="Folder to store raw Gemma outputs")
    parser.add_argument("--prompt_file", required=True, help="Prompt text file")
    parser.add_argument("--max_tokens", type=int, default=8192)
    args = parser.parse_args()

    input_root = Path(args.input_root)
    output_root = Path(args.output_root)
    prompt_file = Path(args.prompt_file)

    output_root.mkdir(parents=True, exist_ok=True)
    run_log = output_root / "run_summary.json"

    prompt_text = prompt_file.read_text(encoding="utf-8")
    summary = []

    page_dirs = sorted([p for p in input_root.iterdir() if p.is_dir() and p.name.startswith("page_")])

    for page_dir in page_dirs:
        page_name = page_dir.name
        source_md = page_dir / "source_kn.md"

        if not source_md.exists():
            summary.append({
                "page": page_name,
                "status": "skipped",
                "reason": "source_kn.md not found"
            })
            continue

        markdown_text = source_md.read_text(encoding="utf-8")
        out_md = output_root / f"{page_name}_raw_gemma_en.md"
        out_txt = output_root / f"{page_name}_raw_gemma_error.txt"

        print(f"Processing {page_name} ...")

        try:
            translated = gemma_translate_markdown(
                prompt_text=prompt_text,
                markdown_text=markdown_text,
                max_tokens=args.max_tokens
            )
            out_md.write_text(translated, encoding="utf-8")

            summary.append({
                "page": page_name,
                "status": "success",
                "output_file": str(out_md)
            })
            print(f"Saved: {out_md}")

        except Exception as e:
            err_msg = str(e)
            out_txt.write_text(err_msg, encoding="utf-8")
            summary.append({
                "page": page_name,
                "status": "failed",
                "error_file": str(out_txt),
                "error": err_msg
            })
            print(f"Failed: {page_name} -> {err_msg}")

    run_log.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRun summary saved to: {run_log}")


if __name__ == "__main__":
    main()
