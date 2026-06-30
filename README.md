# Signal — AI News Radar

A Telegram bot that monitors the web 24/7 and delivers personalized news digests filtered by intent, not just keywords.

---

## Overview

Signal lets users define topics in plain language — *"AI tools for designers"*,  *"EU startup grants"* — and automatically delivers relevant articles on a schedule. A Groq-powered LLM filters results by semantic intent and explains why each article was selected.

The bot runs two independent layers: a Telegram interface on Railway and a cron worker on Cloudflare that wakes up every hour, aggregates sources, filters content, and pushes digests — without any user interaction.

---

## Features

- **Semantic filtering** — Groq LLM selects articles by meaning, not keyword matching, and explains relevance in one sentence per article
- **Auto-language detection** — topics written in Cyrillic route to Russian sources; Latin to English sources
- **Multi-source aggregation** — RSS feeds, Hacker News, Reddit, and Tavily web search combined per digest
- **No duplicates** — sent article URLs stored in Supabase; same article never appears twice
- **Spam protection** — `last_sent_at` prevents the worker from sending multiple digests in short succession; manual requests always go through
- **Flexible scheduling** — per-topic frequency: hourly / every 3h / 3×day / morning / evening / off
- **Up to 10 topics** per user, each independently configured

---

## Tech Stack

| Layer         | Technology                                     |
| ------------- | ---------------------------------------------- |
| Telegram Bot  | Python, `python-telegram-bot` 22.8             |
| Cron Worker   | Cloudflare Workers (JavaScript)                |
| LLM Filtering | Groq API (`llama-3.3-70b-versatile`)           |
| Web Search    | Tavily Search API                              |
| Database      | Supabase (PostgreSQL)                          |
| Hosting       | Railway (bot), Cloudflare (worker)             |
| RSS Parsing   | `feedparser` + `httpx` with browser User-Agent |

---

## Architecture

```
┌─────────────────────────────┐
│     Telegram User           │
└────────────┬────────────────┘
             │
┌────────────▼────────────────┐
│   Railway — bot.py          │  ← handles commands, manual digest requests
│   python-telegram-bot       │
└────────────┬────────────────┘
             │
┌────────────▼────────────────┐
│   Supabase (PostgreSQL)     │  ← users, topics, sent_articles, last_sent_at
└────────────┬────────────────┘
             │
┌────────────▼────────────────┐
│   Cloudflare Worker         │  ← cron 0 * * * *, auto-sends digests
│   worker.js                 │
└────────────┬────────────────┘
             │
    ┌────────┴─────────┐
    │                  │
┌───▼───┐         ┌────▼────┐
│Tavily │         │  Groq   │
│  HN   │         │  LLM    │
│Reddit │         └─────────┘
│  RSS  │
└───────┘
```

**Digest flow:**

1. Worker wakes up every hour
2. Checks which topics are due based on their frequency setting
3. Fetches articles from all sources
4. Filters out already-sent URLs via `sent_articles`
5. Sends remaining articles to Groq for semantic ranking
6. Pushes digest to Telegram; records sent URLs and `last_sent_at`

---

## Sources

### Russian (`lang: ru`)

**RSS:** RBC, Lenta.ru, VC.ru, Habr, Pikabu, Kommersant, Meduza, TASS, Izvestia, IT-World

**Reddit:** r/russia, r/russian, r/newsru, r/investing_ru, r/financeru, r/devops_ru, r/artificial, r/startups, r/technology, r/programming

**Search:** Tavily (global)

### English (`lang: en`)

**RSS:** Ars Technica, The Verge, TechCrunch

**Reddit:** r/technology, r/startups, r/artificial, r/programming

**Aggregator:** Hacker News Top Stories

**Search:** Tavily (global)

> Adding a new RSS source: one line in the `RSS_FEEDS` dict in both `bot.py` and `worker.js`.

---

## Database Schema

```sql
-- Users
CREATE TABLE users (
    id BIGINT PRIMARY KEY,  -- telegram user_id
    username TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Topics
CREATE TABLE topics (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    language TEXT DEFAULT 'ru',
    frequency TEXT DEFAULT 'daily_morning',
    last_sent_at TIMESTAMP DEFAULT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Deduplication log
CREATE TABLE sent_articles (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT NOW()
);
```

---

## Environment Variables

### Railway (`bot.py`)

| Variable         | Description                 |
| ---------------- | --------------------------- |
| `TELEGRAM_TOKEN` | Bot token from @BotFather   |
| `SUPABASE_URL`   | Supabase project URL        |
| `SUPABASE_KEY`   | Supabase `service_role` key |
| `GROQ_API_KEY`   | Groq API key                |
| `TAVILY_API_KEY` | Tavily API key              |

### Cloudflare Worker (`worker.js`)

Same variables set via **Settings → Variables and Secrets** in the Cloudflare dashboard.

---

## Deployment

### Bot (Railway)

```bash
# Connect your GitHub repo to Railway
# Railway auto-deploys on push to main

# Procfile
web: python bot.py
```

### Worker (Cloudflare)

1. Go to **Workers & Pages** → Create Worker
2. Paste `worker.js` into the editor → Deploy
3. Go to **Settings → Triggers → Cron Triggers** → Add `0 * * * *`
4. Add environment variables under **Settings → Variables and Secrets**

---

## Local Development

```bash
git clone https://github.com/a2650040-dev/signal-bot.git
cd signal-bot

pip install -r requirements.txt

cp .env.example .env
# Fill in your keys

python bot.py
```

**requirements.txt**

```
python-telegram-bot==22.8
httpx==0.28.1
python-dotenv==1.0.0
supabase==2.15.2
feedparser==6.0.11
```

---
