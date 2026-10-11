"""
Ежедневный дайджест клубов финансовых директоров.
Каждый день проверяет клубы по очереди и публикует в канал сводку по первому клубу,
у которого есть новости, и добавляет её на страницу сайта docs/clubs.html.
Сводку готовит Claude, читая открытые страницы клуба.

Секреты (GitHub → Settings → Secrets → Actions):
  ANTHROPIC_API_KEY  — ключ Claude API
  TG_BOT_TOKEN       — токен Telegram-бота
  TG_CHAT_ID         — канал, например @kurs_valut_igor
Необязательные настройки (там же, вкладка Variables):
  MONTHLY_BUDGET_USD — лимит расходов на месяц, по умолчанию 5
  MAX_CHECKS         — сколько клубов максимум проверять за день, по умолчанию 3
  CLAUDE_MODEL       — модель, по умолчанию claude-haiku-5-5
"""
import html
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
DOCS = ROOT / "docs"
HISTORY = DOCS / "clubs_history.json"
SITE_URL = os.getenv("SITE_URL", "")
CLUBS = json.loads((ROOT / "clubs.json").read_text(encoding="utf-8"))

MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"
BUDGET = float(os.getenv("MONTHLY_BUDGET_USD") or 5)
MAX_CHECKS = int(os.getenv("MAX_CHECKS") or 3)
# Цены Claude Haiku 5.5 за 1 млн токенов (до 100K в запросе) и за 1 веб-поиск, в долларах
PRICE_IN = float(os.getenv("PRICE_IN") or 0.10)
PRICE_OUT = float(os.getenv("PRICE_OUT") or 0.50)
PRICE_SEARCH = 0.01


# ---------- состояние ----------
def load_state():
    s = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    s.setdefault("next", 0)
    s.setdefault("clubs", {})
    month = date.today().strftime("%Y-%m")
    if s.get("month") != month:
        s.update(month=month, spent_usd=0.0, budget_notified=False)
    return s


def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Claude ----------
def call_claude(messages):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 4000,
            "messages": messages,
            "tools": [
                {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4,
                 "max_content_tokens": 15000},
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 1},
            ],
        },
        timeout=300,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:500]}")
    return r.json()


def cost_of(resp):
    u = resp.get("usage", {})
    tokens_in = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0))
    searches = (u.get("server_tool_use") or {}).get("web_search_requests", 0)
    return tokens_in / 1e6 * PRICE_IN + u.get("output_tokens", 0) / 1e6 * PRICE_OUT + searches * PRICE_SEARCH


def build_prompt(club, info):
    since = info.get("last_post") or (date.today() - timedelta(days=30)).isoformat()
    reported = "; ".join(info.get("reported", [])[-30:]) or "—"
    return f"""Ты — аналитик, который готовит короткие дайджесты для финансового директора. Сегодня {date.today().isoformat()}.

Клуб: {club['name']} ({club['region']}; {club['type']}).
Официальные страницы: {', '.join(club['urls'])}
Поисковый запрос, если нужен: {club['search']}

Задача:
1. Открой страницы клуба (web_fetch). Если на них мало информации — сделай один поиск (web_search).
2. Найди только НОВОЕ, появившееся после {since}: что клуб запустил, какой продукт, программу, формат или исследование добавил, какие мероприятия прошли или анонсированы.
3. Не повторяй уже опубликованное ранее: {reported}
4. Ничего не выдумывай. Включай только то, что прямо видно в источниках, со ссылками. Если новостей нет или дату нельзя подтвердить — has_news = false.

Ответь ТОЛЬКО JSON между тегами <json> и </json>:
{{"has_news": true или false, "items": ["короткий заголовок каждой новости"], "digest": "текст для Telegram"}}

Требования к digest: на русском языке, не длиннее 2500 символов, разметка только <b>, <i>, <a href="...">. Структура:
<b>🏛 {club['name']}</b> · {club['region']}

<b>🆕 Что нового</b> — 2–4 пункта: что запустили, какой продукт или формат добавили
<b>💡 Цепляющее и полезное для CFO</b> — 1–3 пункта: почему это важно и что можно применить у себя
<b>📅 События</b> — ближайшие мероприятия с датами (пропусти блок, если их нет)
<b>🔗 Источники</b> — ссылки"""


def ask_about(club, info):
    messages = [{"role": "user", "content": build_prompt(club, info)}]
    total = 0.0
    for _ in range(4):  # продолжение, если серверные инструменты поставили паузу
        resp = call_claude(messages)
        total += cost_of(resp)
        if resp.get("stop_reason") != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp["content"]})
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return parse_answer(text), total


def clean(text):
    """Оставляет только безопасные теги <b>, <i> и ссылки http(s); всё остальное экранирует."""
    t = html.escape(text, quote=True)
    t = re.sub(r"&lt;(/?)(b|i)&gt;", r"<\1\2>", t)
    t = re.sub(r"&lt;a href=&quot;(https?://.+?)&quot;&gt;", r'<a href="\1">', t)
    t = t.replace("&lt;/a&gt;", "</a>")
    if t.count("<a href=") != t.count("</a>"):  # непарные ссылки — убираем теги ссылок
        t = re.sub(r'<a href="[^"]*">|</a>', "", t)
    return t


def parse_answer(text):
    m = re.search(r"<json>(.*?)</json>", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        data = json.loads(raw)
    except Exception:
        print("Не удалось разобрать ответ:", text[:500])
        return {"has_news": False}
    return data if isinstance(data, dict) else {"has_news": False}


# ---------- Telegram ----------
def send(text):
    token, chat = os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"]
    for part in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, data={"chat_id": chat, "text": part, "parse_mode": "HTML",
                                     "disable_web_page_preview": "true"}, timeout=30)
        if r.status_code == 400:  # ошибка разметки — отправляем простым текстом
            plain = re.sub(r"<[^>]+>", "", part)
            r = requests.post(url, data={"chat_id": chat, "text": plain}, timeout=30)
        print(r.text[:300])
        r.raise_for_status()


# ---------- страница сайта ----------
def update_site(club, digest):
    DOCS.mkdir(exist_ok=True)
    hist = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    hist.insert(0, {"date": date.today().strftime("%d.%m.%Y"), "club": club["name"], "html": digest})
    hist = hist[:60]
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    cards = "\n".join(
        f'<article class="card"><div class="meta">{h["date"]}</div>{h["html"].replace(chr(10), "<br>")}</article>'
        for h in hist)
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Новости клубов CFO</title>
<style>
:root {{ --bg:#f6f5f1; --card:#fff; --ink:#1c1c1a; --muted:#6b6a64; --line:#e4e2da; --link:#2f5aa8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --card:#1e1e1c; --ink:#ecebe6; --muted:#9a998f; --line:#33322e; --link:#7aa2ec; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:32px 16px 48px; }}
h1 {{ font-size:28px; margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 24px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; margin-bottom:16px; overflow-wrap:anywhere; }}
.meta {{ color:var(--muted); font-size:13px; margin-bottom:6px; }}
a {{ color:var(--link); }}
</style></head><body><main>
<h1>Новости клубов финансовых директоров</h1>
<p class="sub">Что запустили, что нового и что полезного для CFO · обновляется ежедневно · <a href="./">← курсы валют</a></p>
{cards}
</main></body></html>"""
    (DOCS / "clubs.html").write_text(page, encoding="utf-8")


# ---------- главный цикл ----------
def main():
    s = load_state()
    n = len(CLUBS)
    for k in range(min(MAX_CHECKS, n)):
        if s["spent_usd"] >= BUDGET:  # в публичный канал об этом не пишем — только в журнал запуска
            print(f"Месячный лимит ${BUDGET} исчерпан, до 1-го числа дайджест на паузе")
            break
        idx = (s["next"] + k) % n
        club = CLUBS[idx]
        info = s["clubs"].setdefault(club["name"], {})
        print(f"Проверяю: {club['name']}")
        data, cost = ask_about(club, info)
        s["spent_usd"] = round(s["spent_usd"] + cost, 4)
        print(f"  стоимость ${cost:.4f}, за месяц ${s['spent_usd']:.4f}")
        save_state(s)
        if data.get("has_news") and data.get("digest"):
            body = clean(data["digest"])
            text = body
            if SITE_URL:
                text += f'\n\n🌐 <a href="{SITE_URL}clubs.html">Все новости клубов на сайте</a>'
            send(text)
            update_site(club, body)
            info["reported"] = (info.get("reported", []) + list(data.get("items", [])))[-40:]
            info["last_post"] = date.today().isoformat()
            s["next"] = (idx + 1) % n
            save_state(s)
            return
        print("  новостей нет")
    # ни у одного из проверенных клубов новостей нет — завтра начнём со следующих
    s["next"] = (s["next"] + min(MAX_CHECKS, n)) % n
    save_state(s)


if __name__ == "__main__":
    main()
