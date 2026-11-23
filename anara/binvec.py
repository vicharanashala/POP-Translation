"""Vector storage experiment: float32 blobs + Atlas vector-index quantization.

    python anara/binvec.py migrate     copy doc_embeddings -> doc_embeddings_bin, vectors as 4-byte float32 blobs
    python anara/binvec.py index [names]   vector indexes on the new collection (bin_none, bin_scalar, bin_binary); M0 fits two at a time
    python anara/binvec.py dropindex NAME  drop one of them to make room
    python anara/binvec.py eval        recall of every arm against an EXACT local search (data/chunk_matrix.npz)
    python anara/binvec.py drop        remove the new collection and its indexes

doc_embeddings (the app's collection, list-of-doubles, index chunk_vector_index) is only read, never changed.
Ground truth is exact cosine (a dot product, vectors are normalised) over every chunk, so "recall" means
"how many of the true nearest chunks did the index return".
"""
from __future__ import annotations

import sys
import time

import numpy as np
from bson.binary import Binary, BinaryVectorDtype
from pymongo.operations import SearchIndexModel

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
from db import EMBEDDINGS, get_db  # noqa: E402

NEW = "doc_embeddings_bin"
DIMS = 384
ARMS = {"bin_none": None, "bin_scalar": "scalar", "bin_binary": "binary"}
OLD_INDEX = "chunk_vector_index"
POOL = 500            # the app's pool: limit 500
CANDIDATES = 5000     # the app's numCandidates for k=500


def to_blob(vec) -> Binary:
    return Binary.from_vector(np.asarray(vec, dtype="float32").tolist(), BinaryVectorDtype.FLOAT32)


def migrate(batch: int = 1000) -> None:
    db = get_db()
    if db[NEW].estimated_document_count():
        raise SystemExit(f"{NEW} already has documents; run `drop` first")
    n = 0
    buf = []
    for d in db[EMBEDDINGS].find({}).batch_size(batch):
        d["embedding"] = to_blob(d["embedding"])
        buf.append(d)
        if len(buf) == batch:
            db[NEW].insert_many(buf)
            n += len(buf)
            buf = []
            print(f"  {n:,} chunks", flush=True)
    if buf:
        db[NEW].insert_many(buf)
        n += len(buf)
    for name in (EMBEDDINGS, NEW):
        s = db.command("collstats", name)
        print(f"{name}: {s['count']:,} docs, data {s['size']/1e6:.1f} MB, storage {s['storageSize']/1e6:.1f} MB, "
              f"avg object {s['avgObjSize']} B")
    # the blobs must decode to the very same numbers as the stored lists
    z = np.load(common.DATA / "chunk_matrix.npz")
    ids, mat = list(z["ids"]), z["mat"]
    pos = {i: k for k, i in enumerate(ids)}
    worst = 0.0
    for d in db[NEW].aggregate([{"$sample": {"size": 300}}]):
        v = np.array(d["embedding"].as_vector().data, dtype="float32")
        worst = max(worst, float(np.abs(v - mat[pos[str(d["_id"])]]).max()))
    print(f"max |blob - original| over 300 sampled chunks: {worst:.2e}")


def make_indexes(names: list[str] | None = None) -> None:
    """M0 allows 3 search indexes in total (chunk_vector_index counts), so create at most two of the arms
    at a time: `index bin_none bin_scalar`, then drop one (`dropindex`) before `index bin_binary`."""
    db = get_db()
    have = {i["name"] for i in db[NEW].list_search_indexes()}
    for name in names or list(ARMS):
        quant = ARMS[name]
        if name in have:
            continue
        field = {"type": "vector", "path": "embedding", "numDimensions": DIMS, "similarity": "cosine"}
        if quant:
            field["quantization"] = quant
        db[NEW].create_search_index(SearchIndexModel(name=name, type="vectorSearch", definition={"fields": [field]}))
        print("created index", name, flush=True)
    wait_ready()


def drop_index(name: str) -> None:
    get_db()[NEW].drop_search_index(name)
    time.sleep(10)
    print("dropped index", name)


def wait_ready(timeout: int = 1800) -> None:
    db = get_db()
    t = time.time()
    while time.time() - t < timeout:
        st = {i["name"]: (i.get("status"), i.get("queryable")) for i in db[NEW].list_search_indexes()}
        print(f"  {int(time.time() - t)}s", st, flush=True)
        if st and all(q for _, q in st.values()):
            return
        time.sleep(20)
    raise SystemExit("indexes not ready in time")


def exact_top(mat: np.ndarray, qv: np.ndarray, k: int) -> np.ndarray:
    s = mat @ qv
    top = np.argpartition(-s, k)[:k]
    return top[np.argsort(-s[top])]


def atlas_top(coll: str, index: str, qv: list[float], k: int = POOL) -> tuple[list[str], list[float], float]:
    t = time.time()
    rows = list(get_db()[coll].aggregate([
        {"$vectorSearch": {"index": index, "path": "embedding", "queryVector": qv,
                           "numCandidates": CANDIDATES, "limit": k}},
        {"$project": {"score": {"$meta": "vectorSearchScore"}}}]))
    return [str(r["_id"]) for r in rows], [r["score"] for r in rows], time.time() - t


def doc_rank(ids: list[str], scores: list[float], doc_of: dict, top_n: int = 5) -> list[str]:
    """The app's ranking: a document's score is the mean of its 5 best chunks, padded with the pool's lowest."""
    from collections import defaultdict
    by = defaultdict(list)
    for i, s in zip(ids, scores):
        by[doc_of[i]].append(s)
    floor = min(scores)
    mean = {d: (sum(sorted(v, reverse=True)[:top_n]) + floor * max(0, top_n - len(v))) / top_n for d, v in by.items()}
    return sorted(mean, key=lambda d: -mean[d])


def queries(n_extra: int = 100, seed: int = 7) -> list[str]:
    """The 12 compare.py queries plus `n_extra` made from random chunks: a 14-word run from the MIDDLE of a
    chunk, so it is not an exact-start match. Fixed seed: every run uses the same queries."""
    from bson import ObjectId
    from compare import QUERIES
    rng = np.random.default_rng(seed)
    ids = list(np.load(common.DATA / "chunk_matrix.npz")["ids"])
    pick = [ids[i] for i in rng.permutation(len(ids))[: n_extra * 3]]
    text = {str(r["_id"]): r["text"] for r in get_db()[EMBEDDINGS].find({"_id": {"$in": [ObjectId(i) for i in pick]}}, {"text": 1})}
    out = list(QUERIES)
    for i in pick:
        w = text[i].split()
        if len(w) >= 60:
            out.append(" ".join(w[int(rng.integers(20, len(w) - 34)):][:14]))
        if len(out) >= 12 + n_extra:
            break
    return out


def evaluate() -> None:
    from embedder import embed_query
    z = np.load(common.DATA / "chunk_matrix.npz")
    ids, mat = list(z["ids"]), z["mat"]
    doc_of = dict(zip(ids, z["docs"]))
    qs = queries()
    print(f"{len(qs)} queries, pool {POOL}, numCandidates {CANDIDATES}, {len(ids):,} chunks\n")
    present = {i["name"] for i in get_db()[NEW].list_search_indexes() if i.get("queryable")}
    arms = [("today: list of doubles", EMBEDDINGS, OLD_INDEX)] + [(f"blob float32 / {q or 'no quantization'}", NEW, n)
                                                                   for n, q in ARMS.items() if n in present]
    res = {a[0]: {"r10": [], "r500": [], "top1": [], "top3": [], "t": [], "gap": [], "docs": [], "chunks": []} for a in arms}
    for q in qs:
        qv = np.array(embed_query(q), dtype="float32")
        ex = exact_top(mat, qv, POOL)
        ex_ids = [ids[i] for i in ex]
        ex_scores = (mat[ex] @ qv).tolist()
        ex_docs = doc_rank(ex_ids, ex_scores, doc_of)
        for label, coll, index in arms:
            got, sc, dt = atlas_top(coll, index, qv.tolist())
            r = res[label]
            r["r10"].append(len(set(got[:10]) & set(ex_ids[:10])) / 10)
            r["r500"].append(len(set(got) & set(ex_ids)) / POOL)
            gd = doc_rank(got, sc, doc_of)
            r["top1"].append(gd[0] == ex_docs[0])
            r["top3"].append(len(set(gd[:3]) & set(ex_docs[:3])) / 3)
            r["t"].append(dt)
            r["gap"].append(abs(sc[0] - (1 + ex_scores[0]) / 2))   # Atlas cosine score = (1 + cosine) / 2
            r["docs"].append(gd[:5])
            r["chunks"].append(got[:10])
    print(f"{'arm':36} {'recall@10':>9} {'recall@500':>10} {'doc top-1':>9} {'doc top-3':>9} {'sec/query':>9} {'|score err|':>11}")
    for label, r in res.items():
        print(f"{label:36} {np.mean(r['r10']):9.3f} {np.mean(r['r500']):10.3f} {np.mean(r['top1']):9.3f} "
              f"{np.mean(r['top3']):9.3f} {np.mean(r['t']):9.2f} {np.mean(r['gap']):11.5f}")
    base = res["today: list of doubles"]
    print("\npaired against today's index (the same 112 queries):")
    print(f"{'arm':36} {'same top-1 doc':>14} {'same top-5 docs (set)':>22} {'same top-10 chunks':>19}")
    for label, r in res.items():
        if r is base:
            continue
        same1 = np.mean([a[0] == b[0] for a, b in zip(r["docs"], base["docs"])])
        same5 = np.mean([len(set(a) & set(b)) / 5 for a, b in zip(r["docs"], base["docs"])])
        same10 = np.mean([len(set(a) & set(b)) / 10 for a, b in zip(r["chunks"], base["chunks"])])
        print(f"{label:36} {same1:14.3f} {same5:22.3f} {same10:19.3f}")


def drop() -> None:
    db = get_db()
    for i in list(db[NEW].list_search_indexes()):
        db[NEW].drop_search_index(i["name"])
    time.sleep(5)
    db[NEW].drop()
    print("dropped", NEW)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if cmd == "index":
        make_indexes(sys.argv[2:] or None)
    elif cmd == "dropindex":
        drop_index(sys.argv[2])
    else:
        {"migrate": migrate, "eval": evaluate, "drop": drop}[cmd]()
