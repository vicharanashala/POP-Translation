"""Full embedding search vs keyword-radius search on the same queries.

    python anara/compare.py [--radius 2]

Per query: keywords matched, chunks inside the radius (of all chunks), whether the top-1 and
top-3 documents agree with the full search, and the time of each. Agreement is NOT accuracy:
the full search is the reference, not ground truth.
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import hybrid  # noqa: E402
from search import search  # noqa: E402

QUERIES = [
    "How do I control pink bollworm in cotton?",
    "Which varieties of wheat are recommended for late sowing?",
    "subsidy on paddy straw management machines",
    "foot rot disease in basmati",
    "what is the seed rate for maize",
    "how to manage fall armyworm",
    "recommended dose of nitrogen fertilizer for paddy",
    "zinc deficiency in rice",
    "contact number of KVK Bathinda",
    "pesticide registration for sugarcane borer",
    "weed control in wheat herbicide",
    "organic farming certification in Punjab",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=int, default=2)
    a = ap.parse_args()
    hybrid._load()
    total = len(hybrid._state["ids"])
    search("warm up")
    print(f"{total} chunks in total, radius +/-{a.radius}\n")
    agree1 = agree3 = fb = 0
    for q in QUERIES:
        t = time.time(); full = search(q); tf = time.time() - t
        t = time.time(); hy = hybrid.search_hybrid(q, a.radius); th = time.time() - t
        f3 = [r["doc_id"] for r in full[:3]]; h3 = [r["doc_id"] for r in hy[:3]]
        mode = hy[0]["mode"]; rad = hy[0]["radius"]
        fb += mode != "keyword-radius"
        same1 = f3[0] == h3[0]; ov = len(set(f3) & set(h3))
        agree1 += same1; agree3 += ov
        share = f"{rad}/{total} ({rad / total:.0%})" if rad else "n/a"
        print(f"{q[:55]:55} {mode[:8]:8} radius {share:18} top1 {'same' if same1 else 'DIFF'} top3 overlap {ov}/3  "
              f"full {tf:.2f}s hybrid {th:.2f}s")
        if not same1:
            print(f"     full  : {full[0]['name'][:70]}\n     hybrid: {hy[0]['name'][:70]}")
    n = len(QUERIES)
    print(f"\ntop-1 agreement {agree1}/{n}, mean top-3 overlap {agree3 / n:.1f}/3, fallbacks {fb}")


if __name__ == "__main__":
    main()
