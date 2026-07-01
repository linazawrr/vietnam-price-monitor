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
# Occasionally it also posts an already-combined round trip line, e.g.
# "Алматы — Нячанг — Алматы  17 апреля — 24 апреля — 216 000 ₸" - a single real fare, which we
# prefer over self-pairing two one-way legs. "Камрань" (Cam Ranh airport) is used interchangeably
# with "Нячанг" (Nha Trang) for this route - same CXR airport, just a different resort-area name.
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

DESTINATION_NAMES = ("Нячанг", "Камрань")
DEST_ALT = "|".join(DESTINATION_NAMES)

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
MONTH_ALT = "|".join(MONTHS)

DATE_PRICE_RE = re.compile(rf"(\d{{1,2}})\s+({MONTH_ALT})\s*[—-]\s*([\d\s]{{4,}})\s*₸")
ROUNDTRIP_LINE_RE = re.compile(
    rf"(\d{{1,2}})\s+({MONTH_ALT})\s*[—-]\s*(\d{{1,2}})\s+({MONTH_ALT})\s*[—-]\s*([\d\s]{{4,}})\s*₸"
)
ROUNDTRIP_HEADER_RE = re.compile(rf"Алматы\s*[—-]\s*(?:{DEST_ALT})\s*[—-]\s*Алматы")
OUTBOUND_HEADER_RE = re.compile(rf"Алматы\s*[—-]\s*(?:{DEST_ALT})(?!\s*[—-]\s*Алматы)")
INBOUND_HEADER_RE = re.compile(rf"(?:{DEST_ALT})\s*[—-]\s*Алматы")


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
    if "Вьетнам" not in text or not any(name in text for name in DESTINATION_NAMES):
        return None
    idx = text.find("Вьетнам")
    rest = text[idx:]
    end = rest.find("⸻")
    return rest[:end] if end != -1 else rest


def _parse_date(day_str: str, month_str: str) -> dt.date | None:
    try:
        return dt.date(YEAR, MONTHS[month_str], int(day_str))
    except ValueError:
        return None


def parse_route_prices(section: str, direction: str) -> dict[dt.date, int]:
    """direction: 'outbound' (Алматы -> Нячанг/Камрань) or 'inbound' (Нячанг/Камрань -> Алматы)."""
    header_re = OUTBOUND_HEADER_RE if direction == "outbound" else INBOUND_HEADER_RE
    m = header_re.search(section)
    if not m:
        return {}
    chunk = section[m.end():]
    # stop at the next route header (another "X — Y" line) if present
    next_route = re.search(r"[А-Яа-яё]+\s*[—-]\s*[А-Яа-яё]+", chunk)
    if next_route:
        chunk = chunk[:next_route.start()]

    prices = {}
    for day_str, month_str, price_str in DATE_PRICE_RE.findall(chunk):
        date = _parse_date(day_str, month_str)
        if date is None:
            continue
        prices[date] = int(price_str.replace(" ", ""))
    return prices


def parse_roundtrip_combos(section: str) -> list[dict]:
    """Already-combined round-trip lines, e.g. 'Алматы — Нячанг — Алматы  17 апреля — 24 апреля — 216 000 ₸'."""
    m = ROUNDTRIP_HEADER_RE.search(section)
    if not m:
        return []
    chunk = section[m.end():]
    next_route = re.search(r"[А-Яа-яё]+\s*[—-]\s*[А-Яа-яё]+", chunk)
    if next_route:
        chunk = chunk[:next_route.start()]

    combos = []
    for day1, month1, day2, month2, price_str in ROUNDTRIP_LINE_RE.findall(chunk):
        dep_date = _parse_date(day1, month1)
        ret_date = _parse_date(day2, month2)
        if dep_date is None or ret_date is None:
            continue
        combos.append({"depart": dep_date, "return": ret_date, "price": int(price_str.replace(" ", ""))})
    return combos


def find_all_roundtrips(posts: list[tuple[str, str]]) -> list:
    candidates = []
    for post_id, text in posts:
        section = extract_vietnam_section(text)
        if not section:
            continue

        for combo in parse_roundtrip_combos(section):
            dep_date, ret_date = combo["depart"], combo["return"]
            if not (DEPARTURE_WINDOW_START <= dep_date <= DEPARTURE_WINDOW_END):
                continue
            if (ret_date - dep_date).days not in TRIP_DURATIONS:
                continue
            candidates.append({
                "post_id": post_id,
                "depart": dep_date,
                "return": ret_date,
                "price_per_person": combo["price"],
                "source": "combined",
            })

        outbound = parse_route_prices(section, "outbound")
        inbound = parse_route_prices(section, "inbound")
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
                        "source": "paired",
                    })
    return candidates


def find_best_roundtrip(posts: list[tuple[str, str]]):
    candidates = find_all_roundtrips(posts)
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["price_per_person"])


def format_digest_entry(rank: int, deal: dict) -> str:
    tag = "туда-обратно" if deal["source"] == "combined" else "комбинация из 2 билетов"
    return (
        f"{rank}. 📅 {deal['depart'].isoformat()} → {deal['return'].isoformat()} ({tag})\n"
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
    ]
    if deal["source"] == "combined":
        lines.append("ℹ️ Это единый тариф туда-обратно, как указан в посте")
    else:
        lines.append("ℹ️ Это комбинация двух билетов в одну сторону из одного поста, не единый билет туда-обратно — уточни стыковку у менеджера")
    lines.append(f"🔗 https://t.me/{CHANNEL}/{deal['post_id']}")
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
