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
DEPARTURE_WINDOW_END = dt.date(2026, 8, 15)
TRIP_DURATIONS = (6, 7)
PRICE_THRESHOLD_PER_PERSON = 320_000
PASSENGERS = 2

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SEARCH_MONTH = "2026-08"


def _call_api(token: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
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
        "direct": "false",
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
                "direct": "false",
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
    return duration in TRIP_DURATIONS


def build_booking_link(flight: dict) -> str:
    link = flight.get("link")
    if link:
        return f"https://www.aviasales.com{link}"
    return (
        f"https://www.aviasales.com/search/{ORIGIN}"
        f"{flight['departure_at'][8:10]}{flight['departure_at'][5:7]}{DESTINATION}1"
    )


def format_alert(flight: dict) -> str:
    price = flight["price"]
    diff = PRICE_THRESHOLD_PER_PERSON - price
    departure = flight["departure_at"][:10]
    return_date = flight["return_at"][:10]
    link = build_booking_link(flight)
    return (
        "✈️ Найден дешёвый авиабилет Алматы → Нячанг\n"
        f"Вылет: {departure}, обратно: {return_date}\n"
        f"Цена: {price:,.0f} {CURRENCY} на человека (дешевле лимита на {diff:,.0f} {CURRENCY})\n"
        f"На {PASSENGERS} взрослых: ~{price * PASSENGERS:,.0f} {CURRENCY}\n"
        "⚠️ Цена может быть без учёта багажа — уточни на сайте перед покупкой\n"
        f"Ссылка: {link}"
    )


def main() -> int:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("[check_flights] TRAVELPAYOUTS_TOKEN not set, skipping flight check")
        return 0

    try:
        candidates = fetch_month_matrix(token)
        matching = [f for f in candidates if matches_window(f)]
        print(f"[check_flights] month-matrix returned {len(candidates)} flights, {len(matching)} within our date window")

        if not matching:
            matching = fetch_exact_date_pairs(token)
            print(f"[check_flights] fallback query found {len(matching)} flights within our date window")

        if not matching:
            print("[check_flights] no flights found in the target date window at all")
            return 0

        best = min(matching, key=lambda f: f["price"])
        print(f"[check_flights] cheapest found: {best['price']} {CURRENCY} on {best['departure_at']} / {best['return_at']}")

        if best["price"] <= PRICE_THRESHOLD_PER_PERSON:
            send_message(format_alert(best))
        else:
            print(f"[check_flights] cheapest price {best['price']} {CURRENCY} is above threshold {PRICE_THRESHOLD_PER_PERSON}, no alert")

        return 0

    except Exception as exc:
        print(f"[check_flights] error during flight check: {exc}")
        if should_send_error_alert("flights"):
            send_message(f"⚠️ Мониторинг авиабилетов сломался: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
