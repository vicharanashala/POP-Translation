"""Scratch: synthetic benchmark from headings. NOT part of the app.

For each document with a crop heading level (zonesearch.crop_level), each crop-level heading that names exactly one
crop family starts a chapter; the body-size/bold headings inside it (sub-headings such as "Plant protection",
"Seed rate") each start a SECTION whose pages run to the next heading of any kind on a later page. Question =
"<sub-heading> <crop>" (the words a farmer would use are NOT in the section text necessarily). Gold = (doc, pages).
Only the source document is known to be right; other documents may answer too, so scores are lower bounds.
"""
import json, random, re, sys
sys.path.insert(0, "/home/paulose610/repos/annam/containers/POP-Translation/anara")
import zonesearch as z

S = z._load()
random.seed(7)
GENERIC = re.compile(r"^(introduction|contents|references|summary|annexure|appendix|table|fig|note|sl|page|index)\b", re.I)
out = []
for di, d in enumerate(S["docs"]):
    L = z.crop_level(di)
    if L is None or S["lengths"][di] < 20:
        continue
    rows = S["heads"].get(d, [])          # [page, text, ratio, bold, y]
    rows = sorted(rows, key=lambda r: (r[0], r[4]))
    named = {id(r): z._head_rows(di) for r in []}  # placeholder
    hr = z._head_rows(di)                  # same order as S["heads"][d] (unsorted)
    raw = S["heads"][d]
    items = [(raw[i][0], raw[i][4], raw[i][1], raw[i][2], hr[i][3]) for i in range(len(raw))]
    items.sort(key=lambda r: (r[0], r[1]))
    # chapters
    chap = [(i, it) for i, it in enumerate(items) if z._bucket(it[3]) >= L and it[4]
            and all(S["related"][next(iter(it[4]))][j] for j in it[4])]
    n_pages = int(S["lengths"][di])
    for k, (i, it) in enumerate(chap):
        end_i = next((j for j in range(i + 1, len(items)) if z._bucket(items[j][3]) >= L and items[j][2] != it[2]), len(items))
        end_page = items[end_i][0] - 1 if end_i < len(items) else n_pages
        sub = [x for x in items[i + 1:end_i] if z._bucket(x[3]) < L and 0.95 <= x[3] <= 1.3 and 4 <= len(x[2]) <= 40
               and not GENERIC.match(x[2]) and len(x[2].split()) <= 5]
        ci = next(iter(it[4]))
        crop = S["crops"][ci]["name"]
        for m, s_ in enumerate(sub):
            nxt = next((x for x in items[i + 1 + m + 1:end_i] if x[0] > s_[0]), None)
            p1 = (nxt[0] - 1 if nxt and nxt[0] > s_[0] else s_[0])
            p1 = max(p1, s_[0]); p1 = min(p1, end_page) if end_page >= s_[0] else s_[0]
            out.append({"doc": d, "doc_i": di, "crop": crop, "heading": s_[2], "p0": s_[0], "p1": p1,
                        "chapter": [it[0], end_page], "q": f"{s_[2]} {crop}"})
print("candidate questions:", len(out), "docs:", len({o['doc'] for o in out}))
import collections
def norm(h): return re.sub(r"[^a-z ]", "", h.lower()).strip()
ndoc = collections.defaultdict(set)
for o in out: ndoc[norm(o["heading"])].add(o["doc"])
common_heads = {h for h, ds in ndoc.items() if len(ds) >= 8 and len(h) >= 5}
print("recurring headings (>=8 docs):", len(common_heads))
cnt = collections.Counter((o["doc"], o["chapter"][0], norm(o["heading"])) for o in out)
keep = [o for o in out if norm(o["heading"]) in common_heads and cnt[(o["doc"], o["chapter"][0], norm(o["heading"]))] == 1
        and o["p1"] - o["p0"] <= 5]
for o in keep:
    o["q"] = f"{norm(o['heading'])} {o['crop']}"; o["key"] = [norm(o["heading"]), o["crop"]]
pool = collections.defaultdict(list)
for o in keep: pool[tuple(o["key"])].append([o["doc"], o["p0"], o["p1"]])
for o in keep: o["pool"] = pool[tuple(o["key"])]
random.shuffle(keep)
per = collections.Counter(); sel = []
for o in keep:
    if per[o["doc"]] < 5: per[o["doc"]] += 1; sel.append(o)
print("kept", len(keep), "selected", len(sel), "docs", len({o['doc'] for o in sel}))
json.dump(sel, open("/home/paulose610/repos/annam/containers/POP-Translation/anara/research/bench.json", "w"))
print(collections.Counter(norm(o["heading"]) for o in sel).most_common(30))
print([(o["q"], o["p0"], o["p1"], len(o["pool"])) for o in random.sample(sel, 10)])
