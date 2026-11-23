"""Crop-zone search against the embedding search on queries that name a crop.

    python anara/compare_zone.py

Agreement with the embedding search is not accuracy; only KNOWN holds ground truth: pages in the
Kharif POP (documents whose name starts with the given prefix) that really answer the question.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import zonesearch as zs  # noqa: E402
from search import search  # noqa: E402

QUERIES = [
    "How do I control pink bollworm in cotton?",
    "which wheat varieties are recommended for late sowing",
    "foot rot disease in basmati rice",
    "what is the seed rate for maize",
    "how to manage fall armyworm in maize",
    "recommended dose of nitrogen fertilizer for paddy",
    "zinc deficiency in rice",
    "weed control in wheat herbicide",
    "pesticide registration for sugarcane borer",
    "yellow rust in wheat",
    "pod borer in chickpea",
    "sowing time of mustard",
]
KNOWN = [  # (query, document-name prefix, pages that answer it)
    ("pink bollworm in cotton", "Punjab Kharif Crop POP", range(52, 69)),   # p52 names it too
    ("foot rot disease in basmati rice", "Punjab Kharif Crop POP", range(27, 33)),
]

if __name__ == "__main__":
    agree1 = overlap = ran = 0
    for q in QUERIES:
        t = time.time()
        z = zs.search_zone(q)
        tz = time.time() - t
        if not z:
            print(f"{q[:48]:48} zone: no result")
            continue
        e = search(q)
        ran += 1
        z3, e3 = [r["doc_id"] for r in z[:3]], [r["doc_id"] for r in e[:3]]
        agree1 += z3[0] == e3[0]
        overlap += len(set(z3) & set(e3))
        i = z[0]["keywords"]
        print(f"{q[:48]:48} crop={i['crop']:<12} groups={len(i['groups'])} docs={len(z):2} top1_same={z3[0] == e3[0]!s:5} "
              f"top3_overlap={len(set(z3) & set(e3))}/3 {tz:.2f}s")
    print(f"\n{ran} queries with results: top-1 same {agree1}/{ran}, mean top-3 overlap {overlap / max(ran, 1):.1f}/3")
    print("\nKnown answers:")
    for q, prefix, want in KNOWN:
        res = zs.search_zone(q)
        for rank, r in enumerate(res, 1):
            if r["name"].startswith(prefix):
                print(f"  {q}: {prefix} rank {rank}/{len(res)}, stretch {r['stretch']}, "
                      f"in expected pages: {r['stretch'][0] in want}")
