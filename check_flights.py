import datetime as dt
import os
import sys
import time
import urllib.request
import urllib.parse
import json

from telegram_alert import send_message, should_send_error_alert

ORIGIN = "ALA"
DESTINATION = "CXR"  # Cam Ranh airport, serves Nha Trang
CURRENCY = "KZT"
DEPARTURE_WINDOW_START = dt.date(2026, 8, 1)
DEPARTURE_WINDOW_END = dt.date(2026, 8, 20)
TRIP_DURATIONS = (5, 6, 8, 9)  # 5-6 = short trip, 8-9 = weekend-bridge trip (bracket a workweek with weekends)
PASSENGERS = 3
PRICE_THRESHOLD_PER_PERSON = 330_000  # reference only - the actual gate is the rounded total below
PRICE_THRESHOLD_TOTAL = 1_000_000  # for PASSENGERS people, rounded up from 3 x 330k = 990k

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SEARCH_MONTH = "2026-08"


def is_ideal_return_date(return_date: dt.date) -> bool:
    """Weekend arrival back in Almaty (Saturday/Sunday) - a nice-to-have, not a filter."""
    return return_date.weekday() >= 5


def is_weekend_bridge(departure: dt.date, return_date: dt.date) -> bool:
    """Both ends fall Fri/Sat/Sun - lets a Mon-Fri vacation week be bracketed by two free weekends."""
    return departure.weekday() >= 4 and return_date.weekday() >= 4


def _call_api(token: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
        return json.loads(body)


def fetch_month_matrix(token: str) -> list:
    params = {
        "origin": ORIGIN,
        "destination": DESTINATION,
        "currency": CURRENCY,
        "departure_at": SEARCH_MONTH,
        "return_at": SEARCH_MONTH,
        "one_way": "false",
        "direct": "true",
        "sorting": "price",
        "limit": 1000,
        "page": 1,
        "token": token,
    }
    result = _call_api(token, params)
    if not result.get("success"):
        print(f"[check_flights] month-matrix query failed: {result}")
        return []
    return result.get("data", [])


def fetch_exact_date_pairs(token: str) -> list:
    """Fallback: query each candidate departure/return date pair individually."""
    found = []
    day = DEPARTURE_WINDOW_START
    checked = 0
    while day <= DEPARTURE_WINDOW_END:
        for duration in TRIP_DURATIONS:
            return_day = day + dt.timedelta(days=duration)
            params = {
                "origin": ORIGIN,
                "destination": DESTINATION,
                "currency": CURRENCY,
                "departure_at": day.isoformat(),
                "return_at": return_day.isoformat(),
                "one_way": "false",
                "direct": "true",
                "sorting": "price",
                "limit": 1,
                "page": 1,
                "token": token,
            }
            checked += 1
            try:
                result = _call_api(token, params)
                if result.get("success") and result.get("data"):
                    found.extend(result["data"])
            except Exception as exc:
                print(f"[check_flights] exact-date query failed for {day}/{return_day}: {exc}")
            time.sleep(1.5)
        day += dt.timedelta(days=1)
    print(f"[check_flights] fallback: checked {checked} exact date-pair combinations")
    return found


def matches_window(flight: dict) -> bool:
    try:
        departure = dt.datetime.fromisoformat(flight["departure_at"].replace("Z", "+00:00")).date()
        return_date = dt.datetime.fromisoformat(flight["return_at"].replace("Z", "+00:00")).date()
    except (KeyError, ValueError):
        return False
    if not (DEPARTURE_WINDOW_START <= departure <= DEPARTURE_WINDOW_END):
        return False
    duration = (return_date - departure).days
    if duration not in TRIP_DURATIONS:
        return False
    # belt-and-suspenders on top of the direct=true query param
    if flight.get("transfers", 0) or flight.get("return_transfers", 0):
        return False
    return True


def build_booking_link(flight: dict) -> str:
    # Aviasales' own cached "link" is generated for 1 passenger and can't be changed to
    # PASSENGERS after the fact; our own fallback link at least searches for the right count.
    return (
        f"https://www.aviasales.com/search/{ORIGIN}"
        f"{flight['departure_at'][8:10]}{flight['departure_at'][5:7]}{DESTINATION}{PASSENGERS}"
    )


def format_alert(flight: dict) -> str:
    price = flight["price"]
    total = price * PASSENGERS
    diff = PRICE_THRESHOLD_TOTAL - total
    departure = flight["departure_at"][:10]
    return_date_str = flight["return_at"][:10]
    return_date = dt.date.fromisoformat(return_date_str)
    link = build_booking_link(flight)
    lines = [
        "✈️ Найден авиабилет Алматы → Нячанг дешевле лимита",
        f"📅 Вылет {departure} → обратно {return_date_str}",
        f"💵 За человека: {price:,.0f} {CURRENCY}",
        f"💵 На {PASSENGERS} человек: {total:,.0f} {CURRENCY} (дешевле лимита на {diff:,.0f} {CURRENCY})",
        "⚠️ Цена может быть без учёта багажа — уточни на сайте перед покупкой",
        f"🔗 {link}",
    ]
    if is_ideal_return_date(return_date):
        lines.append("🎯 Идеальные даты — прилёт в Алматы в выходной")
    if is_weekend_bridge(dt.date.fromisoformat(departure), return_date):
        lines.append("🌉 Мостик через выходные — вылет и прилёт в пятницу-воскресенье")
    lines.append(f"ℹ️ Ссылка уже настроена на поиск для {PASSENGERS} человек")
    return "\n\n".join(lines)


def gather_matching_flights(token: str) -> list:
    candidates = fetch_month_matrix(token)
    matching = [f for f in candidates if matches_window(f)]
    print(f"[check_flights] month-matrix returned {len(candidates)} flights, {len(matching)} within our date window")

    if not matching:
        matching = fetch_exact_date_pairs(token)
        print(f"[check_flights] fallback query found {len(matching)} flights within our date window")

    return matching


def format_digest_entry(rank: int, flight: dict) -> str:
    departure_str = flight["departure_at"][:10]
    return_date_str = flight["return_at"][:10]
    departure = dt.date.fromisoformat(departure_str)
    return_date = dt.date.fromisoformat(return_date_str)
    price = flight["price"]
    tags = ""
    if is_ideal_return_date(return_date):
        tags += " 🎯"
    if is_weekend_bridge(departure, return_date):
        tags += " 🌉"
    return (
        f"{rank}. 📅 {departure_str} → {return_date_str}{tags}\n"
        f"💵 {price:,.0f} {CURRENCY}/чел | На {PASSENGERS}: {price * PASSENGERS:,.0f} {CURRENCY}\n"
        f"🔗 {build_booking_link(flight)}"
    )


def list_top_flights(n: int = 5) -> str:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return "✈️ Авиабилеты Aviasales: TRAVELPAYOUTS_TOKEN не настроен, проверка недоступна."

    matching = gather_matching_flights(token)
    if not matching:
        return "✈️ Авиабилеты Aviasales: ничего не найдено в текущем окне дат."

    top = sorted(matching, key=lambda f: f["price"])[:n]
    entries = [format_digest_entry(i, f) for i, f in enumerate(top, start=1)]
    header = (
        f"✈️ Авиабилеты Aviasales — топ {len(top)} (1-20 августа, 5-6/8-9 дней, прямые, "
        "🎯 = выходной прилёт, 🌉 = мостик через выходные)"
    )
    return header + "\n\n" + "\n\n".join(entries)


def main() -> int:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("[check_flights] TRAVELPAYOUTS_TOKEN not set, skipping flight check")
        return 0

    try:
        matching = gather_matching_flights(token)

        if not matching:
            print("[check_flights] no flights found in the target date window at all")
            return 0

        best = min(matching, key=lambda f: f["price"])
        total = best["price"] * PASSENGERS
        print(
            f"[check_flights] cheapest found: {best['price']} {CURRENCY}/person "
            f"({total:,.0f} for {PASSENGERS}) on {best['departure_at']} / {best['return_at']}"
        )

        if total <= PRICE_THRESHOLD_TOTAL:
            send_message(format_alert(best))
        else:
            print(f"[check_flights] cheapest total {total:,.0f} {CURRENCY} is above threshold {PRICE_THRESHOLD_TOTAL}, no alert")

        return 0

    except Exception as exc:
        print(f"[check_flights] error during flight check: {exc}")
        if should_send_error_alert("flights"):
            send_message(f"⚠️ Мониторинг авиабилетов сломался: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
