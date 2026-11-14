"""Local search page: type a query, see the relevant documents and pages.

    .venv/bin/python3 anara/app.py        # then open http://127.0.0.1:8050
    GET /api/search?q=...&k=50  ->  search() output as JSON

Listens on 127.0.0.1 only. Loads the embedding model once at startup.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from db import get_db  # noqa: E402
from embedder import get_model  # noqa: E402
from search import search  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_model()
    get_db().command("ping")
    yield


app = FastAPI(lifespan=lifespan)

PAGE = """<!doctype html><meta charset=utf-8><title>Document search</title>
<style>
body{font:15px system-ui;max-width:860px;margin:2rem auto;padding:0 1rem;color:#222}
form{display:flex;gap:.5rem}input{flex:1;padding:.6rem;font-size:16px}
button{padding:.6rem 1.2rem;font-size:16px;cursor:pointer}
.doc{border:1px solid #ddd;border-radius:6px;padding:.8rem 1rem;margin:1rem 0}
.doc h3{margin:0 0 .3rem;font-size:16px}.meta{color:#666;font-size:13px}
.pg{margin:.5rem 0 0 0;padding-left:.7rem;border-left:3px solid #8ab}
.pg b{font-size:13px}.pg p{margin:.15rem 0;color:#444;font-size:13px}
#status{color:#666;margin:1rem 0}
</style>
<h2>Document search</h2>
<form id=f><input id=q placeholder="Ask a question, e.g. how to control pink bollworm in cotton" autofocus>
<button>Search</button></form>
<div id=status></div><div id=out></div>
<script>
const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
f.onsubmit=async e=>{e.preventDefault();const q=document.getElementById('q').value.trim();if(!q)return;
 status.textContent='Searching...';out.innerHTML='';const t=performance.now();
 const r=await fetch('/api/search?q='+encodeURIComponent(q));const d=await r.json();
 status.textContent=d.length+' documents in '+((performance.now()-t)/1000).toFixed(1)+' s';
 out.innerHTML=d.map((x,i)=>`<div class=doc><h3>${i+1}. <a href="${x.link}" target=_blank>${esc(x.name)}</a></h3>
 <div class=meta>top-5 mean ${x.top5_mean.toFixed(3)} &middot; ${x.matches} chunks in pool &middot; best chunk ${x.score_max.toFixed(3)} &middot; pages ${x.pages.join(', ')}</div>
 ${x.page_hits.slice(0,3).map(p=>`<div class=pg><b>Page ${p.page}</b> (${p.score.toFixed(3)})<p>${esc(p.snippet.slice(0,260))}...</p></div>`).join('')}</div>`).join('')};
</script>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/search")
def api_search(q: str = Query(min_length=1), k: int | None = Query(None, ge=5, le=1000),
               rank: str = Query("top5", pattern="^(top5|count|best)$")):
    return search(q, k, rank=rank)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
