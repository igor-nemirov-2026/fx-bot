python
"""
Ежедневная сводка: курсы НБУ (USD, EUR) + динамика за 30 дней + анекдот.
Публикует пост с графиком в Telegram-канал и страницу сайта docs/index.html.
"""
import json
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
CURRENCIES = ["USD", "EUR"]
DAYS = 30
NBU = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"

JOKES = [
    "— Доктор, я всё время думаю о курсе доллара.\n— Ну и как, растёт?\n— Нет, доктор, только тревога.",
    "Экономист — это человек, который завтра точно объяснит, почему не сбылся его вчерашний прогноз.",
    "Купил доллары по курсу 41. Теперь у меня не сбережения, а воспоминания.",
    "— Почему ты держишь деньги в банке из-под огурцов?\n— Там хоть процент соли гарантирован.",
    "Программист пишет скрипт за 5 минут, чтобы не делать вручную работу на 30 секунд. И он прав.",
]


def nbu_rate(code, d):
    r = requests.get(NBU, params={"valcode": code, "date": d.strftime("%Y%m%d"), "json": ""}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return float(data[0]["rate"]) if data else None


def fetch_history(code):
    out = []
    today = date.today()
    for i in range(DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        rate = nbu_rate(code, d)
        if rate is not None:
            out.append((d.isoformat(), rate))
    return out


def fetch_joke():
    try:
        r = requests.get("http://rzhunemogu.ru/RandJSON.aspx?CType=1", timeout=10)
        text = r.content.decode("cp1251")
        joke = text.split('"content":"', 1)[1].rsplit('"', 1)[0].strip()
        if joke:
            return joke
    except Exception:
        pass
    return random.choice(JOKES)


def demo_history(base):
    today, rate, out = date.today(), base, []
    for i in range(DAYS - 1, -1, -1):
        rate += random.uniform(-0.12, 0.13)
        out.append(((today - timedelta(days=i)).isoformat(), round(rate, 4)))
    return out


def make_chart(hist, path):
    fig, axes = plt.subplots(len(hist), 1, figsize=(9, 6), sharex=True)
    colors = {"USD": "#2a7a4b", "EUR": "#2f5aa8"}
    for ax, (code, rows) in zip(axes, hist.items()):
        xs = [d[5:] for d, _ in rows]
        ys = [v for _, v in rows]
        ax.plot(xs, ys, color=colors.get(code), linewidth=2)
        ax.fill_between(xs, ys, min(ys), color=colors.get(code), alpha=0.08)
        ax.set_title(f"{code}/UAH за {DAYS} дней", loc="left", fontsize=11)
        ax.grid(alpha=0.25)
        ax.spines[["top", "right"]].set_visible(False)
    axes[-1].set_xticks(xs[:: max(1, len(xs) // 8)])
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def summary(hist):
    s = {}
    for code, rows in hist.items():
        today_rate = rows[-1][1]
        prev = rows[-2][1] if len(rows) > 1 else today_rate
        month_ago = rows[0][1]
        s[code] = {
            "today": today_rate,
            "day_change": today_rate - prev,
            "month_change_pct": (today_rate / month_ago - 1) * 100,
        }
    return s


def arrow(x):
    return "🔺" if x > 0 else ("🔻" if x < 0 else "▪️")


def telegram_caption(s, joke, site_url):
    lines = [f"💱 <b>Курсы НБУ на {date.today():%d.%m.%Y}</b>", ""]
    flags = {"USD": "🇺🇸", "EUR": "🇪🇺"}
    for code, v in s.items():
        lines.append(
            f"{flags.get(code, '')} {code}: <b>{v['today']:.4f}</b> ₴ "
            f"{arrow(v['day_change'])} {v['day_change']:+.4f} за день, "
            f"{v['month_change_pct']:+.2f}% за месяц"
        )
    lines += ["", "😄 <b>Анекдот дня</b>", joke]
    if site_url:
        lines += ["", f'🌐 <a href="{site_url}">Подробнее на сайте</a>']
    return "\n".join(lines)[:1020]


def post_telegram(caption, chart):
    token, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        print("Telegram: токен/канал не заданы — пропускаю")
        return
    with open(chart, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f},
            timeout=30,
        )
    print(r.text)
    r.raise_for_status()
    print("Telegram: опубликовано")


def build_site(hist, s, joke):
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    html = (
        template.replace("__DATE__", f"{date.today():%d.%m.%Y}")
        .replace("__DATA__", json.dumps({"hist": hist, "summary": s}, ensure_ascii=False))
        .replace("__JOKE__", joke.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>"))
    )
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    print("Сайт: docs/index.html обновлён")


def main():
    demo = "--demo" in sys.argv
    DOCS.mkdir(exist_ok=True)
    if demo:
        hist = {"USD": demo_history(41.2), "EUR": demo_history(47.9)}
        joke = random.choice(JOKES)
    else:
        hist = {c: fetch_history(c) for c in CURRENCIES}
        joke = fetch_joke()
    s = summary(hist)
    chart = DOCS / "chart.png"
    make_chart(hist, chart)
    build_site(hist, s, joke)
    caption = telegram_caption(s, joke, os.getenv("SITE_URL", ""))
    print(caption)
    if not demo:
        post_telegram(caption, chart)


if __name__ == "__main__":
    main()
