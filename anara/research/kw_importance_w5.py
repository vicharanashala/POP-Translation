"""Keyword importance with 5-page BM25 windows (keywords.py method, WINDOW 10 -> 5, threshold 1.0).

Output: data/keyword_importance_w5.csv  (keyword, pages_important, windows_found, total_frequency)
Same rules as keywords.page_scores: windows are consecutive blocks of the whole page sequence (docs in
build_stats order, blocks straddle documents); idf = log(1+(N-df+.5)/(df+.5)) over windows; a page's score is
idf * tf*(K1+1)/(tf + K1*(1-B+B*len/avg)) from the page's own tf; important when >= 1.0.
"""
import resource
import sys
import time
from collections import Counter

import numpy as np
import pandas as pd
import scipy.sparse as sp

sys.path.insert(0, __file__.rsplit("/", 2)[0])
import common  # noqa: E402
import keywords  # noqa: E402
import pagetext  # noqa: E402

W, THR = 5, 1.0
K1, B = keywords.K1, keywords.B
t0 = time.time()

report = pd.read_csv(common.DATA / "text_report.csv")
ids = report.loc[report.verdict == "english_text", "doc_id"].tolist()   # build_stats(zone=True) list

vocab: dict[str, int] = {}
rows, cols, vals = [], [], []
n_pages, skipped, ndocs = 0, [], 0
for n, d in enumerate(ids, 1):
    try:
        pgs = pagetext.pages(d)
    except Exception as e:  # unreadable page text: skip and count
        skipped.append((d, type(e).__name__))
        continue
    ndocs += 1
    for p in pgs:
        c = Counter(keywords.tokenize(p))
        for t, f in c.items():
            rows.append(n_pages)
            cols.append(vocab.setdefault(t, len(vocab)))
            vals.append(f)
        n_pages += 1
    if n % 200 == 0:
        print(n, len(ids), round(time.time() - t0), flush=True)

V = len(vocab)
M = sp.csr_matrix((np.array(vals, np.float32), (np.array(rows, np.int32), np.array(cols, np.int32))),
                  shape=(n_pages, V))
del rows, cols, vals
M.sum_duplicates()
tf_total = np.asarray(M.sum(axis=0)).ravel().astype(np.int64)
plen = np.asarray(M.sum(axis=1)).ravel()
avg = plen.mean()
coo = M.tocoo()
page_of, term_of, tf = coo.row, coo.col, coo.data.astype(np.float64)
norm = K1 * (1 - B + B * plen / avg)


def run(w):
    win = page_of // w
    nw = int(-(-n_pages // w))
    # df over windows: unique (window, term) pairs
    key = np.unique(win.astype(np.int64) * V + term_of)
    df = np.bincount(key % V, minlength=V)
    idf = np.log(1 + (nw - df + 0.5) / (df + 0.5))
    sc = idf[term_of] * tf * (K1 + 1) / (tf + norm[page_of])
    imp = sc >= THR
    pages_imp = np.bincount(term_of[imp], minlength=V)
    return nw, df, pages_imp, int(imp.sum())


nw5, df5, imp5, pairs5 = run(5)
nw10, df10, imp10, pairs10 = run(10)
inv = np.empty(V, object)
for t, i in vocab.items():
    inv[i] = t
df = pd.DataFrame({"keyword": inv, "pages_important": imp5, "windows_found": df5, "total_frequency": tf_total})
df = df.sort_values(["pages_important", "total_frequency"], ascending=False, kind="stable")
out = common.DATA / "keyword_importance_w5.csv"
full = len(df)
if full > 400_000:
    df = df[df.total_frequency >= 2]
df.to_csv(out, index=False)
pd.DataFrame({"keyword": inv, "pages_important_w10": imp10, "windows_w10": df10}).to_csv(
    common.DATA / "keyword_importance_w10_compare.csv", index=False)
print(f"docs {ndocs} skipped {len(skipped)} {skipped[:5]} pages {n_pages} windows5 {nw5} windows10 {nw10} vocab {V} rows_written {len(df)} (full {full})")
print(f"important pairs w5 {pairs5}  w10 {pairs10}; keywords>0 w5 {(imp5>0).sum()} w10 {(imp10>0).sum()}; avg_page_len {avg:.2f}")
print(f"runtime {time.time()-t0:.0f}s peak_rss_MB {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}")
