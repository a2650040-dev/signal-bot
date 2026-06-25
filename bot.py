import os
import json
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import httpx
from datetime import datetime

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Хранилище юзеров (в памяти — для MVP, потом заменить на БД)
USER_TOPICS = {}


# ============ GROQ LLM ============
async def filter_articles_by_intent(articles: list, topic: str) -> list:
    """Фильтрует статьи по смыслу. Возвращает топ-5 релевантных."""
    if not articles:
        return []

    articles_text = "\n".join([
        f"{i}. {a['title']}: {a.get('summary', a.get('description', ''))[:200]}"
        for i, a in enumerate(articles[:20])
    ])

    prompt = f"""
Ты — фильтр новостей. Пользователь интересуется: "{topic}"

Вот список статей:
{articles_text}

Выбери ТОП-5 статей, которые МАКСИМАЛЬНО релевантны интересу пользователя.
Учитывай не только ключевые слова, но и смысл, контекст, практическую ценность.

Ответь ТОЛЬКО JSON-массивом индексов, без пояснений:
[0, 2, 5, 7, 9]
"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "mixtral-8x7b-32768",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 100
                },
                timeout=10
            )

            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            # Безопасный парсинг — ищем массив в ответе
            import re
            match = re.search(r'\[[\d,\s]+\]', content)
            if not match:
                return articles[:5]

            indices = json.loads(match.group())
            return [articles[i] for i in indices if i < len(articles)]

    except Exception as e:
        logger.error(f"Groq error: {e}")
        return articles[:5]


# ============ ИСТОЧНИКИ ДАННЫХ ============
async def fetch_hacker_news() -> list:
    """Топ-10 постов с Hacker News."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json",
                timeout=5
            )
            ids = response.json()[:10]

            articles = []
            for post_id in ids:
                post_resp = await client.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{post_id}.json",
                    timeout=5
                )
                post = post_resp.json()
                articles.append({
                    "title": post.get("title", ""),
                    "url": post.get("url", f"https://news.ycombinator.com/item?id={post_id}"),
                    "summary": f"{post.get('score', 0)} points, {post.get('descendants', 0)} comments",
                    "source": "Hacker News"
                })

            return articles
    except Exception as e:
        logger.error(f"HN fetch error: {e}")
        return []


async def fetch_rss_feeds() -> list:
    """Собирает RSS из популярных источников."""
    feeds = [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.techcrunch.com/techcrunch/",
    ]

    articles = []
    try:
        import feedparser
        for feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:5]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "summary": entry.get("summary", "")[:300],
                        "source": feed.feed.get("title", "RSS")
                    })
            except Exception as e:
                logger.warning(f"RSS feed error {feed_url}: {e}")
    except ImportError:
        logger.warning("feedparser not installed")

    return articles


# ============ TELEGRAM HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
        [InlineKeyboardButton("🔍 Получить дайджест сейчас", callback_data="get_digest")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📡 *Signal* — AI-радар для новостей\n\n"
        "Добавь интересующие тебя темы, и я буду присылать релевантные новости "
        "с Hacker News, TechCrunch, ArsTechnica и других источников.\n\n"
        "Фильтрация работает на основе смысла, не просто ключевых слов.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить новую тему"""
    query = update.callback_query
    await query.answer()

    context.user_data["waiting_for_topic"] = True

    await query.edit_message_text(
        "✏️ Напиши тему, которая тебя интересует:\n"
        "_Например: «гранты для стартапов в ЕС» или «новые AI-инструменты»_",
        parse_mode="Markdown"
    )


async def handle_topic_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста темы"""
    if not context.user_data.get("waiting_for_topic"):
        return

    user_id = update.effective_user.id
    topic = update.message.text.strip()

    if user_id not in USER_TOPICS:
        USER_TOPICS[user_id] = []

    if len(USER_TOPICS[user_id]) >= 3:
        await update.message.reply_text(
            "⚠️ Достигнут лимит (3 темы для бесплатного плана).",
            parse_mode="Markdown"
        )
        return

    USER_TOPICS[user_id].append({
        "name": topic,
        "created_at": datetime.now().isoformat(),
        "frequency": "daily"
    })

    context.user_data["waiting_for_topic"] = False

    keyboard = [
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")],
        [InlineKeyboardButton("🔍 Получить дайджест сейчас", callback_data="get_digest")],
    ]

    await update.message.reply_text(
        f"✅ Тема *{topic}* добавлена!\n\n"
        f"У тебя {len(USER_TOPICS[user_id])} тем(ы).",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать все темы юзера"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    topics = USER_TOPICS.get(user_id, [])

    if not topics:
        keyboard = [[InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")]]
        await query.edit_message_text(
            "📋 У тебя пока нет тем. Добавь первую!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    text = "📋 *Твои темы:*\n\n"
    for i, topic in enumerate(topics, 1):
        text += f"{i}. *{topic['name']}* ({topic['frequency']})\n"

    keyboard = [
        [InlineKeyboardButton("🏠 Назад", callback_data="back_to_menu")]
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def get_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить дайджест прямо сейчас"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    topics = USER_TOPICS.get(user_id, [])

    if not topics:
        await query.edit_message_text(
            "📋 Сначала добавь хотя бы одну тему!\n\n"
            "Нажми ➕ Добавить тему в главном меню."
        )
        return

    await query.edit_message_text("⏳ Собираю новости, подожди...")

    # Собираем статьи
    hn_articles = await fetch_hacker_news()
    rss_articles = await fetch_rss_feeds()
    all_articles = hn_articles + rss_articles

    if not all_articles:
        await query.edit_message_text("❌ Не удалось получить новости. Попробуй позже.")
        return

    # Фильтруем по первой теме (для MVP)
    topic_name = topics[0]["name"]
    filtered = await filter_articles_by_intent(all_articles, topic_name)

    if not filtered:
        await query.edit_message_text("🤷 Релевантных статей не найдено.")
        return

    # Формируем дайджест
    text = f"📰 *Дайджест по теме: {topic_name}*\n\n"
    for article in filtered[:5]:
        text += f"🔗 [{article['title']}]({article['url']})\n"
        text += f"_{article['source']}_\n\n"

    keyboard = [[InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")]]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться в главное меню"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
        [InlineKeyboardButton("🔍 Получить дайджест сейчас", callback_data="get_digest")],
    ]

    await query.edit_message_text(
        "📡 *Signal* — главное меню",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ============ MAIN ============
def main():
    """Запуск бота через polling (Railway / локально)"""
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения!")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Регистрируем хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(add_topic, pattern="^add_topic$"))
    application.add_handler(CallbackQueryHandler(list_topics, pattern="^list_topics$"))
    application.add_handler(CallbackQueryHandler(get_digest, pattern="^get_digest$"))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic_input))

    logger.info("🚀 Бот запущен (polling mode)")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
