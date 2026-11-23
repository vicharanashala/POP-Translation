"""Scratch: general (query-independent) keyword importance signals over the corpus, and a smooth multiplier."""
import sys, json
sys.path.insert(0, "anara")
import numpy as np, scipy.sparse as sp
import proximity as P

S = P._load()
raw = P._raw().tocsr()
V, NP = raw.shape
vocab = json.loads(P.META.read_text())["vocab"]
off, ln = S["offsets"], S["lengths"]
ND = len(off)
doc_of = np.repeat(np.arange(ND), ln)
D = sp.csr_matrix((np.ones(NP, np.float32), (np.arange(NP), doc_of)), shape=(NP, ND))

strong = raw.copy()
strong.data[strong.data <= 1.0] = 0
strong.eliminate_zeros()
B = strong.copy(); B.data[:] = 1
pages_imp = np.asarray(B.sum(axis=1)).ravel()
docs_cnt = (B @ D)                                  # V x ND, number of strong pages per doc
docs_imp = np.diff(docs_cnt.tocsr().indptr)         # docs with >=1 strong page
raw_pages = np.diff(raw.indptr)                     # pages containing the term at all (any score)
mass = np.asarray(strong.sum(axis=1)).ravel()
mean_sc = mass / np.maximum(pages_imp, 1)
max_sc = np.asarray(strong.max(axis=1).todense()).ravel()
# windows of 5 pages (non-overlapping blocks inside each doc is close enough; use csv for the real count)
# burstiness: share of strong pages in the single densest doc, and of total mass in top-3 docs
dc = docs_cnt.tocsr()
top1 = np.zeros(V)
for i in range(V):
    a, b = dc.indptr[i], dc.indptr[i + 1]
    if b > a:
        top1[i] = dc.data[a:b].max() / pages_imp[i]
has = pages_imp > 0
idf_doc = np.log((ND + 1) / (docs_imp + 1))                 # (a) IDF over documents
idf_page = np.log((NP + 1) / (raw_pages + 1))               # (a') IDF over pages, any occurrence
sig = {
    "idf_doc": idf_doc,                                    # rare across documents = high
    "idf_page": idf_page,
    "inv_pages_imp": -np.log(pages_imp + 1),               # (b) fewer important pages = higher
    "mean_bm25": mean_sc,                                  # (d)
    "max_bm25": max_sc,
    "burst": top1,                                         # (e) concentration: share in densest doc
    # (f) combination: rarity across documents, weighted by how strongly it scores where it occurs
    "idf_x_mean": idf_doc * np.log1p(mean_sc),
}
np.savez("/tmp/kw_sig.npz", **sig, pages_imp=pages_imp, docs_imp=docs_imp, raw_pages=raw_pages, mean_sc=mean_sc)
print(f"V={V:,} terms; with >=1 important page (score>1): {int(has.sum()):,}; docs {ND}, pages {NP:,}")
for k in ("pages_imp", "docs_imp", "raw_pages", "mean_sc", "top1"):
    v = {"pages_imp": pages_imp, "docs_imp": docs_imp, "raw_pages": raw_pages, "mean_sc": mean_sc, "top1": top1}[k][has]
    print(f"{k:10} pctl 1/10/25/50/75/90/99:", np.round(np.percentile(v, [1, 10, 25, 50, 75, 90, 99]), 2))
