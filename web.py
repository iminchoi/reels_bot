"""릴스 노트 웹: bot.py가 만든 reels.db를 읽어 보여준다.  실행: python web.py → http://localhost:8000"""
import json, sqlite3
from contextlib import closing

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

DB_PATH = "reels.db"
app = FastAPI()


def q(sql, params=(), write=False):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(sql, params).fetchall()
        if write:
            db.commit()
        return rows


def row(r):
    d = dict(r)
    d["details"] = json.loads(d["details"]) if d.get("details") else {}
    return d


@app.get("/api/reels")
def reels(scope: str = "today"):
    where = {"today": "WHERE date(created_at,'+9 hours')=date('now','+9 hours')",
             "todo": "WHERE status='todo'"}.get(scope, "")
    return [row(r) for r in q(f"SELECT * FROM reels {where} ORDER BY category, id DESC")]


@app.get("/api/reels/{rid}")
def reel(rid: int):
    rs = q("SELECT * FROM reels WHERE id=?", (rid,))
    if not rs:
        raise HTTPException(404)
    r = row(rs[0])
    tags = set(r["details"].get("tags", []))
    scored = []
    for o in map(row, q("SELECT * FROM reels WHERE id!=?", (rid,))):  # 태그가 겹치는 저장 릴스 찾기
        s = 2 * len(tags & set(o["details"].get("tags", []))) + (o["category"] == r["category"])
        if s:
            scored.append((s, o))
    scored.sort(key=lambda x: -x[0])
    r["related"] = [{k: o[k] for k in ("id", "title", "summary", "category")} for _, o in scored[:5]]
    return r


@app.post("/api/reels/{rid}/toggle")
def toggle(rid: int):
    q("UPDATE reels SET status=CASE status WHEN 'done' THEN 'todo' ELSE 'done' END, "
      "done_at=CURRENT_TIMESTAMP WHERE id=?", (rid,), write=True)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Reels Note</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#F2F4F1;--ink:#1B2430;--mute:#5F6B7A;--line:#D9DEE4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 "IBM Plex Sans KR",system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:30px;line-height:1.3;margin:8px 0 12px}
h2{font-size:18px;margin:28px 0 10px;padding-left:10px;border-left:5px solid var(--c)}
h2 small{color:var(--mute);font-weight:400}
h3{font-size:17px;margin:28px 0 8px}
nav{display:flex;gap:8px}
button,.btn{font:inherit;border:1px solid var(--line);background:#fff;color:var(--ink);padding:6px 14px;border-radius:99px;cursor:pointer;text-decoration:none}
nav .on,.btn.pri{background:var(--ink);color:#fff;border-color:var(--ink)}
:focus-visible{outline:3px solid #2F6FED;outline-offset:2px}
.card{display:block;background:#fff;border:1px solid var(--line);border-left:5px solid var(--c);border-radius:4px;padding:12px 14px;margin-bottom:8px;color:inherit;text-decoration:none}
.card p{margin:4px 0 0;color:var(--mute);font-size:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card.done{opacity:.55}
.cat{color:var(--c);font-weight:700;margin:20px 0 0}
.sum{font-size:18px;line-height:1.8}
li{margin-bottom:6px}
.step{display:block;background:#fff;border:1px solid var(--line);padding:10px 12px;margin-bottom:6px;border-radius:4px}
.tags span{display:inline-block;margin:0 8px 4px 0;color:var(--mute);font-size:14px}
.acts{display:flex;gap:8px;margin-top:24px}
.back{color:var(--mute);text-decoration:none}
.empty{color:var(--mute);margin-top:40px}
</style>
<main id="app"></main>
<script>
const app=document.getElementById('app');let scope='today';
const col=c=>`hsl(${[...c].reduce((a,ch)=>a+ch.charCodeAt(0)*7,0)%360} 50% 36%)`;
const esc=s=>String(s??'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const api=async(u,o)=>(await fetch(u,o)).json();
async function list(){
  const rows=await api('/api/reels?scope='+scope),g={};
  rows.forEach(r=>(g[r.category||'기타']??=[]).push(r));
  app.innerHTML='<h1>릴스 노트</h1><nav>'+[['today','오늘'],['todo','안 본 것'],['all','전체']]
   .map(([k,l])=>`<button data-s="${k}" class="${k==scope?'on':''}">${l}</button>`).join('')+'</nav>'+
   (rows.length?Object.entries(g).map(([c,rs])=>`<h2 style="--c:${col(c)}">${esc(c)} <small>${rs.length}</small></h2>`+
     rs.map(r=>`<a class="card ${r.status}" style="--c:${col(c)}" href="#/reel/${r.id}"><b>${esc(r.title||'제목 없음')}</b><p>${esc(r.summary)}</p></a>`).join('')).join('')
    :'<p class="empty">이 목록에 릴스가 없어요. 텔레그램 봇에 릴스 링크를 보내보세요.</p>');
  app.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{scope=b.dataset.s;list()});
}
async function detail(id){
  const r=await api('/api/reels/'+id),d=r.details||{},c=r.category||'기타',k='steps'+id;
  const done=JSON.parse(localStorage.getItem(k)||'[]');
  app.innerHTML=`<a class="back" href="#/">← 목록</a><p class="cat" style="--c:${col(c)}">${esc(c)}</p><h1>${esc(r.title||'제목 없음')}</h1>
  <p class="sum">${esc(d.summary||r.summary||'요약이 아직 없어요. 텔레그램에서 /memo '+r.id+' 메모 로 내용을 추가해 보세요.')}</p>
  ${(d.key_points||[]).length?'<h3>핵심 정리</h3><ul>'+d.key_points.map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>':''}
  ${(d.steps||[]).length?'<h3>해볼 것</h3>'+d.steps.map((x,i)=>`<label class="step"><input type="checkbox" data-i="${i}" ${done.includes(i)?'checked':''}> ${esc(x)}</label>`).join(''):''}
  <p class="tags">${(d.tags||[]).map(t=>`<span>#${esc(t)}</span>`).join('')}</p>
  <div class="acts"><a class="btn" href="${esc(r.url)}" target="_blank" rel="noopener">원본 릴스 보기</a>
  <button class="btn pri" id="dn">${r.status=='done'?'안 본 것으로 되돌리기':'다 봤어요'}</button></div>
  ${r.related.length?'<h3>같이 보면 좋은 저장 릴스</h3>'+r.related.map(x=>`<a class="card" style="--c:${col(x.category||'기타')}" href="#/reel/${x.id}"><b>${esc(x.title||x.summary||'제목 없음')}</b></a>`).join(''):''}`;
  app.querySelectorAll('.step input').forEach(i=>i.onchange=()=>{
    const s=[...app.querySelectorAll('.step input')].flatMap((e,n)=>e.checked?[n]:[]);localStorage.setItem(k,JSON.stringify(s))});
  document.getElementById('dn').onclick=async()=>{await api(`/api/reels/${id}/toggle`,{method:'POST'});detail(id)};
}
function route(){const m=location.hash.match(/reel\\/(\\d+)/);scrollTo(0,0);m?detail(m[1]):list()}
onhashchange=route;route();
</script></html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
