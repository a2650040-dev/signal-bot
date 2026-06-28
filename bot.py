import os
import logging
import html
import httpx
from datetime import datetime, timezone
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FREQUENCY_LABELS = {
    "hourly":         "Каждый час",
    "every_3h":       "Каждые 3 часа",
    "three_times":    "Утром, днём и вечером",
    "daily_morning":  "Раз в день утром",
    "daily_evening":  "Раз в день вечером",
    "off":            "Выключить",
}


def e(text: str) -> str:
    return html.escape(str(text))


def detect_lang(text: str) -> str:
    cyrillic = any(c for c in text if "\u0400" <= c <= "\u04FF")
    return "ru" if cyrillic else "en"


# ============ SUPABASE ============

def ensure_user(user_id: int, username: str):
    existing = supabase.table("users").select("id").eq("id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "id": user_id,
            "username": username or ""
        }).execute()


def get_topics(user_id: int) -> list:
    result = supabase.table("topics").select("*").eq("user_id", user_id).execute()
    return result.data or []


def add_topic(user_id: int, name: str) -> bool:
    topics = get_topics(user_id)
    if len(topics) >= 10:
        return False
    if any(t["name"].lower() == name.lower() for t in topics):
        return False
    lang = detect_lang(name)
    supabase.table("topics").insert({
        "user_id": user_id,
        "name": name,
        "language": lang,
        "frequency": "daily_morning"
    }).execute()
    return True


def delete_topic(topic_id: int):
    supabase.table("topics").delete().eq("id", topic_id).execute()


def update_frequency(topic_id: int, frequency: str):
    supabase.table("topics").update({"frequency": frequency}).eq("id", topic_id).execute()


def get_sent_urls(user_id: int, topic_id: int) -> set:
    result = supabase.table("sent_articles") \
        .select("url") \
        .eq("user_id", user_id) \
        .eq("topic_id", topic_id) \
        .execute()
    return {row["url"] for row in (result.data or [])}


def mark_sent(user_id: int, topic_id: int, urls: list):
    rows = [{"user_id": user_id, "topic_id": topic_id, "url": url} for url in urls]
    if rows:
        supabase.table("sent_articles").insert(rows).execute()


def update_last_sent(topic_id: int):
    supabase.table("topics").update({
        "last_sent_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", topic_id).execute()



# ============ ИСТОЧНИКИ ============

async def fetch_tavily(topic: str, lang: str) -> list:
    country = "russia" if lang == "ru" else "united states"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"},
                json={
                    "query": topic,
                    "search_depth": "basic",
                    "topic": "general",
                    "time_range": "week",
                    "max_results": 10,
                    "country": country,
                    "include_answer": False,
                    "include_raw_content": False
                }
            )
            if resp.status_code != 200:
                return []
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""),
                 "summary": r.get("content", "")[:300], "source": "Tavily"}
                for r in resp.json().get("results", [])
            ]
    except Exception as ex:
        logger.error(f"Tavily error: {ex}")
        return []


async def fetch_hackernews() -> list:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            ids_resp = await client.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            ids = ids_resp.json()[:30]
            articles = []
            for post_id in ids[:15]:
                post_resp = await client.get(f"https://hacker-news.firebaseio.com/v0/item/{post_id}.json")
                post = post_resp.json()
                if post.get("title") and post.get("url"):
                    articles.append({
                        "title": post["title"],
                        "url": post["url"],
                        "summary": f"{post.get('score', 0)} points, {post.get('descendants', 0)} comments",
                        "source": "Hacker News"
                    })
            return articles
    except Exception as ex:
        logger.error(f"HN error: {ex}")
        return []


async def fetch_reddit(subreddits: list = None) -> list:
    if subreddits is None:
        subreddits = ["technology", "startups", "artificial"]
    articles = []
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "SignalBot/1.0"}) as client:
            for sub in subreddits:
                resp = await client.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=10")
                if resp.status_code != 200:
                    continue
                posts = resp.json().get("data", {}).get("children", [])
                for post in posts:
                    d = post["data"]
                    articles.append({
                        "title": d.get("title", ""),
                        "url": f"https://reddit.com{d.get('permalink', '')}",
                        "summary": d.get("selftext", "")[:200] or d.get("title", ""),
                        "source": f"Reddit r/{sub}"
                    })
    except Exception as ex:
        logger.error(f"Reddit error: {ex}")
    return articles


# RSS источники — добавить новый: просто дописать строку в список
RSS_FEEDS = {
    "ru": [
        "https://rbc.ru/rss/news",
        "https://lenta.ru/rss/news",
        "https://vc.ru/rss",
        "https://habr.com/ru/rss/articles/",
        "https://pikabu.ru/rss.php",
    ],
    "en": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.feedburner.com/TechCrunch",
    ]
}


async def fetch_rss(lang: str) -> list:
    import feedparser
    articles = []
    feeds = RSS_FEEDS.get(lang, RSS_FEEDS["en"])
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:8]:
                articles.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "summary": entry.get("summary", "")[:300],
                    "source": feed.feed.get("title", "RSS")
                })
        except Exception as ex:
            logger.error(f"RSS error {feed_url}: {ex}")
    return articles


# ============ GROQ ФИЛЬТРАЦИЯ ============

async def filter_and_summarize(articles: list, topic: str) -> list:
    """Groq фильтрует по смыслу/интенту и добавляет одно предложение релевантности."""
    if not articles:
        return []

    articles_text = "\n".join([
        f"{i}. {a['title']}: {a.get('summary', '')[:150]}"
        for i, a in enumerate(articles)
    ])

    prompt = f"""Пользователь интересуется: "{topic}"

Вот список статей:
{articles_text}

Задача:
1. Выбери ТОП-5 статей которые МАКСИМАЛЬНО релевантны интересу пользователя по смыслу и интенту (не просто ключевые слова).
2. Для каждой выбранной статьи напиши ОДНО короткое предложение — почему она релевантна теме пользователя.

Ответь строго в JSON формате:
[
  {{"index": 0, "reason": "Почему релевантно"}},
  {{"index": 3, "reason": "Почему релевантно"}}
]
Только JSON, без пояснений."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama3-8b-8192",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 500
                }
            )
            import json, re
            groq_data = resp.json()
            logger.info(f"Groq raw response: {groq_data}")
            content = groq_data["choices"][0]["message"]["content"]
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if not match:
                return articles[:5]
            selected = json.loads(match.group())
            result = []
            for item in selected:
                idx = item.get("index")
                reason = item.get("reason", "")
                if idx is not None and idx < len(articles):
                    article = dict(articles[idx])
                    article["reason"] = reason
                    result.append(article)
            return result
    except Exception as ex:
        logger.error(f"Groq error: {ex}")
        return articles[:5]


# ============ ДАЙДЖЕСТ ============

async def build_digest(user_id: int, topic: dict) -> str | None:
    """Собирает дайджест для одной темы одного пользователя."""
    topic_id = topic["id"]
    topic_name = topic["name"]
    lang = topic.get("language", "ru")

    # Собираем статьи из всех источников
    all_articles = []
    tavily = await fetch_tavily(topic_name, lang)
    all_articles.extend(tavily)

    if lang == "en":
        hn = await fetch_hackernews()
        all_articles.extend(hn)
        reddit = await fetch_reddit()
        all_articles.extend(reddit)

    rss = await fetch_rss(lang)
    all_articles.extend(rss)

    # Убираем уже отправленные
    sent = get_sent_urls(user_id, topic_id)
    new_articles = [a for a in all_articles if a.get("url") and a["url"] not in sent]

    if not new_articles:
        return None

    # Groq фильтрация
    filtered = await filter_and_summarize(new_articles, topic_name)
    if not filtered:
        return None

    # Сохраняем в историю
    mark_sent(user_id, topic_id, [a["url"] for a in filtered])
    update_last_sent(topic_id)

    # Форматируем сообщение
    lines = [f"📰 <b>Дайджест: {e(topic_name)}</b>\n"]
    for i, article in enumerate(filtered, 1):
        title = e(article.get("title", "Без заголовка"))
        url = e(article.get("url", ""))
        source = e(article.get("source", ""))
        reason = e(article.get("reason", ""))
        lines.append(f"<b>{i}. {title}</b>")
        if reason:
            lines.append(f"<i>💡 {reason}</i>")
        lines.append(f"📌 {source}")
        lines.append(f"🔗 {url}\n")

    return "\n".join(lines)


# ============ МЕНЮ ============

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
        [InlineKeyboardButton("🔍 Получить дайджест сейчас", callback_data="choose_topic")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")
    await update.message.reply_text(
        "📡 <b>Signal</b> — AI-радар новостей\n\n"
        "Добавь темы и получай персональные дайджесты.\n"
        "Фильтрация по смыслу, не по ключевым словам.",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )


async def cb_add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["state"] = "waiting_topic"
    await query.edit_message_text(
        "✏️ Напиши тему которая тебя интересует:\n\n"
        "<i>Например: гранты для стартапов в ЕС, новые AI-инструменты, крипта</i>",
        parse_mode="HTML"
    )


async def cb_list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topics = get_topics(query.from_user.id)

    if not topics:
        await query.edit_message_text(
            "📋 Тем пока нет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
            ])
        )
        return

    buttons = []
    for t in topics:
        freq = FREQUENCY_LABELS.get(t["frequency"], "")
        buttons.append([InlineKeyboardButton(
            f"⚙️ {t['name']} ({freq})", callback_data=f"topic_{t['id']}"
        )])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

    await query.edit_message_text(
        "📋 <b>Твои темы</b> — нажми для настройки:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def cb_topic_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topic_id = int(query.data.split("_")[1])
    context.user_data["selected_topic_id"] = topic_id

    # Получаем тему
    result = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not result.data:
        await query.edit_message_text("Тема не найдена.")
        return
    topic = result.data[0]

    buttons = [
        [InlineKeyboardButton("🔔 Частота уведомлений", callback_data=f"freq_{topic_id}")],
        [InlineKeyboardButton("🔍 Получить дайджест сейчас", callback_data=f"digest_{topic_id}")],
        [InlineKeyboardButton("❌ Удалить тему", callback_data=f"del_{topic_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="list_topics")],
    ]
    freq = FREQUENCY_LABELS.get(topic["frequency"], "")
    await query.edit_message_text(
        f"⚙️ <b>{e(topic['name'])}</b>\n"
        f"Частота: {e(freq)}\n"
        f"Язык: {topic['language'].upper()}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def cb_freq_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topic_id = int(query.data.split("_")[1])

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"setfreq_{topic_id}_{key}")]
        for key, label in FREQUENCY_LABELS.items()
    ]
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"topic_{topic_id}")])

    await query.edit_message_text(
        "🔔 Выбери частоту уведомлений:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_set_freq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    topic_id = int(parts[1])
    frequency = parts[2]

    update_frequency(topic_id, frequency)
    label = FREQUENCY_LABELS.get(frequency, "")
    await query.edit_message_text(
        f"✅ Частота обновлена: <b>{e(label)}</b>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К теме", callback_data=f"topic_{topic_id}")]
        ]),
        parse_mode="HTML"
    )


async def cb_delete_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topic_id = int(query.data.split("_")[1])
    delete_topic(topic_id)
    await query.edit_message_text(
        "❌ Тема удалена.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
        ])
    )


async def cb_choose_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topics = get_topics(query.from_user.id)

    if not topics:
        await query.edit_message_text(
            "📋 Сначала добавь хотя бы одну тему!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")]
            ])
        )
        return

    buttons = [[InlineKeyboardButton(f"🔍 {t['name']}", callback_data=f"digest_{t['id']}")] for t in topics]
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    await query.edit_message_text(
        "По какой теме собрать дайджест?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    topic_id = int(query.data.split("_")[1])
    user_id = query.from_user.id

    result = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not result.data:
        await query.edit_message_text("Тема не найдена.")
        return
    topic = result.data[0]

    await query.edit_message_text(
        f"⏳ Собираю дайджест по теме <b>{e(topic['name'])}</b>...",
        parse_mode="HTML"
    )

    digest = await build_digest(user_id, topic)

    if not digest:
        await query.edit_message_text(
            "📭 Новых статей по этой теме пока нет. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"digest_{topic_id}")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
            ])
        )
        return

    if len(digest) > 4000:
        digest = digest[:4000] + "\n\n<i>[обрезано]</i>"

    # Убираем кнопки у сообщения "⏳ Собираю..." (если есть)
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass

    # Отправляем дайджест новым сообщением
    await query.message.reply_text(
        digest,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"digest_{topic_id}")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["state"] = None

    # Убираем кнопки у предыдущего сообщения
    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
    except Exception:
        pass

    # Отправляем новое сообщение с меню
    await query.message.reply_text(
        "📡 <b>Signal</b> — главное меню",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or "")

    if context.user_data.get("state") == "waiting_topic":
        topic_name = update.message.text.strip()
        success = add_topic(user.id, topic_name)

        if success:
            topics = get_topics(user.id)
            await update.message.reply_text(
                f"✅ Тема <b>{e(topic_name)}</b> добавлена!\n"
                f"Всего тем: {len(topics)}\n\n"
                f"По умолчанию дайджест будет приходить <b>утром</b>. "
                f"Можешь изменить в настройках темы.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
        else:
            topics = get_topics(user.id)
            if len(topics) >= 10:
                await update.message.reply_text("⚠️ Максимум 10 тем.", reply_markup=main_menu_keyboard())
            else:
                await update.message.reply_text(
                    f"⚠️ Тема <b>{e(topic_name)}</b> уже есть.",
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard()
                )
        context.user_data["state"] = None
    else:
        await update.message.reply_text("Используй кнопки меню 👇", reply_markup=main_menu_keyboard())


# ============ MAIN ============

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_add_topic,      pattern="^add_topic$"))
    app.add_handler(CallbackQueryHandler(cb_list_topics,    pattern="^list_topics$"))
    app.add_handler(CallbackQueryHandler(cb_choose_topic,   pattern="^choose_topic$"))
    app.add_handler(CallbackQueryHandler(cb_topic_settings, pattern=r"^topic_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_freq_menu,      pattern=r"^freq_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_set_freq,       pattern=r"^setfreq_\d+_.+$"))
    app.add_handler(CallbackQueryHandler(cb_delete_topic,   pattern=r"^del_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_digest,         pattern=r"^digest_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_menu,           pattern="^menu$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
