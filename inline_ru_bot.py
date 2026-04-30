#!/usr/bin/env python3
"""
Агрегатор новостей: inline.ru + theins.ru + meduza.io → Telegram.

Установка зависимостей:
    pip install requests beautifulsoup4 python-telegram-bot

Конфигурация в config.json:
    { "BOT_TOKEN": "...", "CHAT_ID": "..." }
"""

import os
import re
import json
import time
import hashlib
import asyncio
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

CONFIG = {
    "SECTIONS": [
        "http://www.inline.ru/economi.asp?NewsID={}",
        "http://www.inline.ru/business.asp?NewsID={}",
        "http://www.inline.ru/market.asp?NewsID={}",
        "http://www.inline.ru/polit.asp?NewsID={}",
        "http://www.inline.ru/sobytie.asp?NewsID={}",
        "http://www.inline.ru/sport.asp?NewsID={}",
        "http://www.inline.ru/hitech.asp?NewsID={}",
        "http://www.inline.ru/medic.asp?NewsID={}",
    ],
    "START_NEWS_ID":     788460,
    "SUMMARY_SENTENCES": 3,
    "THEINS_RSS":        "https://theins.ru/feed",
    "MEDUZA_RSS":        "https://meduza.io/rss/all",
    "POLL_INTERVAL":     10 * 60,
    "HISTORY_SIZE":      100,
    "STATE_FILE":        "news_agg_state.json",
    "CONFIG_FILE":       "config.json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def internet_available() -> bool:
    """Быстрая проверка интернета — DNS-запрос к 8.8.8.8."""
    import socket
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo("8.8.8.8", 53)
        return True
    except Exception:
        return False


def load_credentials() -> dict:
    path = CONFIG["CONFIG_FILE"]
    if not os.path.exists(path):
        log.error(f"Файл {path} не найден!")
        raise SystemExit(1)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ("BOT_TOKEN", "CHAT_ID"):
        if not data.get(key):
            log.error(f"В {path} отсутствует {key}")
            raise SystemExit(1)
    return data


def load_state() -> dict:
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_pub_dt":        "",
        "published_ids":      [],
        "history_fps":        [],
        "inline_last_id":     CONFIG["START_NEWS_ID"] - 1,
        "inline_last_title":  "",
        "inline_last_pub_dt": "",
    }


def save_state(state: dict):
    with open(CONFIG["STATE_FILE"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def format_dt(dt: datetime) -> str:
    if not dt:
        return ""
    msk = dt.astimezone(timezone(timedelta(hours=3)))
    M = ["","января","февраля","марта","апреля","мая","июня",
         "июля","августа","сентября","октября","ноября","декабря"]
    return f"{msk.day} {M[msk.month]} {msk.year}, {msk.strftime('%H:%M')}"


def make_fp(title: str, text: str) -> str:
    s = (title + " " + text)[:200].lower()
    s = re.sub(r"\s+", " ", s).strip()
    return hashlib.md5(s.encode()).hexdigest()


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    def words(s):
        return set(re.findall(r"[а-яёa-z]{4,}", s.lower()))
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= threshold


def _stem(w: str) -> str:
    """Префиксный стемминг: срезаем падежные окончания."""
    if len(w) >= 8: return w[:6]   # квартиры/квартирой → кварти
    if len(w) >= 6: return w[:5]   # долина/долины → долин
    if len(w) >= 4: return w[:-1]  # дела/делу → дел
    return w


def similar_titles(a: str, b: str, threshold: float = 0.38) -> bool:
    """Сравниваем заголовки со стеммингом — ловит дубли с разными падежами."""
    def stemmed(s):
        return set(_stem(w) for w in re.findall(r"[а-яёa-z]{4,}", s.lower()))
    wa, wb = stemmed(a), stemmed(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= threshold


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def deduplicate(items: list, history_fps: list, history_texts: list = None) -> list:
    """history_texts — тексты опубликованных новостей из прошлых циклов."""
    result     = []
    seen_fps   = set(history_fps)
    seen_texts = list(history_texts or [])  # включаем историю из прошлых циклов
    for item in items:
        f = make_fp(item["title"], item.get("text", ""))
        if f in seen_fps:
            log.info("[dedup] Дубль: «" + item['title'][:50] + "» [" + item.get('source','') + "]")
            continue
        combined = item["title"] + " " + item.get("text", "")
        # Проверяем похожесть полного текста (60%) или только заголовков (40%)
        title = item["title"]
        is_dup = False
        for t in seen_texts[-50:]:
            # Новый формат: "заголовок|||текст"
            if "|||" in t:
                prev_title, prev_body = t.split("|||", 1)
                if similar_titles(title, prev_title):
                    log.info(f"[dedup] Совпадение по заголовку: «{title[:40]}» ~ «{prev_title[:40]}»")
                    is_dup = True
                    break
                if similar_titles(item.get("text",""), prev_body):
                    log.info(f"[dedup] Совпадение по тексту: «{title[:40]}»")
                    is_dup = True
                    break
            else:
                if similar_titles(title, t):
                    is_dup = True
                    break
        if is_dup:
            log.info("[dedup] Дубль: «" + item['title'][:60] + "» [" + item.get('source','') + "] " + item.get('link',''))
            continue
        seen_fps.add(f)
        seen_texts.append(title + "|||" + item.get("text","")[:200])
        item["_fp"] = f
        item["_combined"] = title + "|||" + item.get("text","")[:200]
        result.append(item)
    return result


# ── INLINE.RU ──

def fetch_inline_news(news_id: int) -> dict | None:
    for url_tpl in CONFIG["SECTIONS"]:
        url = url_tpl.format(news_id)
        try:
            result = _fetch_inline_url(news_id, url)
        except requests.exceptions.Timeout:
            log.warning(f"[inline] Таймаут NewsID={news_id} — сайт недоступен, прерываем.")
            return "TIMEOUT"  # сигнал что сайт упал
        if result:
            return result
    return None


def inline_site_available() -> bool:
    """Быстрая проверка доступности сайта — один лёгкий запрос."""
    try:
        r = requests.get("http://www.inline.ru/", timeout=(3, 4),
                         headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except Exception:
        return False


def _fetch_inline_url(news_id: int, url: str) -> dict | None:
    try:
        resp = requests.get(url, timeout=(5, 8), headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        resp.encoding = "windows-1251"
        soup = BeautifulSoup(resp.text, "html.parser")

        news_cell = None
        for td in soup.find_all("td"):
            if td.find("a", href=lambda h: h and "for_print" in h):
                news_cell = td
                break
        if not news_cell:
            return None

        SECTION_NAMES = {
            "НОВОСТИ ЭКОНОМИКИ","НОВОСТИ БИЗНЕСА","РЫНКИ","ПОЛИТИКА",
            "СОБЫТИЯ","СПОРТ","ТЕХНОЛОГИИ","МЕДИЦИНА","МИРОВЫЕ НОВОСТИ",
        }
        title = ""
        for b_tag in news_cell.find_all("b"):
            c = b_tag.get_text(strip=True)
            if c.upper() not in SECTION_NAMES and len(c) >= 15:
                title = c
                break
        if not title:
            for line in news_cell.get_text("\n", strip=True).splitlines():
                line = line.strip()
                if line.upper() not in SECTION_NAMES and len(line) >= 15:
                    title = line
                    break

        date_match = re.search(
            r"(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье)"
            r"\s+(\d{1,2})\s+(\S+)\s+(\d{4}),\s*(\d{2}:\d{2})",
            news_cell.get_text()
        )
        pub_date = ""
        pub_dt   = None
        if date_match:
            pub_date = f"{date_match.group(2)} {date_match.group(3)} {date_match.group(4)}, {date_match.group(5)}"
            MONTHS = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
                      "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
            try:
                day = int(date_match.group(2))
                mon = MONTHS.get(date_match.group(3).lower(), 0)
                yr  = int(date_match.group(4))
                hm  = date_match.group(5).split(":")
                pub_dt = datetime(yr, mon, day, int(hm[0]), int(hm[1]),
                                  tzinfo=timezone(timedelta(hours=3)))
            except Exception:
                pass

        cell = BeautifulSoup(str(news_cell), "html.parser")
        for a in cell.find_all("a", href=lambda h: h and "for_print" in h):
            a.decompose()
        for tag in cell(["script", "style"]):
            tag.decompose()
        raw = cell.get_text("\n", strip=True)
        raw = re.sub(
            r"(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье)"
            r"\s+\d{1,2}\s+\S+\s+\d{4},\s*\d{2}:\d{2}", "", raw)
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if lines and lines[0] == title:
            lines = lines[1:]
        body = "\n".join(lines).strip()

        if not title or len(body) < 30:
            return None

        return {
            "source":   "inline",
            "guid":     f"inline:{news_id}",
            "news_id":  news_id,
            "title":    title,
            "text":     body,
            "link":     url,
            "pub_date": pub_date,
            "pub_dt":   pub_dt,
        }
    except requests.exceptions.Timeout:
        raise  # пробрасываем таймаут наверх
    except Exception as e:
        log.warning(f"[inline] Ошибка NewsID={news_id}: {e}")
        return None


def summarize(body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n+", body.strip()) if p.strip()]
    paragraphs = [p for p in paragraphs if len(p) >= 40]
    return "\n\n".join(paragraphs[:CONFIG["SUMMARY_SENTENCES"]])


def detect_inline_latest_id() -> int | None:
    """Находим последний NewsID на главной странице inline.ru."""
    urls = [
        "http://www.inline.ru/",
        "http://www.inline.ru/economi.asp",
        "http://www.inline.ru/polit.asp",
    ]
    max_id = None
    for url in urls:
        try:
            resp = requests.get(url, timeout=(4, 6), headers={"User-Agent": "Mozilla/5.0"})
            resp.encoding = "windows-1251"
            ids = re.findall(r"NewsID=(\d+)", resp.text)
            if ids:
                found = max(int(i) for i in ids)
                if max_id is None or found > max_id:
                    max_id = found
        except ConnectionRefusedError as e:
            log.warning(f"[inline] Сервер отклонил подключение ({url}) — не пробуем другие.")
            return None
        except Exception as e:
            err = str(e)
            # WinError 10061 = connection refused
            if "10061" in err or "Connection refused" in err:
                log.warning(f"[inline] Сервер отклонил подключение — не пробуем другие.")
                return None
            log.warning(f"[inline] Не удалось определить текущий ID с {url}: {e}")
    if max_id:
        log.info(f"[inline] Текущий максимальный NewsID на сайте: {max_id}")
    return max_id


def collect_inline(state: dict) -> list:
    items          = []
    news_id        = state["inline_last_id"] + 1
    last_title     = state.get("inline_last_title", "")
    last_pub_dt    = state.get("inline_last_pub_dt", "")
    no_news_streak = 0
    MAX_NO_NEWS    = 3
    seen_titles    = set()
    max_id_limit   = news_id + 30  # не идём дальше чем +30 от стартового ID

    while news_id <= max_id_limit:
        log.info(f"[inline] Запрашиваю NewsID={news_id}...")
        news = fetch_inline_news(news_id)

        if news == "TIMEOUT":
            log.warning("[inline] Сайт недоступен, прерываем сбор.")
            break

        if not news:
            no_news_streak += 1
            log.info(f"[inline] NewsID={news_id} — нет ({no_news_streak}/{MAX_NO_NEWS}).")
            if no_news_streak >= MAX_NO_NEWS:
                log.info("[inline] Стоп — много пустых подряд.")
                break
            news_id += 1
            continue

        log.info(f"[inline] NewsID={news_id} — найдена: «{news['title'][:50]}» [{news.get('pub_date','')}]")

        # Та же новость что и last_title — дошли до конца, стоп
        if news["title"] == last_title:
            log.info("[inline] Дошли до последней опубликованной, стоп.")
            break

        # Дубль в текущем цикле — считаем как "нет новости"
        if news["title"] in seen_titles:
            no_news_streak += 1
            log.info(f"[inline] NewsID={news_id} — дубль ({no_news_streak}/{MAX_NO_NEWS}).")
            if no_news_streak >= MAX_NO_NEWS:
                log.info("[inline] Стоп — много дублей подряд, пауза 60 мин.")
                state["inline_blocked_until"] = time.time() + 3600
                break
            news_id += 1
            continue

        no_news_streak = 0  # сбрасываем только когда нашли реально новую

        if last_pub_dt and news["pub_dt"]:
            if news["pub_dt"].strftime("%Y%m%d%H%M") < last_pub_dt:
                log.info(f"[inline] NewsID={news_id} — старее, пропускаем «{news['title'][:50]}»")
                seen_titles.add(news["title"])
                news_id += 1
                continue

        if news["title"] == last_title:
            log.info(f"[inline] NewsID={news_id} — дошли до последней, стоп.")
            break

        seen_titles.add(news["title"])
        news["text"] = summarize(news["text"])
        state["inline_last_id"]      = news_id
        state["inline_last_title"]   = news["title"]
        if news["pub_dt"]:
            state["inline_last_pub_dt"] = news["pub_dt"].strftime("%Y%m%d%H%M")
        items.append(news)
        news_id += 1

    log.info(f"[inline] Собрано: {len(items)}")
    return items


# ── THEINS.RU ──

def theins_extract_paragraphs(html: str, n: int = 3) -> str:
    html = re.sub(r"<div[^>]*data-block[^>]*>.*?</div>", "", html, flags=re.DOTALL)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
    result = []
    for p in paras:
        text = re.sub(r"<[^>]+>", "", p).strip()
        text = re.sub(r"[ \t\r\n]+", " ", text)
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
        if len(text) >= 40 and not text.rstrip().endswith(":"):
            result.append(text)
    if not result:
        return ""
    log.info(f"[theins] Абзацев: {len(result)}, берём: {min(n, len(result))}")
    return "\n\n".join(result[:n])


def fetch_theins() -> list:
    try:
        resp = requests.get(CONFIG["THEINS_RSS"], timeout=(10, 20),
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning(f"[theins] Ошибка RSS: {e}")
        return []

    NS = "http://purl.org/rss/1.0/modules/content/"
    items = []
    channel = root.find("channel")
    if not channel:
        return []

    for item in channel.findall("item"):
        def g(tag, _item=item):
            el = _item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title   = g("title")
        link    = g("link")
        guid    = g("guid") or link
        pub_str = g("pubDate")
        pub_dt  = None
        try:
            pub_dt = parsedate_to_datetime(pub_str)
        except Exception:
            pass

        encoded   = item.find(f"{{{NS}}}encoded")
        full_html = (encoded.text or "") if encoded is not None else g("description")

        if not title:
            continue
        items.append({
            "source":    "theins",
            "guid":      guid,
            "title":     title,
            "link":      link,
            "pub_dt":    pub_dt,
            "pub_date":  format_dt(pub_dt),
            "full_html": full_html,
            "text":      "",
        })
    return items


# ── MEDUZA.IO ──

def fetch_meduza() -> list:
    try:
        resp = requests.get(CONFIG["MEDUZA_RSS"], timeout=(10, 20),
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning(f"[meduza] Ошибка RSS: {e}")
        return []

    NS = "http://purl.org/rss/1.0/modules/content/"
    items = []
    for item in root.iter("item"):
        def g(tag, _item=item):
            el = _item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        title   = g("title")
        link    = g("link")
        guid    = g("guid") or link
        pub_str = g("pubDate")
        pub_dt  = None
        try:
            pub_dt = parsedate_to_datetime(pub_str)
        except Exception:
            pass

        encoded = item.find(f"{{{NS}}}encoded")
        raw = (encoded.text if encoded is not None else None) or g("description") or ""

        # Убираем встроенные твиты и blockquote перед парсингом
        raw = re.sub(r"<blockquote[^>]*>.*?</blockquote>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"<div[^>]*twitter[^>]*>.*?</div>", "", raw, flags=re.DOTALL | re.I)

        # Фразы-маркеры шаблонных абзацев Медузы — пропускаем их
        MEDUZA_BOILERPLATE = [
            "с 24 февраля 2022 года",
            "в прямом эфире рассказывает",
            "поделитесь с нами мыслями о войне",
            "форма для обратной связи",
            "обзор предыдущего дня можно прочитать",
            "ежедневно публикуем ваши письма",
        ]

        # Извлекаем только первые 2-3 абзаца из HTML
        paras = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL)
        if paras:
            clean_paras = []
            for p in paras:
                t = re.sub(r"<[^>]+>", " ", p)
                t = re.sub(r"[ \t\r\n]+", " ", t).strip()
                if len(t) < 30 or t.rstrip().endswith(":"):
                    continue
                if any(bp in t.lower() for bp in MEDUZA_BOILERPLATE):
                    continue
                clean_paras.append(t)
                if len(clean_paras) >= 3:
                    break
            text = "\n\n".join(clean_paras)
        else:
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"[ \t\r\n]+", " ", text).strip()
            text = truncate_to_sentence(text, max_len=400)

        if not title:
            continue
        # Фильтруем рекламные/служебные посты Медузы
        MEDUZA_SKIP = [
            "подпишитесь на", "sos-рассылку", "sos рассылку",
            "поддержите медузу", "стать спонсором", "пожертвовать",
            "оформить подписку", "помочь медузе",
            "в прямом эфире рассказывает о российско-украинской войне",
            "поделитесь с нами мыслями о войне",
            "форма для обратной связи — в конце этой статьи",
            "скачайте приложение", "приложение медузы",
            "обходить блокировки и читать",
            "уже в продаже", "в издательстве медузы", "вышла книга",
        ]
        # Дневник войны — заголовок всегда начинается с "Война. Тысяча..."
        is_war_diary = re.match(r"война\.\s+тысяча", title.lower())
        if is_war_diary or any(skip in (title + " " + text).lower() for skip in MEDUZA_SKIP):
            log.info(f"[meduza] Пропускаем служебный пост: «{title[:60]}»")
            continue
        items.append({
            "source":   "meduza",
            "guid":     guid,
            "title":    title,
            "link":     link,
            "pub_dt":   pub_dt,
            "pub_date": format_dt(pub_dt),
            "text":     text,
        })
    return items


def truncate_to_sentence(text: str, max_len: int = 700) -> str:
    """Обрезаем текст по последнему полному предложению."""
    if len(text) <= max_len:
        return text
    chunk = text[:max_len]
    # Ищем последнее завершённое предложение
    best = -1
    for punct in (".", "!", "?", "…"):
        idx = chunk.rfind(punct)
        if idx > max_len // 2 and idx > best:
            best = idx
    if best > 0:
        result = chunk[:best + 1].rstrip()
        # Если после обрезки текст заканчивается на : или , — откатываем ещё раз
        while result and result[-1] in (";", ":", ","):
            for punct in (".", "!", "?", "…"):
                idx = result.rfind(punct)
                if idx > len(result) // 2:
                    result = result[:idx + 1]
                    break
            else:
                break
        return result
    idx = chunk.rfind(" ")
    return (chunk[:idx] + "…") if idx > 0 else chunk


# ── ОТПРАВКА ──

async def send_item(bot: Bot, item: dict, chat_id: str):
    EMOJI     = {"inline": "📰", "theins": "📝", "meduza": "🍊"}
    emoji     = EMOJI.get(item.get("source", ""), "📰")
    date_line = f"\n<i>🕐 {esc(item['pub_date'])}</i>" if item.get("pub_date") else ""
    text_part = "\n\n" + esc(truncate_to_sentence(item["text"])) if item.get("text") else ""
    _link = item.get("link", "")
    link_part = ("\n\n<a href=\"" + esc(_link) + "\">Читать далее →</a>") if _link else ""
    message   = emoji + " <b>" + esc(item["title"]) + "</b>" + date_line + text_part + link_part

    for _ in range(5):
        try:
            await bot.send_message(chat_id=chat_id, text=message,
                                   parse_mode="HTML", disable_web_page_preview=True)
            log.info("✅ [" + item.get("source","?") + "] [" + item.get("pub_date","") + "] «" + item["title"][:60] + "»")
            return
        except TelegramError as e:
            m = re.search(r"Retry.*?(\d+)", str(e))
            if m:
                wait = int(m.group(1)) + 2
                log.warning(f"Flood control, жду {wait} сек...")
                await asyncio.sleep(wait)
            else:
                log.error(f"Ошибка отправки: {e}")
                return


# ── ГЛАВНЫЙ ЦИКЛ ──

async def news_loop(bot: Bot, chat_id: str):
    state     = load_state()
    first_run = True
    log.info("inline_last_id=" + str(state['inline_last_id']) + ", last_pub_dt=" + state.get('last_pub_dt','—'))

    while True:
      try:
        now             = datetime.now(timezone.utc)
        published_ids   = set(state.get("published_ids", []))
        history_fps     = state.get("history_fps", [])
        last_pub_dt_str = state.get("last_pub_dt", "")
        last_pub_dt     = None
        if last_pub_dt_str:
            try:
                last_pub_dt = datetime.fromisoformat(last_pub_dt_str)
                if last_pub_dt.tzinfo is None:
                    last_pub_dt = last_pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        if first_run:
            three_h_ago = now - timedelta(hours=3)
            if last_pub_dt and last_pub_dt >= three_h_ago:
                cutoff = last_pub_dt
                log.info(f"Продолжаем с: {format_dt(last_pub_dt)}")
            else:
                cutoff = three_h_ago
                log.info("Берём последние 3 часа")
        else:
            cutoff = last_pub_dt

        all_items = []

        # Проверяем интернет перед запросами — если нет, ждём и пропускаем цикл
        if not internet_available():
            log.warning("Нет интернета. Жду 2 мин...")
            await asyncio.sleep(120)
            continue

        # Пауза 60 мин если inline.ru недавно отказал (WinError 10061 и т.п.)
        inline_blocked_until = state.get("inline_blocked_until", 0)
        now_ts = time.time()
        if inline_blocked_until and now_ts < inline_blocked_until:
            mins_left = int((inline_blocked_until - now_ts) / 60) + 1
            log.info(f"[inline] Пауза после отказа сервера — ещё {mins_left} мин.")
        else:
            try:
                loop = asyncio.get_event_loop()
                current_max = await loop.run_in_executor(None, detect_inline_latest_id)
                if current_max is None:
                    log.warning("[inline] Сайт недоступен — пауза 60 мин.")
                    state["inline_blocked_until"] = now_ts + 3600
                else:
                    state.pop("inline_blocked_until", None)  # снимаем блок если сайт ответил
                    gap = current_max - state["inline_last_id"]
                    if gap > 50:
                        new_start = max(current_max - 15, state["inline_last_id"])
                        log.info(f"[inline] Разрыв {gap} ID → начинаем с {new_start}")
                        state["inline_last_id"]     = new_start
                        state["inline_last_title"]  = ""
                        state["inline_last_pub_dt"] = ""
                    inline_result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: collect_inline(state)),
                        timeout=120)
                    all_items.extend(inline_result)
            except asyncio.TimeoutError:
                log.warning("[inline] Таймаут 120с — пауза 60 мин.")
                state["inline_blocked_until"] = now_ts + 3600
            except Exception as e:
                log.warning(f"[inline] Недоступен, пауза 60 мин: {e}")
                state["inline_blocked_until"] = now_ts + 3600

        try:
            loop = asyncio.get_event_loop()
            theins_raw = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_theins), timeout=30)
            log.info(f"[theins] Проверяем {len(theins_raw)} новостей...")
            for item in theins_raw:
                if item["guid"] in published_ids:
                    continue
                if cutoff and item["pub_dt"]:
                    c = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
                    dt = item["pub_dt"] if item["pub_dt"].tzinfo else item["pub_dt"].replace(tzinfo=timezone.utc)
                    if dt <= c:
                        continue
                full_html = item.pop("full_html", "")
                summary   = theins_extract_paragraphs(full_html, n=CONFIG["SUMMARY_SENTENCES"])
                if not summary:
                    summary = re.sub(r"[ \t\r\n]+", " ",
                                     re.sub(r"<[^>]+>", " ", full_html)).strip()
                    summary = truncate_to_sentence(summary, max_len=600)
                item["text"] = summary
                all_items.append(item)
            log.info(f"[theins] После фильтрации: {len([i for i in all_items if i.get('source')=='theins'])}")
        except asyncio.TimeoutError:
            log.warning("[theins] Таймаут 30с — пропускаем.")
        except Exception as e:
            log.error(f"[theins] Ошибка: {e}")

        try:
            meduza_raw = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_meduza), timeout=30)
            log.info(f"[meduza] Проверяем {len(meduza_raw)} новостей...")
            for item in meduza_raw:
                if item["guid"] in published_ids:
                    continue
                if cutoff and item["pub_dt"]:
                    c = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
                    dt = item["pub_dt"] if item["pub_dt"].tzinfo else item["pub_dt"].replace(tzinfo=timezone.utc)
                    if dt <= c:
                        continue
                all_items.append(item)
            log.info(f"[meduza] После фильтрации: {len([i for i in all_items if i.get('source')=='meduza'])}")
        except asyncio.TimeoutError:
            log.warning("[meduza] Таймаут 30с — пропускаем.")
        except Exception as e:
            log.error(f"[meduza] Ошибка: {e}")

        log.info(f"Всего собрано: {len(all_items)}")

        unique = deduplicate(all_items, history_fps, state.get("history_texts", []))
        log.info(f"После дедупликации: {len(unique)}")

        if last_pub_dt and not first_run:
            unique = [i for i in unique if i.get("pub_dt") and i["pub_dt"] > last_pub_dt]
            log.info(f"Новее последней публикации: {len(unique)}")

        unique.sort(key=lambda i: i.get("pub_dt") or datetime.min.replace(tzinfo=timezone.utc))

        published = 0
        for item in unique:
            await send_item(bot, item, chat_id)
            published_ids.add(item["guid"])
            if item.get("_fp"):
                history_fps.append(item["_fp"])
            if item.get("pub_dt"):
                new_dt = item["pub_dt"].isoformat()
                if not state["last_pub_dt"] or new_dt > state["last_pub_dt"]:
                    state["last_pub_dt"] = new_dt
            if item.get("_combined"):
                history_texts = state.get("history_texts", [])
                # Сохраняем "заголовок|||текст" — чтобы сравнивать заголовки отдельно
                history_texts.append(item["title"] + "|||" + item.get("text","")[:200])
                state["history_texts"] = history_texts[-CONFIG["HISTORY_SIZE"]:]
            state["published_ids"] = list(published_ids)[-CONFIG["HISTORY_SIZE"] * 3:]
            state["history_fps"]   = history_fps[-CONFIG["HISTORY_SIZE"]:]
            save_state(state)
            published += 1
            await asyncio.sleep(3)

        log.info(f"Опубликовано: {published}" if published else "Новых новостей нет.")
        first_run = False

        wait = CONFIG["POLL_INTERVAL"]
        log.info(f"⏳ Жду {wait // 60} мин...")
        await asyncio.sleep(wait)
      except Exception as e:
        err = str(e)
        if any(x in err for x in ["NetworkError", "TimedOut", "ConnectError", "getaddrinfo"]):
            log.warning(f"[loop] Сетевая ошибка: {e}. Жду 60 сек...")
            await asyncio.sleep(60)
        else:
            log.error(f"[loop] Необработанная ошибка: {e}. Жду 30 сек...")
            await asyncio.sleep(30)


async def main():
    log.info("▶ Запуск агрегатора (inline.ru + theins.ru + meduza.io)")
    creds   = load_credentials()
    from telegram.request import HTTPXRequest
    bot = Bot(
        token=creds["BOT_TOKEN"],
        request=HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30),
    )
    chat_id = str(creds["CHAT_ID"])
    try:
        me = await bot.get_me()
        log.info(f"Бот авторизован: @{me.username}")
    except TelegramError as e:
        log.error(f"Не удалось подключиться: {e}")
        return
    await news_loop(bot, chat_id)


if __name__ == "__main__":
    import time as _time
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            log.error(f"⚠️ Бот упал: {e}. Перезапуск через 30 сек...")
            _time.sleep(30)
