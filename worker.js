/**
 * Signal Worker — Cloudflare Worker
 * Запускается по cron, собирает новости и отправляет дайджесты пользователям
 */

// RSS источники — добавить новый: просто дописать строку в список
const RSS_FEEDS = {
  ru: [
    "https://rbc.ru/rss/news",
    "https://lenta.ru/rss/news",
    "https://vc.ru/rss",
    "https://habr.com/ru/rss/articles/",
    "https://pikabu.ru/rss.php",
    "https://smart-lab.ru/rss.xml",
    "https://www.it-world.ru/rss/",
    "https://4cio.ru/rss/",
  ],
  en: [
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TechCrunch",
  ],
};

const RSS_HEADERS = {
  "User-Agent": "Mozilla/5.0 (compatible; SignalBot/1.0)",
  Accept: "application/rss+xml, application/xml, text/xml",
};

// Расписание — когда отправлять дайджест (UTC часы)
const SCHEDULE = {
  hourly: "every_hour",
  every_3h: [0, 3, 6, 9, 12, 15, 18, 21],
  three_times: [6, 12, 18],   // утро, день, вечер (UTC)
  daily_morning: [6],          // 9:00 МСК = 6:00 UTC
  daily_evening: [16],         // 19:00 МСК = 16:00 UTC
};

export default {
  // HTTP handler (для проверки что воркер живой)
  async fetch(request, env) {
    return new Response("Signal Worker is running ✅");
  },

  // Cron handler — запускается каждый час
  async scheduled(event, env, ctx) {
    const currentHour = new Date().getUTCHours();
    console.log(`🔄 Signal Worker запущен. UTC час: ${currentHour}`);

    try {
      // Получаем всех пользователей с темами
      const users = await getActiveUsers(env);
      console.log(`👥 Пользователей с темами: ${users.length}`);

      for (const user of users) {
        for (const topic of user.topics) {
          // Проверяем нужно ли отправлять сейчас
          if (!shouldSendNow(topic.frequency, currentHour)) continue;

          console.log(`📰 Обрабатываю тему "${topic.name}" для user ${user.id}`);

          try {
            const digest = await buildDigest(user.id, topic, env);
            if (digest) {
              await sendTelegram(user.id, digest, env.TELEGRAM_TOKEN);
              console.log(`✅ Отправлено user ${user.id}, тема "${topic.name}"`);
            } else {
              console.log(`📭 Нет новых статей для "${topic.name}"`);
            }
          } catch (err) {
            console.error(`❌ Ошибка для user ${user.id}, тема "${topic.name}": ${err}`);
          }
        }
      }
    } catch (err) {
      console.error(`❌ Критическая ошибка: ${err}`);
    }
  },
};


// ============ РАСПИСАНИЕ ============

function shouldSendNow(frequency, currentHour) {
  if (frequency === "off") return false;
  if (frequency === "hourly") return true;
  const hours = SCHEDULE[frequency];
  if (!hours) return false;
  return hours.includes(currentHour);
}


// ============ SUPABASE ============

async function supabaseRequest(env, method, path, body = null) {
  const url = `${env.SUPABASE_URL}/rest/v1${path}`;
  const headers = {
    "apikey": env.SUPABASE_KEY,
    "Authorization": `Bearer ${env.SUPABASE_KEY}`,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
  };

  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Supabase error ${resp.status}: ${text}`);
  }

  const text = await resp.text();
  return text ? JSON.parse(text) : [];
}

async function getActiveUsers(env) {
  // Получаем все темы с user_id где frequency != off
  const topics = await supabaseRequest(
    env, "GET",
    `/topics?frequency=neq.off&select=*`
  );

  // Группируем по user_id
  const userMap = {};
  for (const topic of topics) {
    if (!userMap[topic.user_id]) {
      userMap[topic.user_id] = { id: topic.user_id, topics: [] };
    }
    userMap[topic.user_id].topics.push(topic);
  }

  return Object.values(userMap);
}

async function getSentUrls(env, userId, topicId) {
  const rows = await supabaseRequest(
    env, "GET",
    `/sent_articles?user_id=eq.${userId}&topic_id=eq.${topicId}&select=url`
  );
  return new Set(rows.map(r => r.url));
}

async function markSent(env, userId, topicId, urls) {
  if (!urls.length) return;
  const rows = urls.map(url => ({ user_id: userId, topic_id: topicId, url }));
  await supabaseRequest(env, "POST", "/sent_articles", rows);
}


// ============ ИСТОЧНИКИ ============

async function fetchTavily(topic, lang, apiKey) {
  try {
    const resp = await fetch("https://api.tavily.com/search", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        query: topic,
        search_depth: "basic",
        topic: "general",
        time_range: "week",
        max_results: 10,
        include_answer: false,
        include_raw_content: false,
      }),
    });

    if (!resp.ok) return [];
    const data = await resp.json();
    return (data.results || []).map(r => ({
      title: r.title || "",
      url: r.url || "",
      summary: (r.content || "").slice(0, 300),
      source: "Tavily",
    }));
  } catch (e) {
    console.error(`Tavily error: ${e}`);
    return [];
  }
}

async function fetchHackerNews() {
  try {
    const idsResp = await fetch("https://hacker-news.firebaseio.com/v0/topstories.json");
    const ids = (await idsResp.json()).slice(0, 20);
    const articles = [];

    for (const id of ids.slice(0, 12)) {
      const postResp = await fetch(`https://hacker-news.firebaseio.com/v0/item/${id}.json`);
      const post = await postResp.json();
      if (post?.title && post?.url) {
        articles.push({
          title: post.title,
          url: post.url,
          summary: `${post.score || 0} points, ${post.descendants || 0} comments`,
          source: "Hacker News",
        });
      }
    }
    return articles;
  } catch (e) {
    console.error(`HN error: ${e}`);
    return [];
  }
}

async function fetchReddit() {
  const subreddits = ["technology", "startups", "artificial"];
  const articles = [];

  for (const sub of subreddits) {
    try {
      const resp = await fetch(
        `https://www.reddit.com/r/${sub}/top.json?t=day&limit=8`,
        { headers: { "User-Agent": "SignalBot/1.0" } }
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      for (const post of data?.data?.children || []) {
        const d = post.data;
        articles.push({
          title: d.title || "",
          url: `https://reddit.com${d.permalink}`,
          summary: (d.selftext || d.title || "").slice(0, 200),
          source: `Reddit r/${sub}`,
        });
      }
    } catch (e) {
      console.error(`Reddit r/${sub} error: ${e}`);
    }
  }
  return articles;
}

async function fetchRSS(lang) {
  const feeds = RSS_FEEDS[lang] || RSS_FEEDS.en;
  const articles = [];

  for (const feedUrl of feeds) {
    try {
      const resp = await fetch(feedUrl, { headers: RSS_HEADERS });
      if (!resp.ok) continue;
      const text = await resp.text();

      // Простой XML парсер для RSS
      const items = text.match(/<item[\s\S]*?<\/item>/g) || [];
      for (const item of items.slice(0, 8)) {
        const title = (item.match(/<title[^>]*><!\[CDATA\[(.*?)\]\]><\/title>/) ||
                       item.match(/<title[^>]*>(.*?)<\/title>/))?.[1] || "";
        const link = (item.match(/<link[^>]*>(.*?)<\/link>/) ||
                      item.match(/<link[^>]*href="(.*?)"/))?.[1] || "";
        const desc = (item.match(/<description[^>]*><!\[CDATA\[(.*?)\]\]><\/description>/) ||
                      item.match(/<description[^>]*>(.*?)<\/description>/))?.[1] || "";

        if (title && link) {
          const sourceName = (text.match(/<channel>[\s\S]*?<title[^>]*>(.*?)<\/title>/)?.[1] || feedUrl);
          articles.push({
            title: title.replace(/<[^>]+>/g, "").trim(),
            url: link.trim(),
            summary: desc.replace(/<[^>]+>/g, "").slice(0, 300).trim(),
            source: sourceName.replace(/<[^>]+>/g, "").trim(),
          });
        }
      }
    } catch (e) {
      console.error(`RSS error ${feedUrl}: ${e}`);
    }
  }
  return articles;
}


// ============ GROQ ФИЛЬТРАЦИЯ ============

async function filterAndSummarize(articles, topic, apiKey) {
  if (!articles.length) return [];

  const articlesText = articles
    .map((a, i) => `${i}. ${a.title}: ${(a.summary || "").slice(0, 150)}`)
    .join("\n");

  const prompt = `Пользователь интересуется: "${topic}"

Вот список статей:
${articlesText}

Задача:
1. Выбери ТОП-5 статей которые МАКСИМАЛЬНО релевантны интересу пользователя по смыслу и интенту (не просто ключевые слова).
2. Для каждой выбранной статьи напиши ОДНО короткое предложение — почему она релевантна теме пользователя.

Ответь строго в JSON формате:
[
  {"index": 0, "reason": "Почему релевантно"},
  {"index": 3, "reason": "Почему релевантно"}
]
Только JSON, без пояснений.`;

  try {
    const resp = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "llama-3.3-70b-versatile",
        messages: [{ role: "user", content: prompt }],
        temperature: 0.2,
        max_tokens: 500,
      }),
    });

    const data = await resp.json();
    const content = data?.choices?.[0]?.message?.content || "";
    const match = content.match(/\[[\s\S]*\]/);
    if (!match) return articles.slice(0, 5);

    const selected = JSON.parse(match[0]);
    return selected
      .filter(item => item.index != null && item.index < articles.length)
      .map(item => ({ ...articles[item.index], reason: item.reason || "" }));
  } catch (e) {
    console.error(`Groq error: ${e}`);
    return articles.slice(0, 5);
  }
}


// ============ ДАЙДЖЕСТ ============

async function buildDigest(userId, topic, env) {
  // Защита от дублей — если недавно отправляли, пропускаем
  if (wasRecentlySent(topic, 30)) {
    console.log(`Пропускаем тему "${topic.name}" — недавно отправлялась`);
    return null;
  }

  const lang = topic.language || "ru";
  const allArticles = [];

  // Собираем из всех источников
  const tavily = await fetchTavily(topic.name, lang, env.TAVILY_API_KEY);
  allArticles.push(...tavily);

  if (lang === "en") {
    const hn = await fetchHackerNews();
    allArticles.push(...hn);
    const reddit = await fetchReddit();
    allArticles.push(...reddit);
  }

  const rss = await fetchRSS(lang);
  allArticles.push(...rss);

  // Убираем уже отправленные
  const sentUrls = await getSentUrls(env, userId, topic.id);
  const newArticles = allArticles.filter(a => a.url && !sentUrls.has(a.url));

  if (!newArticles.length) return null;

  // Groq фильтрация
  const filtered = await filterAndSummarize(newArticles, topic.name, env.GROQ_API_KEY);
  if (!filtered.length) return null;

  // Сохраняем в историю
  await markSent(env, userId, topic.id, filtered.map(a => a.url));
  await updateLastSent(env, topic.id);

  // Форматируем сообщение (HTML)
  let text = `📰 <b>Дайджест: ${escapeHtml(topic.name)}</b>\n\n`;
  for (let i = 0; i < filtered.length; i++) {
    const a = filtered[i];
    text += `<b>${i + 1}. ${escapeHtml(a.title)}</b>\n`;
    if (a.reason) text += `<i>💡 ${escapeHtml(a.reason)}</i>\n`;
    text += `📌 ${escapeHtml(a.source)}\n`;
    text += `🔗 ${escapeHtml(a.url)}\n\n`;
  }

  return text;
}


// ============ TELEGRAM ============

async function sendTelegram(chatId, text, token) {
  // Telegram лимит 4096 символов
  if (text.length > 4000) {
    text = text.slice(0, 4000) + "\n\n<i>[обрезано]</i>";
  }

  const resp = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
  });

  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`Telegram error: ${err}`);
  }
}


// ============ УТИЛИТЫ ============

function wasRecentlySent(topic, minutes = 30) {
  if (!topic.last_sent_at) return false;
  const lastSent = new Date(topic.last_sent_at);
  const diff = (Date.now() - lastSent.getTime()) / 1000 / 60;
  return diff < minutes;
}

async function updateLastSent(env, topicId) {
  await supabaseRequest(env, "PATCH", `/topics?id=eq.${topicId}`, {
    last_sent_at: new Date().toISOString(),
  });
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
