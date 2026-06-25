import os
import logging
import httpx
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

USERS = {}


def get_user(user_id):
    if user_id not in USERS:
        USERS[user_id] = {"topics": [], "state": None}
    return USERS[user_id]


# ============ TAVILY SEARCH ============

async def get_digest_from_tavily(topic: str) -> str:
    """
    Tavily Search API — реальный веб-поиск новостей.
    Возвращает свежие статьи с заголовком, описанием и ссылкой.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                headers={
                    "Authorization": f"Bearer {TAVILY_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "query": topic,
                    "search_depth": "basic",
                    "topic": "news",
                    "days": 7,           # новости за последние 7 дней
                    "max_results": 5,
                    "include_answer": False,
                    "include_raw_content": False
                }
            )
            data = resp.json()
            logger.info(f"Tavily status: {resp.status_code}")

            if resp.status_code != 200:
                logger.error(f"Tavily error: {data}")
                return None

            results = data.get("results", [])
            if not results:
                return None

            lines = []
            for i, item in enumerate(results, 1):
                title = item.get("title", "Без заголовка")
                snippet = item.get("content", "").strip()
                url = item.get("url", "")
                published = item.get("published_date", "")

                # Обрезаем snippet до ~200 символов
                if len(snippet) > 200:
                    snippet = snippet[:200].rsplit(" ", 1)[0] + "..."

                date_str = f" _{published}_" if published else ""
                lines.append(f"*{i}. {title}*{date_str}\n{snippet}\n🔗 {url}")

            return "\n\n".join(lines)

    except Exception as e:
        logger.error(f"Tavily error: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


# ============ МЕНЮ ============

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить тему", callback_data="add_topic")],
        [InlineKeyboardButton("📋 Мои темы", callback_data="list_topics")],
        [InlineKeyboardButton("🔍 Получить дайджест", callback_data="choose_topic")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_user(update.effective_user.id)["state"] = None
    await update.message.reply_text(
        "📡 *Signal* — AI-дайджест новостей\n\n"
        "Добавь любые темы и получай свежие новости из интернета.\n"
        "Работает на Tavily Search — реальный веб-поиск.",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )


async def add_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    get_user(query.from_user.id)["state"] = "waiting_topic"
    await query.edit_message_text(
        "✏️ Напиши тему которая тебя интересует:\n\n"
        "_Например: исторические личности, крипта, футбол, AI-инструменты_",
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

    buttons = [[InlineKeyboardButton(f"❌ {t}", callback_data=f"del_{i}")] for i, t in enumerate(topics)]
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
        user["topics"].pop(idx)

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

    buttons = [[InlineKeyboardButton(f"❌ {t}", callback_data=f"del_{i}")] for i, t in enumerate(topics)]
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])
    await query.edit_message_text(
        "📋 *Твои темы*:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def choose_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    buttons = [[InlineKeyboardButton(f"🔍 {t}", callback_data=f"digest_{i}")] for i, t in enumerate(topics)]
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu")])

    await query.edit_message_text(
        "По какой теме собрать дайджест?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def get_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    idx = int(query.data.split("_")[1])

    if idx >= len(user["topics"]):
        await query.edit_message_text("Тема не найдена.")
        return

    topic = user["topics"][idx]
    await query.edit_message_text(
        f"⏳ Ищу свежие новости по теме *{topic}*...",
        parse_mode="Markdown"
    )

    result = await get_digest_from_tavily(topic)

    if not result:
        await query.edit_message_text(
            "❌ Не удалось получить новости. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"digest_{idx}")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
            ])
        )
        return

    # Telegram ограничивает сообщение 4096 символами
    if len(result) > 3800:
        result = result[:3800] + "...\n\n_[текст обрезан]_"

    await query.edit_message_text(
        f"📰 *Дайджест: {topic}*\n\n{result}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"digest_{idx}")],
            [InlineKeyboardButton("🔍 Другая тема", callback_data="choose_topic")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu")]
        ]),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    get_user(query.from_user.id)["state"] = None
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
                f"⚠️ Тема *{topic}* уже есть.",
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
            f"✅ Тема *{topic}* добавлена! Всего тем: {len(user['topics'])}",
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
    if not TAVILY_API_KEY:
        raise ValueError("TAVILY_API_KEY не задан!")

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
