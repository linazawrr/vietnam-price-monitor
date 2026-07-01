import datetime as dt
import sys
import time
import uuid

import requests

from telegram_alert import send_message, should_send_error_alert

# ht.kz's own tour-search widget (used on https://ht.kz/tours/vietnam-from-almaty) calls this
# JSON endpoint directly - found by inspecting the page's network traffic. It needs no auth/session,
# so we can call it straight with `requests` instead of driving a headless browser. It's fast but
# always prices for 2 adults (the "hot tours" quick widget).
LOAD_TOURS_URL = "https://ht.kz/sellPage/loadTours"
TOUR_VIEW_URL = "https://ht.kz/tour/view"

# ht.kz's full search engine (what /findtours actually uses under the hood). It's slower - you POST
# a query, get a session id back, then poll until it's done - but it supports searching for a single
# adult, and can be pinned to one exact hotel/date/duration via the `hotels` filter.
SEARCH_URL = "https://ws.ht.kz/v1/search/web"
SEARCH_POLL_ATTEMPTS = 8
SEARCH_POLL_DELAY_SECONDS = 2

COUNTRY_ID = 7  # Vietnam
DEPART_CITY_ID = 1  # Almaty
REGION_ID = 63  # Nha Trang (Нячанг)

DEPARTURE_WINDOW_START = dt.date(2026, 8, 1)
DEPARTURE_WINDOW_END = dt.date(2026, 8, 15)
DAYS_MIN = 6
DAYS_MAX = 9
STARS_MIN = 3  # hotel.class below this ('2', 'APT', etc.) is filtered out

PRICE_GREAT_DEAL = 700_000
PRICE_THRESHOLD = 800_000  # top of the acceptable 700k-800k range for 2 adults

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vietnam-price-monitor/1.0)", "Accept": "application/json"}
REQUEST_TIMEOUT = 20
REQUEST_DELAY_SECONDS = 1


def fetch_tours_for_date(day: dt.date) -> list:
    params = {
        "countryId": COUNTRY_ID,
        "departCityId": DEPART_CITY_ID,
        "regionId": REGION_ID,
        "date": day.isoformat(),
    }
    resp = requests.get(LOAD_TOURS_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def matches_duration(tour: dict) -> bool:
    return DAYS_MIN <= tour.get("days", 0) <= DAYS_MAX


def hotel_stars(tour: dict) -> int | None:
    raw = tour.get("hotel", {}).get("class")
    return int(raw) if raw and raw.isdigit() else None


def matches_stars(tour: dict) -> bool:
    stars = hotel_stars(tour)
    return stars is not None and stars >= STARS_MIN


def build_link(tour: dict) -> str:
    return f"{TOUR_VIEW_URL}?from=salesPage&forceDates=1&tour={tour['id']}"


def find_solo_price(tour: dict) -> int | None:
    """Search ht.kz's full engine for the same hotel/dates/duration priced for 1 adult."""
    hotel_id = int(tour["hotel"]["id"])
    day = tour["departDay"]
    nights = tour["nights"]

    body = {
        "type": 0,
        "ukey": uuid.uuid4().hex,
        "bkey": uuid.uuid4().hex,
        "params": {
            "adults": 1,
            "childAges": [],
            "countryId": COUNTRY_ID,
            "dateFrom": day,
            "dateTo": day,
            "departCityId": DEPART_CITY_ID,
            "groupMode": 1,
            "nightsFrom": nights,
            "nightsTo": nights,
            "onlyHotels": False,
            "currency": "kzt",
            "regions": [REGION_ID],
            "hotels": [hotel_id],
        },
    }
    resp = requests.post(SEARCH_URL, json=body, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    session_id = resp.json()["id"]

    poll_url = f"{SEARCH_URL}/{session_id}"
    poll_params = {"id": session_id, "page": 1, "size": 20, "sort": "price", "currency": "kzt"}
    for _ in range(SEARCH_POLL_ATTEMPTS):
        r = requests.get(poll_url, params=poll_params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("searchIsDone"):
            tours = data.get("tours", [])
            if not tours:
                return None
            return min(t["price"]["value"] for t in tours)
        time.sleep(SEARCH_POLL_DELAY_SECONDS)

    print("[check_tours] solo-price search timed out before completion")
    return None


def format_alert(tour: dict, solo_price: int | None) -> str:
    price = tour["price"]["forTour"]
    hotel = tour["hotel"]["realName"]
    region = tour["region"]["name"]
    depart = tour["departDay"]
    ret = tour["returnDay"]
    days = tour["days"]
    nights = tour["nights"]
    stars = hotel_stars(tour) or 0
    link = build_link(tour)

    if price <= PRICE_GREAT_DEAL:
        verdict = f"дешевле нижней границы допуска на {PRICE_GREAT_DEAL - price:,.0f} тг"
    else:
        verdict = f"дешевле верхней границы допуска на {PRICE_THRESHOLD - price:,.0f} тг"

    lines = [
        "🌴 Найден тур Алматы → Нячанг дешевле лимита",
        f"🏨 {hotel}, {region}",
        f"⭐ {'⭐' * (stars - 1)} ({stars} звезды)",
        f"📅 Вылет {depart} → обратно {ret} ({days} дней / {nights} ночей)",
        f"💵 За двоих (вы с мужем): {price:,.0f} тг — {verdict}",
    ]
    if solo_price is not None:
        lines.append(f"💵 За одного (для Рифата): {solo_price:,.0f} тг")
    else:
        lines.append("💵 За одного: не удалось найти отдельную цену, уточни на сайте")
    lines.append(f"🔗 {link}")

    return "\n".join(lines)


def main() -> int:
    all_matching = []
    checked_days = 0
    day = DEPARTURE_WINDOW_START

    try:
        while day <= DEPARTURE_WINDOW_END:
            tours = fetch_tours_for_date(day)
            checked_days += 1
            matching = [
                t for t in tours
                if matches_duration(t) and matches_stars(t) and t.get("tourist", {}).get("adults") == 2
            ]
            all_matching.extend(matching)
            print(
                f"[check_tours] {day}: {len(tours)} tours returned, {len(matching)} match "
                f"{DAYS_MIN}-{DAYS_MAX} days and {STARS_MIN}+ stars"
            )
            day += dt.timedelta(days=1)
            time.sleep(REQUEST_DELAY_SECONDS)

        if not all_matching:
            print("[check_tours] no matching tours found in the target window at all")
            return 0

        best = min(all_matching, key=lambda t: t["price"]["forTour"])
        print(
            f"[check_tours] cheapest found: {best['price']['forTour']} KZT for 2, "
            f"{best['hotel']['realName']} ({hotel_stars(best)}*), depart {best['departDay']}"
        )

        if best["price"]["forTour"] <= PRICE_THRESHOLD:
            solo_price = None
            try:
                solo_price = find_solo_price(best)
                print(f"[check_tours] solo price for the same hotel/dates: {solo_price}")
            except Exception as exc:
                print(f"[check_tours] failed to fetch solo price: {exc}")
            send_message(format_alert(best, solo_price))
        else:
            print(f"[check_tours] cheapest price {best['price']['forTour']} KZT is above threshold {PRICE_THRESHOLD}, no alert")

        return 0

    except Exception as exc:
        print(f"[check_tours] error during tour check (checked {checked_days} days before failing): {exc}")
        if should_send_error_alert("tours"):
            send_message(f"⚠️ Мониторинг туров ht.kz сломался: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
