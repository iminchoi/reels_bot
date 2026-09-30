"""릴스 노트 봇: 링크(여러 개 가능) → 캡션 수집 → Claude가 학습 노트로 정리 → SQLite 저장.
환경변수: TELEGRAM_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_OWNER_ID(권장), WEB_BASE(선택, 기본 http://localhost:8000)
"""
import asyncio, json, os, re, sqlite3
from contextlib import closing

import anthropic, yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

DB_PATH = "reels.db"
MODEL = "claude-haiku-4-5-20251001"
CATEGORIES = ["개발/AI", "공부/자격증", "일본어/취업", "운동/건강", "요리", "돈/재테크",
              "여행/맛집", "게임/축구", "SNS/마케팅", "기타"]
URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/\S+")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"]) if os.environ.get("TELEGRAM_OWNER_ID") else None
WEB_BASE = os.environ.get("WEB_BASE", "http://localhost:8000")
llm = anthropic.Anthropic()
EMPTY = {"category": "기타", "title": "", "summary": "", "key_points": [], "steps": [], "tags": []}


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
    for c in ("title", "details"):  # 기존 DB 업그레이드
        if c not in cols:
            db_run(f"ALTER TABLE reels ADD COLUMN {c} TEXT")


def fetch_meta(url):
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return (info.get("description") or info.get("title") or "", info.get("uploader") or "")
    except Exception as e:
        print("fetch 오류:", e)
        return "", ""


def analyze(text):
    """캡션/메모를 학습 노트(dict)로 정리한다."""
    if not text.strip():
        return dict(EMPTY)
    prompt = (f"인스타그램 릴스의 캡션/메모입니다.\n---\n{text[:3000]}\n---\n카테고리 목록: {CATEGORIES}\n"
              "공부용 노트로 정리해 JSON으로만 답하세요:\n"
              '{"category":"목록 중 하나","title":"20자 내외 제목","summary":"핵심 내용 4~6문장",'
              '"key_points":["핵심 3~5개"],"steps":["직접 해볼 행동 0~4개"],"tags":["키워드 3~5개"]}\n'
              "캡션에 없는 내용은 지어내지 말고, 정보가 부족하면 summary에 그렇게 적으세요.")
    try:
        res = llm.messages.create(model=MODEL, max_tokens=900, messages=[{"role": "user", "content": prompt}])
        data = json.loads(re.search(r"\{.*\}", res.content[0].text, re.S).group())
        if data.get("category") not in CATEGORIES:
            data["category"] = "기타"
        return {**EMPTY, **data}
    except Exception as e:
        print("analyze 오류:", e)
        return dict(EMPTY)


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
        caption, author = await asyncio.to_thread(fetch_meta, url)
        full = f"{caption}\n{memo}".strip()
        info = await asyncio.to_thread(analyze, full)
        db_run("INSERT INTO reels (url,author,caption,category,title,summary,details) VALUES (?,?,?,?,?,?,?)",
               (url, author, full, info["category"], info["title"], info["summary"], json.dumps(info, ensure_ascii=False)))
        rid = db_run("SELECT id FROM reels WHERE url=?", (url,), fetch=True)[0]["id"]
        note = "" if full else f"\n캡션을 못 가져왔어요. /memo {rid} 한 줄 메모"
        out.append(f"#{rid} [{info['category']}] {info['title']}\n{WEB_BASE}/#/reel/{rid}{note}")
    await update.message.reply_text("\n\n".join(out))


async def cmd_memo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    if len(ctx.args) < 2 or not ctx.args[0].isdigit():
        await update.message.reply_text("사용법: /memo 번호 메모내용")
        return
    rid, memo = int(ctx.args[0]), " ".join(ctx.args[1:])
    rows = db_run("SELECT caption FROM reels WHERE id=?", (rid,), fetch=True)
    if not rows:
        await update.message.reply_text("그 번호의 릴스가 없어요.")
        return
    full = f"{rows[0]['caption'] or ''}\n{memo}".strip()
    info = await asyncio.to_thread(analyze, full)
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
