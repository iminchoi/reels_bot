"""릴스 노트 봇: 링크(여러 개 가능) → 캡션 수집 → Claude가 학습 노트로 정리 → SQLite 저장.
환경변수: TELEGRAM_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_OWNER_ID(권장), WEB_BASE(선택, 기본 http://localhost:8000)
"""
import asyncio, glob, json, os, re, shutil, sqlite3, tempfile
from contextlib import closing

import anthropic, yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

DB_PATH = "reels.db"
MODEL = "claude-haiku-4-5-20251001"
CATEGORIES = ["개발/AI", "요리", "자기계발", "경제/재테크", "운동", "여행", "공부", "패션", "제품 리뷰", "뉴스/정보", "기타/정보"]
TEMPLATES = {  # 카테고리별 노트 지침: 프롬프트에 그대로 들어간다
    "개발/AI": "핵심=동작 원리·구현 방식·시간복잡도·활용 / 용어=언어·알고리즘·API / 질문=다른 방식과 비교·코드 구현·면접·실무 / 해볼것=예제 코드 작성·직접 실행 / 시간=예상 학습 시간",
    "요리": "핵심=재료·손질·양념 순서·불 조절 / 용어=조리 용어 / 질문=재료 대체·초보 주의점·칼로리·인분 / 해볼것=직접 만들어보기 / 시간=예상 조리 시간 / recipe에 재료와 조리 순서",
    "자기계발": "핵심=실행 단계 / 용어=우선순위·집중 시간 등 / 질문=내 상황 적용·계획 수정 / 해볼것=실제 생활에 적용 / 시간=예상 소요 시간",
    "경제/재테크": "핵심=영상이 설명한 개념·주장·핵심 숫자·주의점(객관적으로, 투자 조언 금지) / 용어=고정비·복리 등 / 질문=쉬운 설명·생활 적용·다른 방법과 차이 / 해볼것=내 지출·예산에 적용 / 시간=예상 소요 시간",
    "운동": "핵심=동작·자세·횟수 / 해볼것=동작 따라하기", "여행": "핵심=장소·동선·팁 / 해볼것=일정 만들어보기", "공부": "핵심=개념·공식 / 해볼것=문제 풀기·복습",
}
URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/\S+")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"]) if os.environ.get("TELEGRAM_OWNER_ID") else None
WEB_BASE = os.environ.get("WEB_BASE", "http://localhost:8000")
llm = anthropic.Anthropic()
EMPTY = {"category": "기타/정보", "title": "", "summary": "", "one_liner": "", "topic": "", "difficulty": "", "time_label": "예상 소요 시간", "time_value": "", "key_points": [], "concepts": [], "questions": [], "steps": [], "tags": [], "recipe": None}


def db_run(sql, params=(), fetch=False):
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.row_factory = sqlite3.Row
        cur = db.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        db.commit()
        return rows


def init_db():
    db_run("""CREATE TABLE IF NOT EXISTS reels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL, author TEXT, caption TEXT,
        category TEXT, summary TEXT, status TEXT NOT NULL DEFAULT 'todo',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, done_at TEXT)""")
    cols = {r["name"] for r in db_run("PRAGMA table_info(reels)", fetch=True)}
    for c in ("title", "details", "transcript"):  # 기존 DB 업그레이드
        if c not in cols:
            db_run(f"ALTER TABLE reels ADD COLUMN {c} TEXT")


_whisper = None


def fetch_reel(url):
    """캡션, 작성자, 음성 전사문을 가져온다. 요청 한 번으로 오디오까지 받아 차단 위험을 줄인다."""
    global _whisper
    tmp = tempfile.mkdtemp()
    opts = {"quiet": True, "format": "bestaudio/best", "outtmpl": f"{tmp}/%(id)s.%(ext)s"}
    if os.environ.get("IG_COOKIES"):  # 선택: 로그인 쿠키 파일(cookies.txt) 경로
        opts["cookiefile"] = os.environ["IG_COOKIES"]
    caption = author = transcript = thumb = ""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        caption = info.get("description") or info.get("title") or ""
        author = info.get("uploader") or ""
        thumb = info.get("thumbnail") or ""
        files = glob.glob(f"{tmp}/*")
        if files:
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
            segs, _ = _whisper.transcribe(files[0], vad_filter=True)
            transcript = " ".join(s.text.strip() for s in segs)
    except Exception as e:
        print("fetch/전사 오류:", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return caption, author, transcript, thumb


def analyze(text, transcript=""):
    """캡션/메모/영상 전사문을 학습 노트(dict)로 정리한다."""
    if not (text.strip() or transcript.strip()):
        return dict(EMPTY)
    guide = "\n".join(f"- {k}: {v}" for k, v in TEMPLATES.items())
    prompt = (f"인스타그램 릴스의 캡션/메모와 음성 전사문입니다(영어일 수 있음).\n[캡션/메모]\n{text[:2000]}\n[전사문]\n{transcript[:12000]}\n---\n"
              f"1) 카테고리 목록 {CATEGORIES} 중 하나를 고르고 2) 그 카테고리 지침에 맞춰 한국어 학습 노트를 JSON으로만 답하세요. 전문용어는 영어 병기.\n지침:\n{guide}\n"
              '{"category":"","title":"20자 내외","summary":"6~10문장","one_liner":"한 줄 요약 1문장","topic":"주제 (2개 이내, · 로 구분)",'
              '"difficulty":"초급|중급|고급","time_label":"예상 학습 시간|예상 조리 시간|예상 소요 시간","time_value":"약 N분",'
              '"key_points":[{"title":"짧은 제목","desc":"1~2문장"}] 3~5개(릴스에서 나온 내용만),'
              '"concepts":[{"term":"","explain":"","source":"reel 또는 ai"}] 3~5개(릴스에 언급됐으면 reel, 이해를 돕기 위해 추가했으면 ai),'
              '"questions":["3개"],"steps":["구체적으로 실행 가능한 행동 3~4개"],"tags":["3~5개"],'
              '"recipe":null 또는 {"ingredients":["재료"],"order":["조리 순서"]}(요리만)}\n'
              "내용에 없는 것은 key_points에 넣지 마세요. 경제/재테크는 투자 조언처럼 쓰지 마세요. 화면으로만 보인 코드/도식은 전사문에 없으니 정보가 부족하면 summary에 그렇게 적으세요.")
    try:
        res = llm.messages.create(model=MODEL, max_tokens=3000, messages=[{"role": "user", "content": prompt}])
        data = json.loads(re.search(r"\{.*\}", res.content[0].text, re.S).group())
        if data.get("category") not in CATEGORIES:
            data["category"] = "기타/정보"
        return {**EMPTY, **data}
    except Exception as e:
        print("analyze 오류:", e)
        return dict(EMPTY)


def save_reel(url, memo=""):
    """수집 → 분석 → 저장. 봇과 웹이 함께 쓴다. (id, info, 내용이 비었는지) 반환"""
    caption, author, transcript, thumb = fetch_reel(url)
    full = f"{caption}\n{memo}".strip()
    info = analyze(full, transcript)
    info["thumbnail"] = thumb
    db_run("INSERT INTO reels (url,author,caption,transcript,category,title,summary,details) VALUES (?,?,?,?,?,?,?,?)",
           (url, author, full, transcript, info["category"], info["title"], info["summary"], json.dumps(info, ensure_ascii=False)))
    rid = db_run("SELECT id FROM reels WHERE url=?", (url,), fetch=True)[0]["id"]
    return rid, info, not (full or transcript)


def allowed(update):
    return OWNER_ID is None or update.effective_user.id == OWNER_ID


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text = update.message.text or ""
    urls = list(dict.fromkeys(u.split("?")[0] for u in URL_RE.findall(text)))
    if not urls:
        await update.message.reply_text("인스타 릴스 링크를 보내주세요. 여러 개를 한 번에 보내도 되고, 링크 뒤에 메모를 붙여도 돼요.")
        return
    memo = URL_RE.sub("", text).strip()
    out = []
    for i, url in enumerate(urls):
        old = db_run("SELECT id FROM reels WHERE url=?", (url,), fetch=True)
        if old:
            out.append(f"이미 저장된 릴스예요 #{old[0]['id']}")
            continue
        if i:
            await asyncio.sleep(2)  # 연속 요청 차단 방지
        rid, info, empty = await asyncio.to_thread(save_reel, url, memo)
        note = f"\n내용을 못 가져왔어요. /memo {rid} 한 줄 메모" if empty else ""
        out.append(f"#{rid} [{info['category']}] {info['title']}\n{WEB_BASE}/#/reel/{rid}{note}")
    await update.message.reply_text("\n\n".join(out))


async def cmd_memo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if len(ctx.args) < 2 or not ctx.args[0].isdigit():
        await update.message.reply_text("사용법: /memo 번호 메모내용")
        return
    rid, memo = int(ctx.args[0]), " ".join(ctx.args[1:])
    rows = db_run("SELECT caption, transcript FROM reels WHERE id=?", (rid,), fetch=True)
    if not rows:
        await update.message.reply_text("그 번호의 릴스가 없어요.")
        return
    full = f"{rows[0]['caption'] or ''}\n{memo}".strip()
    info = await asyncio.to_thread(analyze, full, rows[0]["transcript"] or "")
    old = json.loads(db_run("SELECT details FROM reels WHERE id=?", (rid,), fetch=True)[0]["details"] or "{}")
    info["thumbnail"] = old.get("thumbnail", "")
    db_run("UPDATE reels SET caption=?,category=?,title=?,summary=?,details=? WHERE id=?",
           (full, info["category"], info["title"], info["summary"], json.dumps(info, ensure_ascii=False), rid))
    await update.message.reply_text(f"#{rid} 다시 정리 → [{info['category']}] {info['title']}\n{WEB_BASE}/#/reel/{rid}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    cat = " ".join(ctx.args) if ctx.args else None
    sql = "SELECT * FROM reels WHERE status='todo'" + (" AND category=?" if cat else "") + " ORDER BY id LIMIT 10"
    rows = db_run(sql, (cat,) if cat else (), fetch=True)
    await update.message.reply_text("\n\n".join(
        f"#{r['id']} [{r['category']}] {r['title'] or r['summary'] or '(요약 없음)'}\n{WEB_BASE}/#/reel/{r['id']}"
        for r in rows) or "볼 릴스가 없어요!")


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("사용법: /done 번호")
        return
    db_run("UPDATE reels SET status='done', done_at=CURRENT_TIMESTAMP WHERE id=?", (int(ctx.args[0]),))
    await update.message.reply_text("완료 처리했어요 ✅")


def main():
    init_db()
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    for name, fn in (("list", cmd_list), ("done", cmd_done), ("memo", cmd_memo)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
