Source Mapper: what is not yet tried (written 2026-11-21)

Status. The research helper timed out before writing a report. This file is built from the scratch
work it left in anara/research/ (bench.json, make_bench.py, evalbench.py, eval_rerank.py) plus what
we already knew. No web sources were gathered, so the ideas below come from general knowledge and
are not checked against papers or docs.

1. What was measured (synthetic benchmark, 607 questions built from document headings; 200 run)
Question = "<sub-heading> <crop>", answer = that section's pages. Only the source document counts as
right, so every score is a lower bound. It is not real farmer questions.
  method               answer page@1  answer page@10  source doc found
  zone (today)              0.18          0.50            0.85
  bm25 plain                0.09          0.29            0.78
  bm25 + coverage           0.17          0.47            0.86
  zone + soft, rank fused   0.18          0.52            0.94
100-question run, rerank of the top 20 pages:
  fused only                0.16          0.54            0.78
  + bge-small dense rerank  0.25          0.56            0.78   (4.4 s/query on CPU)
  dense rerank alone        0.24          0.57            0.78   (0.9 s/query)
  + MiniLM cross-encoder    0.24          0.55            0.78   (2.6 s/query; no gain over dense)
Reading: the right page is in the top 10 only about half the time, and the right document is not
found at all in 6 to 22% of questions. A dense rerank of the top 20 helps the top-1 page
(0.16 to 0.25, n=100, so roughly +/-4 points of noise) but cannot help when the document is missing.
Not measured: real questions, Hindi/Marathi, scanned documents, the rerank on the full 200.

2. Not yet tried, within our limits (no new money)
- Rank-fuse zone + soft match for candidate recall (0.85 to 0.94 source found above); then dense rerank
  the top 20. Needs: pagetext store (have it). Effort: 1 to 2 days. Risk: +1 to 4 s per query on CPU.
- Table-of-contents parsing for zone boundaries (documents have a contents page with chapter page
  numbers). Needs: nothing new. Effort: 2 to 3 days. Helps the 1,979 documents with no crop level.
- Demote index, contents and references pages (page-type rule from dot leaders, page-number density).
  Needs: nothing. Effort: 1 day. Fixes ties with real answers.
- Synonym mining from our own text (word co-occurrence) for "black powdery" vs "dark dusty spore mass".
  Needs: nothing. Effort: 2 to 3 days. Uncertain gain; the benchmark above cannot show it.
- Curated filler-word list (come, material, thing) plus dropping very common words. Effort: half a day.
- Local dense vectors for central pages kept in a local file (not Atlas): 74k pages x 384 float32 is
  about 110 MB. Encoding time on this CPU was not measured. Effort: 1 to 2 days.
- Crop-master cleanup (words like Char Magaz, Date, Others) and the relative-peak plan. Half a day.

3. Needs more than we have
- Real questions with the right answer. The production login cannot read any questions collection.
  Needs: read access to it, or someone labelling 50 to 100 questions. This is the biggest gap: every
  figure above is a proxy.
- OCR of the 764 scanned central documents. Needs: Tesseract with Hindi/Marathi data; many CPU hours;
  disk for temporary PDFs. Not estimated.
- Hindi/Marathi questions against English text. Needs: a translation model (GPU or paid API) or a
  multilingual embedding model; not tried.
- Per-QUERY language-model rewriting (not per document). Needs: a decision, since it costs money per
  question, and a budget.
- Learned sparse retrieval (SPLADE-style). Needs a GPU or many CPU hours for 74k pages; not tried.
- More Atlas space for embeddings of central documents. Our M0 is about 290 of 512 MB. The dashboard
  cluster tier and free space are unknown: the login cannot read them. Needs: clusterMonitor role
  or the Atlas console.
