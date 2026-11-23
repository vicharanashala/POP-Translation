Source Mapper: every method tried so far (kept so no result is lost)

Written 2026-11-21. Sources: INDEX.md, NOT_YET_TRIED.md, the work log of this project, scratch scripts in
research/. "Agreement" means two methods return the same thing; it is NOT accuracy. There is still no labelled
question set (the production login cannot read any questions collection), so almost every figure below is a proxy.

CORPUS
- Start: 306 English-text Punjab PDFs (533 unique documents in the sheet, 28,155 chunks).
- Now: 2,378 documents indexed for keyword modes (303 state, 2,075 central), 74,124 pages. Central scope
  documents in prod: 2,983; 764 of the Hindi/Marathi-catalogued ones are scanned (no text, need OCR), 29 mixed.
- Page text kept in data/pagetext/<doc_id>.json.gz (46 MB) so PDFs can be deleted.

A. EMBEDDING MODES (Atlas M0, BAAI/bge-small-en-v1.5, 384 dims, 28,155 chunks; state documents only)
1. full: top-500 chunks by vector search. Works; no labels, so only agreement with other modes is known.
2. hybrid: keyword pages +/-2 radius, then embedding search inside it. Radius covers 27-65% of chunks (25-55% after
   suffix-only matching); top-1 equals full search 12/12; no time saved (~0.4 s).
3. restrict: a document must hold at least one form of every keyword group. 12 documents passed on the example query.
4. Vector storage (112 queries; data 166 MB -> 72 MB as float32 blobs):
     index              recall@10  recall@500  doc top-1  same top-1 doc as today
     today (doubles)    0.983      0.999       0.875      -
     blob, no quantize  0.979      1.000       0.902      0.902
     scalar             0.948      0.962       0.848      0.786
     binary             0.981      0.889       0.902      0.893
   Not tested: 1M chunks, real questions, RAM size.
5. Central documents cannot be seen by these modes (no embeddings, nothing written to Atlas): "central only" returns 0-1.

B. KEYWORD INDEX (keywords.py)
- BM25 K1=1.2, B=0.75, idf over windows of pages, tokens = lowercase words of 3+ letters, English stop words out,
  a vowel required, NO stemming; query words matched by suffix forms only (fuzzy_match; "foot" no longer matches
  "football", "cotton" no longer matches "otton").
- Threshold: page-level BM25 >= 1.0 (provisional). Window: 10 pages (original); 5 pages in the newer index.
- data/keyword_importance_w5.csv (5-page windows, keywords seen once removed): 127,304 keywords; median 4 important
  pages, 90th percentile 57. 10 vs 5 pages: 6.37M vs 7.02M important term/page pairs (+10.2%). A threshold of 1
  removes almost nothing: the generic words "days", "state", "use" are important on 7,045 / 7,582 / 1,144 pages;
  only "crop" (66,019 occurrences, 9,581 of 14,825 windows) scores 0.
- Raw matrix (all scores, terms seen twice+): 8.93M term/page pairs versus 8.05M at >= 1.0, so the threshold drops
  only ~10% of the pairs.

C. LEXICAL ONLY (lexical.py)
- Top-1 equals the embedding search on 2 of 12 queries; the page found is within 1 page of the embedding result
  for 10 of 12.

D. CROP-ZONE MODE (zonesearch.py, headings.py, cropmaster.py; no embeddings)
- Zones: a zone starts at a heading naming the crop and ends at the next crop chapter; score-only zones otherwise.
  Kharif POP labelled chapters: heading zones cover 68/68 pages, score-only zones 44/68 (tuned by eye on one guide).
  Per chapter: sugarcane 17/19, maize 12/13, groundnut 4/5, cotton 11/17, paddy 0/14 (before heading starts).
- Group rule: 75% of the keyword groups or 4, whichever is fewer (need_cap). Changes only queries with 6+ groups
  (7 groups: need 6 -> 4, 7 -> 18 documents); top-1 unchanged on 7 queries.
- No-zone fallback (crop word required, outside every crop zone): 3,000 random cases against brute force, 0
  mismatches. Making the crop required removed 5 of 19, 7 of 28, 4 of 25 results lacking the crop word.
  Fallback counts: pink bollworm 6, yellow rust 7, seed rate for maize 14, pod borer in chickpea 0.
- No-crop query ("How to grow mango?"): the crop master has no plain "Mango", so a keyword-only search over all pages
  runs; the Fruit Crops POP is rank 1 but the page shown was the intro, not the mango chapter.
- Barley "black powdery material" failure: the Rabi POP says "dark dusty spore mass"; only 3 of 6 groups matched
  (< 75%). Fix: relaxed best-effort pass (up to 10 documents, amber banner); Rabi POP became rank 1 (pp33-34).
- Known answers: Kharif POP pink bollworm p65 at first, p52 later (expected pages 52-68); rank 5/18 -> 5/19 -> 13/54 as
  the corpus grew from 306 to 2,378 documents (not re-tuned). Foot rot basmati p29: rank 1 of 19 -> 25 -> 60.
- Agreement with the embedding search: top-1 4/11, later 2/11, mean top-3 overlap 1.1/3.
- Multi-crop measurement over 306 documents: 160 have no crop; of 76 long documents with a crop, only 5 had a
  single crop family at the top, 32 had 6 or more; median 49% of crop-bearing pages co-led by an unrelated crop (75th
  pct 56%). Noise from crop-master words ("Char Magaz", "Date", "Others"). The relative-peak plan was proposed, not built.
- Heading levels (new): each document's headings counted by size level (font size / body size); the level naming the
  most different crops (>= 1.2, >= 3 crops) is the crop level; zones end at the next heading of that level or higher.
  Kerala AU POP 2016 (401 pp, level 1.3): Cardamom 126-267 -> 126-132, Cinnamon 133-134, Clove 135-136, Vanilla
  137-138, Ginger 139-142, Coffee 176-183, Cowpea 53-56. All documents: 9,142 crop/document pairs with a zone, 2,362
  changed (297 documents), median zone 3 -> 2 pages, mean 7.7 -> 5.5; zones reaching the last page 1,867 -> 1,739.
  Only 399 of 2,378 documents have a crop level; the other 1,979 keep the fixed 1.4 rule. Indian Farming Nov 2025
  chose level 1.7 (4 crops) and missed the 3.4 article titles; Wheat 42-62 -> 42-45, unchecked.
- Scope selector (all / state / central): zone results for pink bollworm 53 / 18 / 35, yellow rust 67 / 20 / 60,
  how to grow mango 50 / 31 / 50.
- Filler-word research (no code): IDF and page frequency do not separate fillers ("comes" IDF 3.59 is rarer than
  "black" 2.87); IDF-weighted coverage only passed the barley query at 45%. A curated list (come, material, thing)
  plus dropping IDF < 1.0 words when 3+ remain was suggested; NOT implemented, not validated.

E. SYNTHETIC BENCHMARK (research/make_bench.py, bench.json, evalbench.py, eval_rerank.py)
- 607 questions from headings ("<sub-heading> <crop>"), 200 scored; only the source document counts as right, so
  scores are lower bounds. Not real farmer questions.
     method                    answer page@1  @10   source doc found
     zone (then)                    0.18      0.50   0.85
     bm25 plain                     0.09      0.29   0.78
     bm25 + coverage                0.17      0.47   0.86
     zone + soft, rank-fused        0.18      0.52   0.94
  100-question run, rerank of top 20 pages: fused only 0.16 / 0.54; + bge-small dense rerank 0.25 / 0.56 (4.4 s per
  query); dense alone 0.24 / 0.57 (0.9 s); + MiniLM cross-encoder 0.24 / 0.55 (2.6 s). The last cross-encoder
  evaluation was stopped unfinished. The right document is missing in 6-22% of questions; reranking cannot fix that.

F. KEYWORD PROXIMITY MODE (proximity.py, mode=proximity; no zones, no embeddings) - built in versions
 v1 binary 5-page-window BM25 matrix; query words exact (no stemming); 75% of words, else min(4, that); closest
    stretch of pages wins. Per-document Python loop: 2.4-4.6 s per query; results identical to the vectorised
    version on 76 queries and to brute force on 6,000 random cases.
 v2 each word = a GROUP of its suffix forms (control, controlled, controlling; variety, varieties); a word ending in s
    also gets the word without its s ("ears" -> ear). Expanding the stripped word too was tried and dropped: it
    added "early".
 v3 vectorised: stretch length L grows 0,1,2..; a keyword is in window [s,s+L] if it was in [s,s+L-1] or is on page
    s+L; summed rows count groups per window. Snippets were 80-90% of the time; now 3 pages per document.
 v4 crop prefilter (the crop's group must be on a page of the document); best-available fallback (75%, then 4, then
    the most groups any document has); seeds (windows up to L*+2); clusters grown outwards while a page holds >= 1
    group, up to 3 per document; content toggle ("read page text", off by default). 12 queries: 0.05-0.3 s off,
    0.8-2.2 s on. Range lengths over 600 documents: median 5.5 pages, 90th pct 24, max 535 (stopping only at 0 does
    not stop for common words); core median 1.
 v5 numbers are index terms (proximity.tokenize); the crop is ONE group of all crop-master aliases (all words of an
    alias on one page). Index 242,944 -> 348,704 terms, 6.9M -> 8.05M pairs. Paddy: 772 documents with the word "rice"
    alone, 1,231 with aliases; barley 258 -> 282. Noisy aliases (batta, vat, chal, khul, patti, gom, jab) not cleaned.
 v6 ranking: smallest stretch, then larger SUM of the groups' best BM25 in the window, then more groups (count only
    gates). Over 6 queries top-10 positions changed on 3 (wheat nitrogen 120 kg: 1 of 10 the same).
 v7 (this change) word forms: -ish both ways (yellowish <-> yellow), -our- <-> -or- (colour <-> color), kept only
    when in the index.
 Query used in tests: "Crop growth is reduced, plants remain stunted, and the barley crop shows pale yellowish
    color from a distance. What should I do" (expected pages 31-34 of the PAU Rabi POP 2025-26, doc
    6a9da0696e28d244c1ad4ced). 12 groups, bar 9. Page 33 says "Zinc deficiency symptoms are stunted and bushy
    crop"; pages 31-34 say "yellow" and "colour", not "yellowish"/"color".
   v6 result: window pp37-42 (barley score 0 there), rank 27 of 108.
   v7 result: window pp28-31, range 23-62, rank 30 of 50 shown (core touches p31). Crop is still not required inside the
    window. Experiment (scratch): crop required in the window gave pp34-40 before the form fix; with the fix and
    filler groups dropped, pp28-31 and pp33-38.

G. RAW BM25 PAGE SUM (mode=bm25sum; this change)
- Page score = sum over groups of the group's best raw BM25 on that page; no threshold, no 75% bar, no window, no
  crop filter; a document ranks by its best page.
- Same question: target document rank 123 of 2,136; its best pages 91 (14.9), 29 (12.3), 33 (12.0). Top results are
  pages of other crops with 8-10 groups (ginger p41 22.5, avocado p15 21.6, minor disorders p39 21.8), none with
  barley score; the crop is one of 12 groups.
- Scratch variants, target rank and best page (same raw sums): no crop rule 123/2136 p91; crop on some page of the
  document 39/282 p91; crop on the page itself 24/282 p33; crop within +-2 pages 27/328 p29; crop group counted 3x
  55/2136 p32.

H. WHAT FAILED OR STAYS OPEN
- A vocabulary mismatch ("black powdery" vs "dark dusty spore mass") cannot be fixed by thresholds.
- Index and contents pages tie with real answers because proximity is counted in whole pages.
- Question filler words ("shows", "distance", "remain", "crop") count as groups and lower the 75% reach.
- Rank of the pink bollworm known answer fell 5 -> 13 as the corpus grew; not re-tuned.
- Nothing here is scored against real labelled questions.

FILES: INDEX.md (running log), NOT_YET_TRIED.md (ideas not built), research/ (scratch benchmark),
data/keyword_importance_w5.csv, data/keyword_importance_w10_compare.csv, data/prox_matrix.npz, data/prox_raw.npz.

I. BM25 PAGE + 2 NEIGHBOURS EACH SIDE (mode=bm25near; scores <= 1.0 left out)
- Page score = sum over groups of the group's best BM25 (matrix without scores <= 1.0) on the page and the 2 pages
  either side in the same document, i.e. the page sums added over p-2..p+2 (a word on a neighbouring page counts
  as many times as it appears in the neighbours). No 75% bar, no window, no crop filter. A document ranks by its
  best page; the shown pages are at least 3 pages apart.
- Barley test question (target = PAU Rabi POP 2025-26, expected pages 31-34): rank 72 of 1,984 (raw own-page sum:
  123 of 2,136), best page 31, range 29-33. Target page scores (this page / sum over +-2): p29 12.3/37.3,
  p30 5.0/35.5, p31 6.6/43.1, p32 7.8/40.4, p33 11.3/42.6, p34 9.7/38.0, p35 7.2/35.7.
- Top results are still other crops (strawberry p23 78.9, horticulture guide p337 72.5, minor disorders p33 67.3):
  symptom words dominate and the crop is one of 12 groups.
- Scratch variants (same sums): crop (any alias) anywhere in the same +-2 window: target rank 8 of 282, best page
  31; crop on the centre page: rank 6 of 282, best page 31; top results then Barley disease and their management
  p9, Technological option p198, Wheat-Breeding-Approaches p115. NOT built into the mode.
- One question only; no labelled evaluation.

J. BM25 NEIGHBOUR SUM WITH A CROP-DISTANCE FACTOR (mode=bm25near, now includes it; crop_radius=0 switches it off)
- Page score (the +-2 sum, scores <= 1.0 out) x 1.3 if the query's crop (any alias) is on a page within +-5 of it in
  the same document, x 0.7 if not. Queries with no crop-master crop are unchanged. Constants CROP_RADIUS 5, CROP_UP
  0.3, CROP_DOWN 0.3 in proximity.py; chosen by the user, not tuned.
- Barley test question: target document rank 72 -> 8 of 1,984; best page 31 (score 43.1 -> 56.0); its shown pages
  31, 34, 28. Top results after: Barley disease and their management p9 (83.9), Technological option p198 (71.0),
  Wheat-Breeding-Approaches p115 (61.8), Status Paper on Coarse Cereals p198 (59.0).
- Other queries (before -> after): pink bollworm in cotton, foot rot in basmati rice, yellow rust in wheat: the top-4
  documents and pages are the same, scores x1.3 (they already had the crop near); "how to grow mango" has no
  crop-master crop, unchanged; "Black powdery material ... barley": Rabi POP p33 stays 3rd (51.0 -> 66.3).
- Cost: ~0.4-0.7 s per query without page text (before and after together 0.7-1.5 s). One labelled-style example
  only; not scored.

K. BM25 BEST-OF-NEIGHBOURS (mode=bm25nearmax)
- For every group the score is the MAX of its row over the page and the 2 pages either side (same document), so a
  group present on several of the pages counts once; then the groups are summed. Same matrix as I (scores <= 1.0
  out), same crop factor with a smaller penalty: x1.3 if the crop (any alias) is within +-5 pages, x0.85 if not
  (CROP_DOWN_MAX 0.15; the 1.3 boost was kept, the user only asked to lower the penalty).
- Checked against a direct calculation on 5 results: 0 mismatches. 0.3-0.6 s per query for this and the sum mode together.
- Barley test question: target rank 19 of 1,984 (sum mode J: 8), best page 29, pages 29, 35, 52, score 29.8.
- Known answers, Kharif POP (sum mode J / max mode K): pink bollworm rank 11 / 9 of 1,325, pages in the expected
  52-68 both (63, 60, 66 / 64, 61, 67); foot rot basmati rank 1 / 6 of 1,593, pages in the expected 27-32 both
  (30, 27, 33 / 27, 30, 34).
- The top-10 (document and page) of the two modes shares 0 of 10 on 5 of 6 test queries and 1 of 10 on the barley
  question: the max mode ranks very differently. Scores are on a different scale (about 25-35 versus 60-100).
  No labelled evaluation, so neither is known to be better; on the barley example the sum mode is better, on the
  pink bollworm example the max mode ranks the known document 2 places higher.

L. BEST-OF-NEIGHBOURS WITH A CROP BUMP THAT FOLLOWS THE CROP'S SCORE (mode=bm25nearscaled)
- Same as K (per group the best score over p-2..p+2, groups summed, scores <= 1.0 out), but the crop factor is:
  bump % = max over the crop's pages q within +-5 of p of (crop BM25 on q x 10 - |q - p|), floored at 0;
  factor 1 + bump/100; no crop page within +-5: x0.75 (25% cut). Constants CROP_PER_SCORE 10, CROP_DOWN_SCALED 0.25.
  Reading of the instruction ("bm25 score * 10 percent - pages from centre"): 10 percent per BM25 point, minus 1
  percentage point per page of distance. Built on K (the max mode), not J.
- Checked against a direct calculation: 0 mismatches.
- Barley question: target rank 19 (K) -> 7 of 1,984, pages 29, 35, 32 (32 is in the expected 31-34; best page 29), bump 52.6%.
  Pink bollworm, Kharif POP: rank 9 -> 11 of 1,325, pages 64, 68, 61 (expected 52-68). Foot rot basmati, Kharif POP:
  rank 6 -> 10 of 1,593, pages 28, 25, 31 (expected 27-32 hit). Bumps run 50-65% on these, so scores rise to 35-44.
- Three queries, no labelled set; not shown to be better overall.

L2. CORRECTION TO L: the multiplier was dropped. Bump % = crop BM25 score - distance in pages (CROP_PER_SCORE 1.0; was 10).
- Barley question: target rank 19 of 1,984 (same as K), pages 29, 35, 32, bump 3.4%. Pink bollworm, Kharif POP: rank 9
  (same as K), pages 64, 61, 68. Foot rot basmati, Kharif POP: rank 6 (same as K), pages 28, 31, 25. Bumps are now
  0.5-9%, so with the 25% cut for a missing crop the mode behaves like K with a larger penalty and a small bump; the
  x10 version (L) gave 7 / 11 / 10 on the same three questions.

M. PAGE'S OWN SCORE IN FULL, NEIGHBOURS AT HALF (mode=bm25nearown)
- Motivated by the wheat question (PAU Rabi POP 2025-26, correct page 29): page 29 is the only page of the document with
  all six groups and 2nd on its own sum (10.4), but the +-2 best-of mode (K/L2) ranked its window 26th in the document,
  because a window collects each word's single highest score from different pages.
- Per group: best of (its score on the page, 0.5 x its best score on the 2 pages either side); groups summed; scores
  <= 1.0 out; crop factor as bm25nearscaled (bump % = crop BM25 - pages from centre, x0.75 with no crop within +-5).
  NEIGHBOUR_WEIGHT 0.5 chosen by me (not tuned). Checked against a direct calculation: 0 mismatches.
- Wheat question: target document rank 27 -> 23 of 1,810; pages shown 119, 48, 29 (29 is the third, scores 12.3 for
  the top one); page 29 is shown but not first. Barley question: rank 19 -> 17, pages 29, 34, 171 (31-34 hit via 34, first
  page not). Pink bollworm (Kharif POP): rank 9 -> 13 of 1,325, first page 68 (in 52-68). Foot rot (Kharif POP): rank 6 -> 6,
  first page 29.
- Not shown to beat the earlier modes; 4 queries, no labelled set. Document ranking is still behind other wheat/barley
  documents.

N. GROUP COVERAGE FACTOR (mode=bm25nearcov)
- bm25nearown (M) page score x coverage, coverage = (groups with a score on the page itself + 0.5 x groups found only on
  the +-2 neighbours) / all groups. A group on the page is not counted again for its neighbours. User's formula; checked
  against a direct calculation (0 mismatches). Crop factor as in L2/M.
- Wheat (correct page 29): doc rank 23 -> 7 of 1,810; pages 119, 29, 48 (29 now second; 119 still first, scores 11.3).
  Barley: 17 -> 22 of 1,984, pages 29, 35, 91 (none of 31-34 shown). Pink bollworm (Kharif POP): 13 -> 12, first page 58.
  Foot rot (Kharif POP): 6 -> 4, first page 29. Four queries, no labelled set.

O. LABELLED TEST (Test with full data, /report)
- 100 closed Punjab questions (two exports, de-duplicated by Question ID), 211 labelled sources, 154 resolve to an indexed
  document (57 not indexed), 80 questions have at least one resolvable source and are scored. A question counts as
  answered if ANY of its sources is found (best rank, smallest page distance over its sources). Page metrics use the
  top 5 returned pages of the document against the labelled pages; a document not returned is a miss in the rates and
  is left out of means/medians. 35 sources have no labelled pages (excluded from page metrics).
- 100% run, seed 1, scope all, 80 scored: top-1 / top-10 / top-50 %, median rank, mean page distance, seconds.
  full 7.5 / 43.8 / 67.5 / 8 / 6.27 / 39.8; hybrid 7.5 / 43.8 / 65.0 / 7 / 6.29 / 25.8; restrict 5.0 / 38.8 / 41.2 / 4 / 6.11 / 14.1;
  zone 1.2 / 31.2 / 47.5 / 8 / 2.5 / 112; proximity 5.0 / 25.0 / 56.2 / 15 / 12.7 / 19; bm25sum 3.8 / 31.2 / 61.2 / 29 / 1.37 / 28;
  bm25near 5.0 / 30.0 / 67.5 / 18 / 0.8 / 24; bm25nearmax 3.8 / 36.2 / 63.8 / 22 / 1.4 / 25; bm25nearscaled 3.8 / 37.5 / 62.5 / 24 / 1.45 / 25;
  bm25nearown 2.5 / 33.8 / 63.8 / 24 / 0.8 / 25; bm25nearcov 5.0 / 36.2 / 72.5 / 17 / 0.72 / 25.
  The mean rank is dominated by a few far-down documents, use the median. Reports: data/test_runs/.
- Small set (80 scored questions, many general or multi-crop), so differences of a few points are noise.

O2. LABELLED TEST, second version (eligibility, state vs central, mode registry)
- Mode numbers (order of invention, `modes.py`): 1 full, 2 hybrid, 3 restrict, 4 zone, 5 proximity, 6 bm25sum, 7 bm25near, 8 bm25nearmax, 9 bm25nearscaled, 10 bm25nearown, 11 bm25nearcov.
- Eligibility: a pair is scored only if its document is in the keyword index, has English text (text check verdict, not the catalogued language), is inside the run scope, and the mode can search it. Excluded pairs are listed with the reason and are in no metric. Catalogued-Hindi/Punjabi PDFs whose text is English ARE scored (they are English text).
- Scope: sources are classified state / central from `common.central_ids()`; metrics are split by the scope of the labelled document (question = state, central or both). A source outside the run's scope is excluded ("outside chosen scope"), not counted as a miss; embedding modes (1-3) cannot see central documents, so those sources are excluded for them ("mode searches state documents only"). Overall numbers of modes 1-3 are therefore over fewer questions than modes 4-11; compare the state column.
- Results of the full re-run (100%, seed 1, scope all) are in the table of the final report below.
- Re-run, 100%, seed 1, scope all (top-1 / 5 / 10 / 50 %, median rank, mean page distance; scored questions; state-only / central-only / both questions with top-10 %):
  #1 full 58: 10.3/44.8/60.3/93.1, 8, 6.27; state 36 (63.9), central 0, both 22 (54.5)
  #2 hybrid 58: 10.3/44.8/60.3/89.7, 7, 6.29; same split (63.9, -, 54.5)
  #3 restrict 58: 6.9/41.4/53.4/56.9, 4, 6.11; 36 (50.0), 0, 22 (59.1)
  #4 zone 74: 1.4/20.3/33.8/51.4, 8, 2.5; 36 (33.3), 16 (18.8), 22 (45.5)
  #5 proximity 74: 5.4/20.3/27.0/60.8, 15, 12.72; 36 (30.6), 16 (12.5), 22 (31.8)
  #6 bm25sum 74: 4.1/23.0/33.8/66.2, 29, 1.37; 36 (27.8), 16 (25.0), 22 (50.0)
  #7 bm25near 74: 5.4/23.0/32.4/73.0, 18, 0.80; 36 (27.8), 16 (25.0), 22 (45.5)
  #8 bm25nearmax 74: 4.1/18.9/39.2/68.9, 22, 1.40; 36 (36.1), 16 (25.0), 22 (54.5)
  #9 bm25nearscaled 74: 4.1/23.0/40.5/67.6, 24, 1.45; 36 (36.1), 16 (31.2), 22 (54.5)
  #10 bm25nearown 74: 2.7/23.0/36.5/68.9, 24, 0.80; 36 (30.6), 16 (25.0), 22 (54.5)
  #11 bm25nearcov 74: 5.4/21.6/39.2/78.4, 17, 0.72; 36 (36.1), 16 (25.0), 22 (54.5)
  Pairs: 211 in all; modes 4-11 score 131 (76 state, 55 central) and exclude 80 (58 not in index, 22 no extractable text, 0 not English); modes 1-3 also exclude the 55 central pairs (135). Small set: differences of a few points are noise.
