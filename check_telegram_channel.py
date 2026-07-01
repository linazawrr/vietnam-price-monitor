import datetime as dt
import html as html_module
import re
import sys
import time

import requests

from telegram_alert import send_message, should_send_error_alert

# ht.kz's own Telegram channel (public, no login needed - readable via the t.me/s/<channel> web
# preview) regularly posts one-way fare roundups tagged with a country flag per section, e.g. a
# "🇻🇳 Вьетнам" block listing "Алматы — Нячанг" / "Нячанг — Алматы" one-way prices with dates.
# These are one-way prices for 1 adult, so we pair up an outbound + return date ourselves to get
# a comparable round-trip total.
CHANNEL = "HT_kz"
CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
POSTS_TO_SCAN = 40  # how far back to page through channel history each run
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vietnam-price-monitor/1.0)"}
REQUEST_TIMEOUT = 15

YEAR = 2026
DEPARTURE_WINDOW_START = dt.date(YEAR, 8, 1)
DEPARTURE_WINDOW_END = dt.date(YEAR, 8, 15)
TRIP_DURATIONS = (6, 7)
PRICE_THRESHOLD_PER_PERSON = 320_000

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

DATE_PRICE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s*[—-]\s*([\d\s]{4,})\s*₸")


def fetch_posts() -> list[tuple[str, str]]:
    """Returns a list of (post_id, plain_text) tuples, most recent first."""
    posts = []
    before = None
    for _ in range(max(1, POSTS_TO_SCAN // 20 + 1)):
        url = CHANNEL_URL if before is None else f"{CHANNEL_URL}?before={before}"
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        page = resp.text

        ids = [int(m) for m in re.findall(rf'data-post="{CHANNEL}/(\d+)"', page)]
        if not ids:
            break

        for post_id in ids:
            block_start = page.find(f'data-post="{CHANNEL}/{post_id}"')
            text_match = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_footer|Please open Telegram)',
                page[block_start:block_start + 20000],
                re.S,
            )
            if not text_match:
                continue
            plain = re.sub(r"<[^>]+>", "", text_match.group(1))
            plain = html_module.unescape(plain)
            posts.append((str(post_id), plain))

        if len(posts) >= POSTS_TO_SCAN:
            break
        before = min(ids)
        time.sleep(0.5)

    return posts[:POSTS_TO_SCAN]


def extract_vietnam_section(text: str) -> str | None:
    if "Вьетнам" not in text or "Нячанг" not in text:
        return None
    idx = text.find("Вьетнам")
    rest = text[idx:]
    end = rest.find("⸻")
    return rest[:end] if end != -1 else rest


def parse_route_prices(section: str, route_label: str) -> dict[dt.date, int]:
    """route_label e.g. 'Алматы — Нячанг' or 'Нячанг — Алматы'."""
    idx = section.find(route_label)
    if idx == -1:
        return {}
    chunk = section[idx + len(route_label):]
    # stop at the next route header (another "X — Y" line) if present
    next_route = re.search(r"[А-Яа-яё]+\s*[—-]\s*[А-Яа-яё]+", chunk)
    if next_route:
        chunk = chunk[:next_route.start()]

    prices = {}
    for day_str, month_str, price_str in DATE_PRICE_RE.findall(chunk):
        month = MONTHS[month_str]
        try:
            date = dt.date(YEAR, month, int(day_str))
        except ValueError:
            continue
        price = int(price_str.replace(" ", ""))
        prices[date] = price
    return prices


def find_all_roundtrips(posts: list[tuple[str, str]]) -> list:
    candidates = []
    for post_id, text in posts:
        section = extract_vietnam_section(text)
        if not section:
            continue
        outbound = parse_route_prices(section, "Алматы — Нячанг")
        inbound = parse_route_prices(section, "Нячанг — Алматы")
        for dep_date, dep_price in outbound.items():
            if not (DEPARTURE_WINDOW_START <= dep_date <= DEPARTURE_WINDOW_END):
                continue
            for duration in TRIP_DURATIONS:
                ret_date = dep_date + dt.timedelta(days=duration)
                if ret_date in inbound:
                    candidates.append({
                        "post_id": post_id,
                        "depart": dep_date,
                        "return": ret_date,
                        "price_per_person": dep_price + inbound[ret_date],
                    })
    return candidates


def find_best_roundtrip(posts: list[tuple[str, str]]):
    candidates = find_all_roundtrips(posts)
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["price_per_person"])


def format_digest_entry(rank: int, deal: dict) -> str:
    return (
        f"{rank}. 📅 {deal['depart'].isoformat()} → {deal['return'].isoformat()}\n"
        f"💵 {deal['price_per_person']:,.0f} тг/чел\n"
        f"🔗 https://t.me/{CHANNEL}/{deal['post_id']}"
    )


def list_top_telegram(n: int = 5) -> str:
    posts = fetch_posts()
    candidates = find_all_roundtrips(posts)
    if not candidates:
        return f"📢 Канал HT.KZ: подходящих комбинаций билетов не найдено (просканировано {len(posts)} постов)."

    top = sorted(candidates, key=lambda c: c["price_per_person"])[:n]
    entries = [format_digest_entry(i, c) for i, c in enumerate(top, start=1)]
    header = f"📢 Канал HT.KZ — топ {len(top)} комбинаций билетов (1-15 августа, 6-7 дней)"
    return header + "\n\n" + "\n\n".join(entries)


def format_alert(deal: dict) -> str:
    diff = PRICE_THRESHOLD_PER_PERSON - deal["price_per_person"]
    lines = [
        "✈️ Найден авиабилет (Telegram-канал HT.KZ) дешевле лимита",
        f"📅 Вылет {deal['depart'].isoformat()} → обратно {deal['return'].isoformat()}",
        f"💵 За человека: {deal['price_per_person']:,.0f} тг (дешевле лимита на {diff:,.0f} тг)",
        "🧳 По собственному описанию канала: багаж и питание включены",
        "ℹ️ Это комбинация двух билетов в одну сторону из одного поста, не единый билет туда-обратно — уточни стыковку у менеджера",
        f"🔗 https://t.me/{CHANNEL}/{deal['post_id']}",
    ]
    return "\n\n".join(lines)


def main() -> int:
    try:
        posts = fetch_posts()
        print(f"[check_telegram_channel] scanned {len(posts)} recent posts from @{CHANNEL}")

        best = find_best_roundtrip(posts)
        if not best:
            print("[check_telegram_channel] no matching Vietnam round-trip combo found in scanned posts")
            return 0

        print(
            f"[check_telegram_channel] cheapest combo: {best['price_per_person']:,.0f} KZT/person, "
            f"depart {best['depart']}, return {best['return']} (post {best['post_id']})"
        )

        if best["price_per_person"] <= PRICE_THRESHOLD_PER_PERSON:
            send_message(format_alert(best))
        else:
            print(
                f"[check_telegram_channel] cheapest combo {best['price_per_person']:,.0f} KZT/person "
                f"is above threshold {PRICE_THRESHOLD_PER_PERSON}, no alert"
            )
        return 0

    except Exception as exc:
        print(f"[check_telegram_channel] error during check: {exc}")
        if should_send_error_alert("telegram_channel"):
            send_message(f"⚠️ Мониторинг Telegram-канала HT.KZ сломался: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
