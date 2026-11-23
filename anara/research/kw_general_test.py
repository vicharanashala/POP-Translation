"""Scratch: multiplier mapping table + effect on the bm25nearown logic (scratch copy of the scoring, weights per group)."""
import sys
sys.path.insert(0, "anara"); sys.path.insert(0, "anara/research")
import numpy as np
from kw_general_eval import *          # noqa  (loads matrices and SIG)
import proximity as P

def smooth(x):                          # smoothstep: 0 at 0, 1 at 1, zero slope at both ends
    x = np.clip(x, 0, 1); return x * x * (3 - 2 * x)

# anchors: 5th / 95th percentile of the signal over query-like words (alphabetic, seen on >=50 pages)
qlike = alpha & (pages_raw >= 50) & pop_all
ANCH = {k: np.percentile(np.nan_to_num(s[qlike], nan=0), [5, 95]) for k, s in SIG.items()}
SIG['idf_doc_wide'] = SIG['idf_doc']; ANCH['idf_doc_wide'] = np.array([0.7, 3.2])   # x1.0 at ~1200 docs, x2.0 at ~100 docs
def mult(k, v):
    lo, hi = ANCH[k]; return 1 + smooth((v - lo) / (hi - lo))

def table():
    s = SIG["idf_doc"]; lo, hi = ANCH["idf_doc"]
    print(f"\nquery-like vocabulary: {int(qlike.sum()):,} words; idf_doc anchors lo={lo:.2f} (x1.0) hi={hi:.2f} (x2.0)")
    print("\nSample words: idf_doc -> multiplier (smoothstep of (idf-lo)/(hi-lo)), vs percentile-rank mapping on the 71k real-word population")
    pr = rank_of(s, pop_real); N = int(pop_real.sum())
    print(f"{'word':10}{'docs':>6}{'idf_doc':>8}{'rank':>7}{'mult(value)':>12}{'mult(pctile)':>13}{'mult(logrank)':>14}")
    for w in ["plants","crop","leaves","wheat","brown","yellow","spots","shows","distance","remain","stunted","rust","zinc","foot","barley","basmati","smut","bollworm"]:
        if w in wn:
            i = wn[w]; r = pr[i]
            print(f"{w:10}{int(docs_raw[i]):6d}{s[i]:8.2f}{r:7d}{float(mult('idf_doc', s[i])):12.3f}{(1 + 1 - r / N):13.3f}{(1 + 1 - np.log(r) / np.log(N)):14.3f}")
    print("\nAdjacent-rank differences (importance rank 1 = rarest; real-word population, N=%d)" % N)
    order = np.argsort(pr + (pr == 0) * 10**9)
    print(f"{'ranks':>14}{'idf a':>8}{'idf b':>8}{'d mult(value)':>14}{'d mult(pctile)':>15}{'d mult(logrank)':>16}")
    for a in (1, 100, 981, 10000, 40000, 69000, 71000):
        ia, ib = order[a - 1], order[a]
        f = lambda m: abs(m(ia) - m(ib))
        print(f"{a:6d}/{a+1:<7d}{s[ia]:8.2f}{s[ib]:8.2f}{f(lambda i: mult('idf_doc', s[i])):14.4f}{abs((1-pr[ia]/N)-(1-pr[ib]/N)):15.5f}{abs(np.log(pr[ib])-np.log(pr[ia]))/np.log(N):16.5f}")
    print("\nmultiplier(value) over the idf scale: " + "  ".join(f"{x:.1f}->{float(mult('idf_doc', x)):.2f}" for x in np.arange(0, 6.01, 0.5)))
    print("max |d mult| between words 0.01 idf apart:", round(float(np.max(np.abs(np.diff(mult('idf_doc', np.arange(0, 8, 0.01)))))), 4))

# ---- scratch copy of bm25nearown scoring with a per-group weight ----
def search(q, weights=None, nb=0.5, near=2, crop_r=5, down=0.25):
    tk, groups, snip, rows, ci, sk = P._groups(q, strong)
    words = list(groups)
    if not words: return []
    sc = np.stack([rows[w] for w in words]).astype(np.float64)
    w = np.ones(len(words))
    if weights is not None:
        key, = [weights]
        for k, g in enumerate(words):
            toks = ci["words"] if (ci and k == 0) else [g]
            vals = [SIG[key][wn[t]] for t in toks if t in wn]
            if not vals:
                vals = [SIG[key][wn[f]] for f in groups[g] if f in wn]
            w[k] = float(mult(key, max(vals))) if vals else 1.0
    s0 = sc * w[:, None]
    npg = s0.shape[1]; idx = np.arange(npg); lo_, hi_ = off[doc_of], (off + ln)[doc_of]
    mx = s0.copy()
    for d in range(-near, near + 1):
        if d:
            src = idx + d; valid = (src >= lo_) & (src < hi_)
            mx = np.maximum(mx, nb * np.where(valid, s0[:, np.clip(src, 0, npg - 1)], 0))
    page = mx.sum(axis=0)
    if ci:
        c0 = sc[0]; bump = np.full(npg, -np.inf); near_c = np.zeros(npg, bool)
        for d in range(-crop_r, crop_r + 1):
            src = idx + d; valid = (src >= lo_) & (src < hi_)
            v = np.where(valid, c0[np.clip(src, 0, npg - 1)], 0)
            near_c |= v > 0; bump = np.maximum(bump, np.where(v > 0, v - abs(d), -np.inf))
        page = page * np.where(near_c, 1 + np.maximum(bump, 0) / 100, 1 - down)
    best = np.maximum.reduceat(page, off)
    order = np.argsort(-best, kind="stable")
    res = []
    for di_ in order:
        o, n = int(off[di_]), int(ln[di_]); seg = page[o:o + n]; top = []
        for p in np.argsort(-seg, kind="stable"):
            if seg[p] <= 0 or len(top) == 3: break
            if all(abs(int(p) - t) > near for t in top): top.append(int(p))
        res.append((S["docs"][di_], [t + 1 for t in top], float(best[di_]), w.round(2).tolist()))
        if len(res) >= 3000 or best[di_] <= 0: break
    return res

T = "6a9da0696e28d244c1ad4ced"
def kharif(d): return S["table"].loc[d]["name"].startswith("Punjab Kharif Crop POP")
QS = [("wheat brown spots", "What should I do about brown spots on leaves of wheat crop plants?", lambda d: d == T, range(29, 30)),
      ("barley", "Crop growth is reduced, plants remain stunted, and the barley crop shows pale yellowish color from a distance. What should I do", lambda d: d == T, range(31, 35)),
      ("pink bollworm", "How do I control pink bollworm in cotton?", kharif, range(52, 69)),
      ("foot rot", "foot rot disease in basmati rice", kharif, range(27, 33))]

def test():
    cfgs = [None, "idf_doc", "idf_doc_wide", "idf_win", "mean_bm25"]
    print(f"\n{'query':15}" + "".join(f"{str(c):>22}" for c in cfgs))
    for name, q, isT, pgs in QS:
        line = f"{name:15}"
        for c in cfgs:
            r = search(q, c)
            hit = next(((i + 1, x[1]) for i, x in enumerate(r) if isT(x[0])), None)
            line += f"{('r%d p%s %s' % (hit[0], hit[1][0], 'Y' if hit[1][0] in pgs else ('y' if any(p in pgs for p in hit[1]) else '-'))) if hit else 'none':>22}"
        print(line)
    print("(rK pN: target doc rank, first page shown; Y = first page in expected range, y = another shown page is, - = no)")
    for name, q, isT, pgs in QS[:2]:
        tk, groups, *_ = P._groups(q, strong)
        print(name, "group weights (idf_doc_wide):", dict(zip(groups, search(q, "idf_doc_wide")[0][3])))

if __name__ == "__main__":
    import os
    if not os.environ.get('NOTABLE'): table()
    test()
