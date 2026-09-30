"""AI Reel Summary 웹. 실행: python web.py (내 데이터) / python web.py --demo (예시 4개, demo.db) → http://localhost:8000"""
import json, sqlite3, sys
from contextlib import closing

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import bot  # 수집·분석 파이프라인(save_reel)과 Claude 클라이언트 재사용

DB_PATH = "demo.db" if "--demo" in sys.argv else "reels.db"
bot.DB_PATH = DB_PATH
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
    where = {"today": "WHERE date(created_at,'+9 hours')=date('now','+9 hours')", "todo": "WHERE status='todo'"}.get(scope, "")
    return [row(r) for r in q(f"SELECT * FROM reels {where} ORDER BY category, id DESC")]


@app.get("/api/reels/{rid}")
def reel(rid: int):
    rs = q("SELECT * FROM reels WHERE id=?", (rid,))
    if not rs:
        raise HTTPException(404, "없는 릴스예요.")
    r = row(rs[0])
    tags = set(r["details"].get("tags", []))
    scored = []
    for o in map(row, q("SELECT * FROM reels WHERE id!=?", (rid,))):  # 태그 겹침 + 같은 카테고리
        sc = 2 * len(tags & set(o["details"].get("tags", []))) + (o["category"] == r["category"])
        if sc:
            scored.append((sc, o))
    scored.sort(key=lambda x: -x[0])
    r["related"] = [{k: o[k] for k in ("id", "title", "category")} for _, o in scored[:4]]
    return r


class Add(BaseModel):
    url: str
    memo: str = ""


@app.post("/api/reels")
def add(b: Add):
    m = bot.URL_RE.search(b.url)
    if not m:
        raise HTTPException(400, "인스타그램 릴스 링크를 입력해 주세요.")
    url = m.group().split("?")[0]
    old = q("SELECT id FROM reels WHERE url=?", (url,))
    if old:
        return {"id": old[0]["id"]}
    rid, info, empty = bot.save_reel(url, b.memo)
    return {"id": rid}


@app.post("/api/reels/{rid}/toggle")
def toggle(rid: int):
    q("UPDATE reels SET status=CASE status WHEN 'done' THEN 'todo' ELSE 'done' END, done_at=CURRENT_TIMESTAMP WHERE id=?", (rid,), write=True)
    return {"ok": True}


class Chat(BaseModel):
    messages: list[dict]


@app.post("/api/reels/{rid}/chat")  # AI 대화: 이 엔드포인트만 바꾸면 RAG/에이전트로 확장 가능
def chat(rid: int, b: Chat):
    r = reel(rid)
    ctx = f"제목: {r['title']}\n요약: {r['details'].get('summary')}\n캡션: {(r['caption'] or '')[:1500]}\n전사문: {(r['transcript'] or '')[:8000]}"
    res = bot.llm.messages.create(model=bot.MODEL, max_tokens=900, messages=b.messages,
        system="저장한 인스타 릴스 내용을 바탕으로 학습을 돕는 도우미. 릴스에 나온 내용과 일반 지식을 구분해 한국어로 간결하게 답한다.\n" + ctx)
    return {"answer": res.content[0].text}


def D(title, one, topic, diff, tl, tv, kp, cn, qs, st, tg, recipe=None):
    return dict(title=title, one_liner=one, summary=one, topic=topic, difficulty=diff, time_label=tl, time_value=tv,
                key_points=[{"title": a, "desc": b} for a, b in kp], concepts=[{"term": a, "explain": b, "source": c} for a, b, c in cn],
                questions=qs, steps=st, tags=tg, recipe=recipe)


DEMO_DATA = [
    ("DEMO1", "dev_daily", "개발/AI", D("버블 정렬 3D 시각화", "버블 정렬의 비교·교환 과정을 3D 애니메이션으로 보여주는 영상입니다.", "자료구조 · 알고리즘", "초급", "예상 학습 시간", "약 3분",
        [("인접한 두 값을 비교한다", "배열의 인접한 두 원소를 차례로 비교합니다."), ("큰 값을 오른쪽으로 보낸다", "왼쪽 값이 더 크면 두 값을 교환합니다."), ("한 바퀴가 끝나면 가장 큰 값이 고정된다", "이 과정을 반복하면 배열이 정렬됩니다.")],
        [("Bubble Sort", "인접한 두 원소를 비교·교환하며 정렬하는 알고리즘입니다.", "reel"), ("3D Visualization", "알고리즘 동작을 3차원 애니메이션으로 표현하는 방식입니다.", "reel"), ("시간복잡도 O(n²)", "입력이 n배 늘면 연산이 약 n²배 늘어난다는 뜻입니다.", "ai")],
        ["버블 정렬은 퀵 정렬, 병합 정렬과 무엇이 다른가요?", "버블 정렬이 비효율적인 이유와 실무 사용처는?", "이 애니메이션을 직접 만들려면 어떤 기술이 필요한가요?"],
        ["파이썬으로 버블 정렬 구현해 보기", "패스마다 배열을 출력해 교환 과정 확인하기", "입력 크기를 늘려 실행 시간 재 보기"], ["버블정렬", "알고리즘", "자료구조", "3D시각화"])),
    ("DEMO2", "home_cook", "요리", D("계란 간장 덮밥", "냉장고 재료로 10분 안에 만드는 간단한 집밥 레시피입니다.", "자취 요리 · 덮밥", "초급", "예상 조리 시간", "약 10분",
        [("재료를 준비한다", "밥, 계란, 대파만 있으면 충분합니다."), ("양념을 먼저 섞는다", "간장과 설탕을 미리 섞어 두면 간이 고르게 밉니다."), ("계란은 중불에서 반숙으로", "불이 세면 금방 굳어 퍽퍽해집니다.")],
        [("중불", "불꽃이 팬 바닥에 살짝 닿는 정도의 중간 세기입니다.", "reel"), ("간 맞추기", "먹어 보며 짠맛과 단맛의 균형을 조절하는 일입니다.", "ai"), ("반숙", "노른자가 다 익지 않고 흐르는 상태입니다.", "ai")],
        ["간장 대신 쓸 수 있는 재료가 있나요?", "초보자가 가장 자주 하는 실수는?", "칼로리를 줄이려면 어떻게 바꾸나요?"],
        ["재료 준비하기", "영상 순서대로 직접 만들어 보기", "완성 후 간 확인하고 메모하기"], ["요리", "레시피", "집밥", "자취요리"],
        {"ingredients": ["밥 1공기", "계란 2개", "간장 1큰술", "설탕 0.5큰술", "대파 약간"], "order": ["양념(간장+설탕)을 섞는다", "팬에 기름을 두르고 중불로 달군다", "계란을 넣고 반숙으로 익힌다", "밥 위에 올리고 양념과 대파를 얹는다"]})),
    ("DEMO3", "focus_lab", "자기계발", D("하루를 효율적으로 쓰는 법", "우선순위를 정하고 한 번에 하나씩 집중하는 하루 계획법입니다.", "시간 관리 · 집중", "초급", "예상 소요 시간", "약 5분",
        [("해야 할 일을 적는다", "머릿속 할 일을 종이에 꺼내 놓습니다."), ("우선순위를 정한다", "중요한 일 하나를 가장 먼저 처리합니다."), ("한 번에 하나만 집중한다", "일정 시간마다 진행 상황을 확인합니다.")],
        [("우선순위", "여러 일 중 먼저 할 일의 순서입니다.", "reel"), ("집중 시간", "방해 없이 한 가지 일에 쓰는 시간 단위입니다.", "reel"), ("멀티태스킹", "여러 일을 번갈아 하는 것으로, 효율이 떨어질 수 있습니다.", "ai")],
        ["학생이라면 어떻게 적용할 수 있나요?", "하루 계획은 어떤 순서로 세우나요?", "계획을 못 지켰을 때 어떻게 수정하나요?"],
        ["내일 할 일 3개 적기", "가장 중요한 일 하나 먼저 처리하기", "하루 끝에 계획과 결과 비교하기"], ["자기계발", "시간관리", "생산성", "공부법"])),
    ("DEMO4", "money_note", "경제/재테크", D("월급 관리 기본 개념", "고정비와 변동비를 나눠 월급 흐름을 파악하는 방법을 설명하는 영상입니다.", "가계부 · 소비 관리", "초급", "예상 소요 시간", "약 5분",
        [("월급을 항목으로 나눈다", "고정비, 변동비, 저축으로 구분합니다."), ("고정비부터 확인한다", "매달 나가는 돈이 가장 큰 비중을 차지합니다."), ("변동비는 한도를 정한다", "식비·쇼핑 등은 월 예산을 정해 둡니다.")],
        [("고정비", "월세, 통신비처럼 매달 거의 일정하게 나가는 돈입니다.", "reel"), ("변동비", "식비, 여가비처럼 달마다 달라지는 지출입니다.", "reel"), ("복리", "이자에 다시 이자가 붙는 방식입니다.", "ai")],
        ["이 개념을 쉽게 설명하면?", "실제 생활에서는 어떻게 적용하나요?", "다른 관리 방법과 어떤 차이가 있나요?"],
        ["지난달 지출 확인하기", "항목별로 분류하기", "다음 달 예산 작성하기"], ["재테크", "가계부", "월급관리", "소비습관"])),
]


def seed():
    bot.init_db()
    if q("SELECT 1 FROM reels"):
        return
    for code, author, cat, d in DEMO_DATA:
        d["category"] = cat
        q("INSERT INTO reels (url,author,category,title,summary,details) VALUES (?,?,?,?,?,?)",
          (f"https://www.instagram.com/reel/{code}/", author, cat, d["title"], d["one_liner"], json.dumps(d, ensure_ascii=False)), write=True)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Reel Summary</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#F5F7FC;--ink:#1B2430;--mute:#667085;--line:#E4E8F0;--acc:#4F5BD5;--ok:#0E9F83}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 "IBM Plex Sans KR",system-ui,sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 20px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
header a{font-weight:700;font-size:18px;color:var(--ink);text-decoration:none}header a span{color:var(--acc)}header small{color:var(--mute)}
main{max-width:960px;margin:0 auto;padding:20px 16px 80px}
.bar{display:flex;gap:8px;margin-bottom:6px}.bar input{flex:1;min-width:0;font:inherit;padding:11px 16px;border:1px solid var(--line);border-radius:12px;background:#fff}
button,.btn{font:inherit;border:1px solid var(--line);background:#fff;color:var(--ink);padding:9px 16px;border-radius:12px;cursor:pointer;text-decoration:none;display:inline-block}
.pri{background:var(--acc);border-color:var(--acc);color:#fff}.ghost{color:var(--acc);border-color:var(--acc)}
button:disabled{opacity:.6}:focus-visible{outline:3px solid #2F6FED;outline-offset:2px}
#msg{color:var(--mute);font-size:14px;min-height:22px;margin-bottom:12px}
.sec{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:18px}
.sh{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}.sh h2{font-size:20px;margin:0}
.ic{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;color:#fff;font-size:17px}
.chip{font-size:12px;color:var(--acc);background:#EEF0FF;padding:2px 10px;border-radius:99px}
.top{display:grid;grid-template-columns:minmax(0,300px) 1fr;gap:22px}
.thumb{aspect-ratio:3/4;max-height:340px;border-radius:12px;background:linear-gradient(135deg,#232A4D,#4F5BD5);position:relative;overflow:hidden;display:grid;place-items:center;color:#fff;font-size:36px}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.src{font-weight:700}.mute{color:var(--mute);font-size:14px}
.cat{display:inline-block;margin:8px 0 0;color:var(--c);background:color-mix(in srgb,var(--c) 12%,#fff);padding:1px 10px;border-radius:99px;font-size:13px}
h1{font-size:28px;line-height:1.3;margin:6px 0 8px;overflow-wrap:anywhere}.sum{color:#39445A;margin:0 0 14px}
.strip{margin-top:18px;display:grid;grid-template-columns:1.6fr 2fr;gap:14px;background:#F5F7FF;border:1px solid #E1E5FB;border-radius:12px;padding:14px 16px}
.one b{display:block;color:var(--acc);font-size:14px}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.meta small{display:block;color:var(--mute);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.box{border:1px solid var(--line);border-radius:12px;padding:14px}.box b{display:block}.box p{margin:4px 0 0;color:var(--mute);font-size:14px}
.num{width:28px;height:28px;border-radius:50%;background:#E6F6F2;color:var(--ok);display:inline-grid;place-items:center;font-weight:700;margin-bottom:6px}
.tg{font-size:11px;padding:1px 8px;border-radius:99px;margin-left:6px;font-weight:500}.tg.reel{background:#E8F0FF;color:#2F5BD5}.tg.ai{background:#F1EAFF;color:#7A45D6}
.note{font-size:13px;color:var(--mute);background:#FFF8E6;border-radius:8px;padding:8px 12px;margin-top:12px}
.q{text-align:left;width:100%;display:flex;justify-content:space-between;gap:8px;border-radius:12px;padding:12px 14px}.q:hover{border-color:var(--acc)}
#chat{margin-top:14px}#log p{margin:6px 0;padding:8px 12px;border-radius:10px;background:#F5F7FC;white-space:pre-wrap}#log p.me{background:#EEF0FF}
.pg{margin-left:auto;color:var(--ok);font-weight:700;font-size:14px}.pb{width:90px;height:6px;background:var(--line);border-radius:9px;overflow:hidden}.pb i{display:block;height:100%;background:var(--ok)}
.step{display:flex;gap:10px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px}.step input{width:18px;height:18px;accent-color:var(--ok)}.step.on span{text-decoration:line-through;color:var(--mute)}
.tags span{display:inline-block;margin:0 8px 6px 0;background:#fff;border:1px solid var(--line);border-radius:99px;padding:1px 12px;font-size:14px}
.card{display:block;border:1px solid var(--line);border-left:5px solid var(--c);border-radius:8px;padding:10px 14px;margin-bottom:8px;color:inherit;text-decoration:none;background:#fff}.card.done{opacity:.55}.card p{margin:2px 0 0;color:var(--mute);font-size:14px}
h3.g{font-size:18px;margin:26px 0 8px;padding-left:10px;border-left:5px solid var(--c)}.acts{display:flex;gap:8px;flex-wrap:wrap}
details{color:var(--mute);margin-top:14px}nav{display:flex;gap:8px;margin:14px 0}nav .on{background:var(--ink);color:#fff}
@media(max-width:700px){.top,.strip{grid-template-columns:1fr}.thumb{max-height:220px;aspect-ratio:16/9}.bar{flex-direction:column}}
</style>
<header><a href="#/"><span>▶</span> AI Reel Summary</a><small>릴스를 더 깊이, AI와 함께</small></header>
<main>
<div class="bar"><input id="url" placeholder="Instagram 릴스 URL을 입력해보세요 (예: https://www.instagram.com/reel/xxxxx)"><button class="pri" id="go">요약하기</button></div>
<div id="msg"></div><div id="view"></div></main>
<script>
const $=s=>document.querySelector(s),V=$('#view');let scope='today',msgs=[];
const esc=s=>String(s??'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const api=async(u,o)=>{const r=await fetch(u,o),j=await r.json();if(!r.ok)throw j.detail||'오류가 났어요';return j};
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
const col=c=>`hsl(${[...c].reduce((a,ch)=>a+ch.charCodeAt(0)*7,0)%360} 50% 38%)`;
const ACT={'개발/AI':'코드 작성 · 직접 실행','요리':'직접 만들어보기','자기계발':'실제 생활에 적용','공부':'문제 풀기 · 복습','운동':'동작 따라하기','여행':'일정 만들어보기','경제/재테크':'내 소비·예산에 적용'};
const sec=(ic,bg,t,chip,body)=>`<section class="sec"><div class="sh"><span class="ic" style="background:${bg}">${ic}</span><h2>${t}</h2><span class="chip">${chip}</span></div>${body}</section>`;
$('#go').onclick=async()=>{const b=$('#go'),u=$('#url').value.trim();if(!u)return;b.disabled=true;b.textContent='분석 중…';
  $('#msg').textContent='영상 내려받기 → 음성 전사 → AI 정리까지 30초~1분 걸려요.';
  try{const j=await post('/api/reels',{url:u});$('#url').value='';$('#msg').textContent='';location.hash='#/reel/'+j.id}
  catch(e){$('#msg').textContent=e}b.disabled=false;b.textContent='요약하기'};
async function list(){
  const rows=await api('/api/reels?scope='+scope),g={};rows.forEach(r=>(g[r.category||'기타/정보']??=[]).push(r));
  V.innerHTML='<nav>'+[['today','오늘'],['todo','안 본 것'],['all','전체']].map(([k,l])=>`<button data-s="${k}" class="${k==scope?'on':''}">${l}</button>`).join('')+'</nav>'+
   (rows.length?Object.entries(g).map(([c,rs])=>`<h3 class="g" style="--c:${col(c)}">${esc(c)} <small class="mute">${rs.length}</small></h3>`+rs.map(r=>`<a class="card ${r.status}" style="--c:${col(c)}" href="#/reel/${r.id}"><b>${esc(r.title||'제목 없음')}</b><p>${esc(r.details.one_liner||r.summary)}</p></a>`).join('')).join('')
    :'<p class="mute">이 목록에 릴스가 없어요. 위에 릴스 URL을 넣거나 텔레그램 봇에 보내보세요.</p>');
  V.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{scope=b.dataset.s;list()});
}
async function detail(id){
  msgs=[];const r=await api('/api/reels/'+id),d=r.details||{},c=r.category||'기타/정보',col_=col(c);
  const kp=(d.key_points||[]).map(k=>typeof k=='string'?{title:k,desc:''}:k),st=d.steps||[],cn=d.concepts||[],key='steps'+id;
  const done=JSON.parse(localStorage.getItem(key)||'[]'),one=d.one_liner||(r.summary||'').split('. ')[0];
  const rc=d.recipe;
  V.innerHTML=`<article class="sec"><div class="top"><div class="thumb">${d.thumbnail?`<img src="${esc(d.thumbnail)}" referrerpolicy="no-referrer" onerror="this.remove()">`:''}<span>▶</span></div>
   <div><div class="src">Instagram Reel</div><div class="mute">${r.author?'@'+esc(r.author)+' · ':''}${esc((r.created_at||'').slice(0,10))}</div>
   <span class="cat" style="--c:${col_}">${esc(c)}</span><h1>${esc(r.title||'제목 없음')}</h1>
   <p class="sum">${esc(d.summary||r.summary||'요약이 아직 없어요. 텔레그램에서 /memo '+r.id+' 메모 로 내용을 추가해 보세요.')}</p>
   <a class="btn ghost" href="${esc(r.url)}" target="_blank" rel="noopener">원본 릴스 보기 ↗</a></div></div>
   <div class="strip"><div class="one"><b>한줄 요약</b>${esc(one)}</div><div class="meta"><div><small>주제</small>${esc(d.topic||'-')}</div><div><small>난이도</small>${esc(d.difficulty||'-')}</div><div><small>${esc(d.time_label||'예상 소요 시간')}</small>${esc(d.time_value||'-')}</div></div></div></article>`+
  (kp.length?sec('💡','#4F5BD5','핵심 정리','릴스에서 나온 핵심 내용','<div class="grid">'+kp.map((k,i)=>`<div class="box"><span class="num">${i+1}</span><b>${esc(k.title)}</b><p>${esc(k.desc)}</p></div>`).join('')+'</div>'+(c=='경제/재테크'?'<div class="note">영상의 내용을 정리한 것으로, 개인의 투자 판단을 위한 금융 조언은 아닙니다.</div>':'')):'')+
  (rc?sec('🍳','#E8833A','재료와 조리 순서','릴스에서 나온 내용',`<div class="grid"><div class="box"><b>재료</b><p>${(rc.ingredients||[]).map(esc).join('<br>')}</p></div><div class="box"><b>조리 순서</b><p>${(rc.order||[]).map((x,i)=>`${i+1}. ${esc(x)}`).join('<br>')}</p></div></div>`):'')+
  (cn.length?sec('📖','#5B6CF0','용어 풀이','릴스 + AI 추가 설명','<div class="grid">'+cn.map(x=>`<div class="box"><b>${esc(x.term)}<span class="tg ${x.source=='reel'?'reel':'ai'}">${x.source=='reel'?'릴스에서 언급':'AI 추가 설명'}</span></b><p>${esc(x.explain)}</p></div>`).join('')+'</div>'):'')+
  sec('🤖','#7A45D6','AI에게 물어볼 질문','AI 추천 질문',`<p class="mute" style="margin-top:0">질문을 누르면 AI와 바로 대화할 수 있어요.</p><div class="grid">${(d.questions||[]).map(x=>`<button class="q" data-q="${esc(x)}"><span>${esc(x)}</span><span>→</span></button>`).join('')}</div>
   <div id="chat"><div id="log"></div><div class="bar"><input id="ci" placeholder="이 릴스에 대해 궁금한 점을 물어보세요"><button class="pri" id="cs">AI와 대화하기</button></div></div>`)+
  (st.length?sec('✅','#0E9F83','해볼 것','AI가 생성한 체크리스트 · '+(ACT[c]||'실제 생활에 적용'),`<div class="sh" style="margin-top:-6px"><span class="pg" id="pg"></span><div class="pb"><i id="pi"></i></div></div>`+st.map((x,i)=>`<label class="step ${done.includes(i)?'on':''}"><input type="checkbox" data-i="${i}" ${done.includes(i)?'checked':''}><span>${esc(x)}</span></label>`).join('')):'')+
  ((d.tags||[]).length?`<p class="tags">${d.tags.map(t=>`<span>#${esc(t)}</span>`).join('')}</p>`:'')+
  (r.transcript?`<details><summary>영상 원문 스크립트</summary><p>${esc(r.transcript)}</p></details>`:'')+
  (r.related.length?'<h3 class="g" style="--c:#4F5BD5">같이 보면 좋은 저장 릴스</h3>'+r.related.map(x=>`<a class="card" style="--c:${col(x.category||'기타/정보')}" href="#/reel/${x.id}"><b>${esc(x.title||'제목 없음')}</b></a>`).join(''):'')+
  `<div class="acts" style="margin-top:20px"><a class="btn" href="${esc(r.url)}" target="_blank" rel="noopener">원본 릴스 보기 ↗</a><button class="btn pri" id="dn">${r.status=='done'?'안 본 것으로 되돌리기':'다 봤어요'}</button></div>`;
  const prog=()=>{const bx=[...V.querySelectorAll('.step input')],n=bx.filter(b=>b.checked).length;if($('#pg')){$('#pg').textContent=n+' / '+bx.length;$('#pi').style.width=(bx.length?n/bx.length*100:0)+'%'}};
  V.querySelectorAll('.step input').forEach(i=>i.onchange=()=>{const bx=[...V.querySelectorAll('.step input')];
    localStorage.setItem(key,JSON.stringify(bx.flatMap((e,n)=>e.checked?[n]:[])));i.parentNode.classList.toggle('on',i.checked);prog()});prog();
  const ask=async t=>{if(!t)return;const L=$('#log');msgs.push({role:'user',content:t});L.innerHTML+=`<p class="me">${esc(t)}</p><p id="wait" class="mute">답변 작성 중…</p>`;
    try{const j=await post(`/api/reels/${id}/chat`,{messages:msgs});msgs.push({role:'assistant',content:j.answer});$('#wait').outerHTML=`<p>${esc(j.answer)}</p>`}
    catch(e){msgs.pop();$('#wait').outerHTML=`<p class="mute">${esc(e)}</p>`}};
  V.querySelectorAll('.q').forEach(b=>b.onclick=()=>ask(b.dataset.q));
  $('#cs').onclick=()=>{const v=$('#ci').value.trim();$('#ci').value='';v?ask(v):$('#ci').focus()};
  $('#ci').onkeydown=e=>{if(e.key=='Enter')$('#cs').click()};
  $('#dn').onclick=async()=>{await post(`/api/reels/${id}/toggle`);detail(id)};
}
function route(){const p=location.hash.split('/');scrollTo(0,0);p[1]=='reel'?detail(p[2]):list()}
onhashchange=route;route();
</script></html>"""

if __name__ == "__main__":
    seed() if "--demo" in sys.argv else bot.init_db()
    uvicorn.run(app, host="127.0.0.1", port=8000)
