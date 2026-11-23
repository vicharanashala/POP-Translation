"""Scratch: compare general keyword-importance signals, design the multiplier, test it in a copy of bm25nearown."""
import sys, json
sys.path.insert(0, "anara")
import numpy as np, pandas as pd, scipy.sparse as sp
import proximity as P

S = P._load(); raw = P._raw().tocsr(); strong = P._strong()
V, NP = raw.shape
vocab = json.loads(P.META.read_text())["vocab"]; wn = S["word_no"]
off, ln = S["offsets"], S["lengths"]; ND = len(off)
doc_of = np.repeat(np.arange(ND), ln)
D = sp.csr_matrix((np.ones(NP, np.float32), (np.arange(NP), doc_of)), shape=(NP, ND))

def binar(m):
    b = m.copy(); b.data[:] = 1; return b
Br, Bs = binar(raw), binar(strong)
docs_raw = np.diff((Br @ D).tocsr().indptr)          # documents the term occurs in (any score)
docs_imp = np.diff((Bs @ D).tocsr().indptr)
pages_raw = np.asarray(Br.sum(axis=1)).ravel()
pages_imp = np.asarray(Bs.sum(axis=1)).ravel()
mass = np.asarray(strong.sum(axis=1)).ravel()
mean_imp = mass / np.maximum(pages_imp, 1)
max_imp = np.asarray(strong.max(axis=1).todense()).ravel()
kwcsv = pd.read_csv("anara/data/keyword_importance_w5.csv").drop_duplicates("keyword").set_index("keyword")
_f = kwcsv.total_frequency.to_dict(); _w = kwcsv.windows_found.to_dict()
freq = np.array([_f.get(w, np.nan) for w in vocab]); wins = np.array([_w.get(w, np.nan) for w in vocab])
SIG = {
    "idf_doc": np.log(ND / np.maximum(docs_raw, 1)),            # (a) document frequency, any occurrence
    "idf_page": np.log(NP / np.maximum(pages_raw, 1)),          # (a) page frequency
    "idf_freq": np.log(np.nansum(freq) / freq),                 # (a) corpus frequency (csv, terms seen >=2)
    "idf_docimp": np.log(ND / np.maximum(docs_imp, 1)),         # (b) over documents with an important page
    "idf_pgimp": np.log(NP / np.maximum(pages_imp, 1)),         # (b) important pages
    "idf_win": np.log(NP / 5 / wins),                           # (c) 5-page windows
    "mean_bm25": mean_imp,                                      # (d)
    "max_bm25": max_imp,
    "idf_doc*mean": np.log(ND / np.maximum(docs_raw, 1)) * np.log1p(mean_imp),   # (f)
    "idf_doc+mean": np.log(ND / np.maximum(docs_raw, 1)) + 0.5 * mean_imp,
}
# population: terms with >=1 important page; words of >= 3 letters, alphabetic, seen on >=3 pages (kills OCR junk / typos)
pop_all = pages_imp > 0
alpha = np.array([w.isalpha() and len(w) >= 3 for w in vocab])
pop_real = pop_all & alpha & (pages_raw >= 3)
GEN = "plants plant crop leaves leaf shows distance remain control seed table water soil disease growth reduced color field application time used high rate number results effect management production area using based different important study state year".split()
DIS = ("bollworm rust zinc stunted basmati smut blast whitefly aphid jassid thrips nematode mealybug borer blight mildew chlorosis "
       "glyphosate imidacloprid tillering panicle hopper wilt mosaic anthracnose weevil midge gall sheath cutworm armyworm "
       "pendimethalin phorate nitrogen sulphur potassium urea").split()
GEN = [w for w in GEN if w in wn]; DIS = [w for w in DIS if w in wn]
gi, di = [wn[w] for w in GEN], [wn[w] for w in DIS]

def auc(s):                       # P(random discriminative term scores above random generic term)
    a, b = s[di][:, None], s[gi][None, :]
    return float(((a > b).sum() + 0.5 * (a == b).sum()) / (a.shape[0] * b.shape[1]))

def rank_of(s, pop):              # rank 1 = most important, within `pop`
    idx = np.nonzero(pop)[0]; order = idx[np.argsort(-s[idx], kind="stable")]
    r = np.zeros(V, int); r[order] = np.arange(1, len(order) + 1); return r

if __name__ == "__main__":
    print(f"terms in matrix {V:,}; >=1 important page {int(pop_all.sum()):,}; real-word population (alpha, >=3 letters, >=3 pages) {int(pop_real.sum()):,}")
    print(f"generic list {len(GEN)}, discriminative list {len(DIS)}")
    print(f"\n{'signal':14}{'AUC':>6}{'median generic rank':>22}{'median discr. rank':>20}   (rank within real-word population; 1 = most important)")
    for k, s in SIG.items():
        s2 = np.nan_to_num(s, nan=-1e9)
        r = rank_of(s2, pop_real)
        print(f"{k:14}{auc(s2):6.3f}{np.median([r[i] for i in gi]):22.0f}{np.median([r[i] for i in di]):20.0f}")
    print("\nsample words, signal idf_doc (docs, pages_raw, mean imp bm25):")
    for w in ["plants","crop","leaves","shows","distance","remain","wheat","brown","spots","yellow","stunted","bollworm","rust","zinc","basmati","smut","foot","rot","barley","cotton"]:
        if w in wn:
            i = wn[w]; print(f"  {w:10} docs {int(docs_raw[i]):5d} pagesRaw {int(pages_raw[i]):6d} idf_doc {SIG['idf_doc'][i]:.2f} mean {mean_imp[i]:.1f} imp pages {pages_imp[i]}")
    s = SIG["idf_doc"]; v = s[pop_real]
    print("\nidf_doc over real-word population percentiles 1/5/25/50/75/95/99:", np.round(np.percentile(v, [1,5,25,50,75,95,99]), 2))
    r = rank_of(s, pop_real)
    print("terms with rank<1000:", int((r[pop_real] < 1000).sum()), "; terms tied at the max idf (1 document):", int((docs_raw[pop_real] == 1).sum()))
