import os
import json
import re
import logging
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Хранилище: { user_id: { "topics": [...], "state": "" } }
USERS = {}


def get_user(user_id):
    if user_id not in USERS:
        USERS[user_id] = {"topics": [], "state": None, "pending": None}
    return USERS[user_id]


# ============ ИСТОЧНИКИ ============

async def fetch_google_news(topic: str) -> list:
    """Google News RSS по любой теме"""
    try:
        import feedparser
        url = f"https://news.google.com/rss/search?q={topic}&hl=ru&gl=RU&ceid=RU:ru"
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:15]:
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "summary": entry.get("summary", "")[:200],
                "source": "Google News"
            })
        return articles
    except Exception as e:
        logger.error(f"Google News error: {e}")
        return []


async def fetch_hacker_news() -> list:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            ids = (await client.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json"
            )).json()[:15]
            articles = []
            for post_id in ids:
                post = (await client.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{post_id}.json"
                )).json()
                articles.append({
                    "title": post.get("title", ""),
                    "url": post.get("url", f"https://news.ycombinator.com/item?id={post_id}"),
                    "summary": f"{post.get('score', 0)} points",
                    "source": "Hacker News"
                })
            return articles
    except Exception as e:
        logger.error(f"HN error: {e}")
        return []


async def fetch_rss(feeds: list, source_name: str) -> list:
    try:
        import feedparser
        articles = []
        for url in feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:5]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "summary": entry.get("summary", "")[:200],
                        "source": source_name
                    })
            except:
                pass
        return articles
    except:
        return []


async def fetch_reddit(topic: str) -> list:
    try:
        async with httpx.AsyncClient(
            timeout=8,
            headers={"User-Agent": "SignalBot/1.0"}
        ) as client:
            url = f"https://www.reddit.com/search.json?q={topic}&sort=hot&limit=10&t=day"
            resp = await client.get(url)
            posts = resp.json()["data"]["children"]
            articles = []
            for post in posts:
                d = post["data"]
                articles.append({
                    "title": d.get("title", ""),
                    "url": f"https://reddit.com{d.get('permalink', '')}",
                    "summary": d.get("selftext", "")[:200] or d.get("title", ""),
                    "source": f"Reddit r/{d.get('subreddit', '')}"
                })
            return articles
    except Exception as e:
        logger.error(f"Reddit error: {e}")
        return []


async def fetch_all(topic: str) -> list:
    """Собирает статьи из всех источников"""
    import asyncio

    google_task = fetch_google_news(topic)
    hn_task = fetch_hacker_news()
    reddit_task = fetch_reddit(topic)
    rss_task = fetch_rss([
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.techcrunch.com/techcrunch/",
        "https://habr.com/ru/rss/hubs/all/",
        "https://pikabu.ru/rss.php",
    ], "RSS")

    results = await asyncio.gather(google_task, hn_task, reddit_task, rss_task)
    all_articles = []
    for r in results:
        all_articles.extend(r)

    return all_articles


# ============ GROQ ФИЛЬТРАЦИЯ ============

async def filter_by_topic(articles: list, topic: str) -> list:
    if not articles or not GROQ_API_KEY:
        return articles[:5]

    articles_text = "\n".join([
        f"{i}. [{a['source']}] {a['title']}"
        for i, a in enumerate(articles[:30])
    ])

    prompt = f"""Пользователь интересуется темой: "{topic}"

Статьи (индекс. [источник] заголовок):
{articles_text}

Выбери ТОП-5 статей МАКСИМАЛЬНО релевантных теме "{topic}".
Если релевантных нет — выбери наиболее близкие.
Ответь ТОЛЬКО JSON-массивом индексов без пояснений, например: [0, 3, 7, 12, 18]"""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama3-8b-8192",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 50
                }
            )
            content = resp.json()["choices"][0]["message"]["content"]
            match = re.search(r'\[[\d,\s]+\]', content)
            if not match:
                return articles[:5]
            indices = json.loads(match.group())
            return [articles[i] for i in indices if i < len(articles)]
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return articles[:5]


# ============ МЕНЮ ============

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
        [InlineKeyboardButton("🔍 Получить дайджест", callback_data="choose_topic")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    user["state"] = None
    await update.message.reply_text(
        "📡 *Signal* — AI-дайджест новостей\n\n"
        "Добавь темы и получай релевантные новости из Google News, Reddit, Хабра, HN и других источников.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    user["state"] = "waiting_topic"
    await query.edit_message_text(
        "✏️ Напиши тему которая тебя интересует:\n\n"
        "_Например: исторические личности, крипта, AI-инструменты, футбол_",
        parse_mode="Markdown"
    )


async def list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    topics = user["topics"]

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
    for i, t in enumerate(topics):
        buttons.append([
            InlineKeyboardButton(f"❌ {t}", callback_data=f"del_{i}")
        ])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

    await query.edit_message_text(
        "📋 *Твои темы* (нажми чтобы удалить):",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def delete_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    idx = int(query.data.split("_")[1])

    if idx < len(user["topics"]):
        removed = user["topics"].pop(idx)
        await query.answer(f"Удалено: {removed}", show_alert=False)

    # Обновляем список
    topics = user["topics"]
    if not topics:
        await query.edit_message_text(
            "📋 Тем больше нет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
            ])
        )
        return

    buttons = []
    for i, t in enumerate(topics):
        buttons.append([InlineKeyboardButton(f"❌ {t}", callback_data=f"del_{i}")])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

    await query.edit_message_text(
        "📋 *Твои темы* (нажми чтобы удалить):",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def choose_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор темы перед дайджестом"""
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    topics = user["topics"]

    if not topics:
        await query.edit_message_text(
            "📋 Сначала добавь хотя бы одну тему!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")]
            ])
        )
        return

    buttons = []
    for i, t in enumerate(topics):
        buttons.append([InlineKeyboardButton(f"🔍 {t}", callback_data=f"digest_{i}")])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

    await query.edit_message_text(
        "По какой теме собрать дайджест?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def get_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить дайджест по выбранной теме"""
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    idx = int(query.data.split("_")[1])

    if idx >= len(user["topics"]):
        await query.edit_message_text("Тема не найдена.")
        return

    topic = user["topics"][idx]
    await query.edit_message_text(f"⏳ Собираю новости по теме *{topic}*...", parse_mode="Markdown")

    articles = await fetch_all(topic)

    if not articles:
        await query.edit_message_text(
            "❌ Не удалось получить новости. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]])
        )
        return

    filtered = await filter_by_topic(articles, topic)

    text = f"📰 *Дайджест: {topic}*\n\n"
    for a in filtered:
        text += f"• [{a['title']}]({a['url']})\n"
        text += f"  _{a['source']}_\n\n"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Другая тема", callback_data="choose_topic")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
        ]),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    user["state"] = None
    await query.edit_message_text(
        "📡 *Signal* — главное меню",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)

    if user["state"] == "waiting_topic":
        topic = update.message.text.strip()

        if topic in user["topics"]:
            await update.message.reply_text(
                f"⚠️ Тема *{topic}* уже добавлена.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
            return

        if len(user["topics"]) >= 10:
            await update.message.reply_text("⚠️ Максимум 10 тем.")
            return

        user["topics"].append(topic)
        user["state"] = None

        await update.message.reply_text(
            f"✅ Тема *{topic}* добавлена!\n\nВсего тем: {len(user['topics'])}",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "Используй кнопки меню 👇",
            reply_markup=main_menu_keyboard()
        )


# ============ MAIN ============

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан!")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(add_topic, pattern="^add_topic$"))
    app.add_handler(CallbackQueryHandler(list_topics, pattern="^list_topics$"))
    app.add_handler(CallbackQueryHandler(delete_topic, pattern="^del_\\d+$"))
    app.add_handler(CallbackQueryHandler(choose_topic, pattern="^choose_topic$"))
    app.add_handler(CallbackQueryHandler(get_digest, pattern="^digest_\\d+$"))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern="^menu$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Бот запущен (polling mode)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
