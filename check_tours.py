import datetime as dt
import sys
import time

import requests

from telegram_alert import send_message, should_send_error_alert

# ht.kz's own tour-search widget (used on https://ht.kz/tours/vietnam-from-almaty) calls this
# JSON endpoint directly - found by inspecting the page's network traffic. It needs no auth/session,
# so we can call it straight with `requests` instead of driving a headless browser.
LOAD_TOURS_URL = "https://ht.kz/sellPage/loadTours"
TOUR_VIEW_URL = "https://ht.kz/tour/view"

COUNTRY_ID = 7  # Vietnam
DEPART_CITY_ID = 1  # Almaty
REGION_ID = 63  # Nha Trang (Нячанг)

DEPARTURE_WINDOW_START = dt.date(2026, 8, 1)
DEPARTURE_WINDOW_END = dt.date(2026, 8, 15)
DAYS_MIN = 6
DAYS_MAX = 9

PRICE_GREAT_DEAL = 700_000
PRICE_THRESHOLD = 800_000  # top of the acceptable 700k-800k range for 2 adults

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; vietnam-price-monitor/1.0)"}
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


def build_link(tour: dict) -> str:
    return f"{TOUR_VIEW_URL}?from=salesPage&forceDates=1&tour={tour['id']}"


def format_alert(tour: dict) -> str:
    price = tour["price"]["forTour"]
    hotel = tour["hotel"]["realName"]
    region = tour["region"]["name"]
    depart = tour["departDay"]
    ret = tour["returnDay"]
    days = tour["days"]
    nights = tour["nights"]
    link = build_link(tour)

    if price <= PRICE_GREAT_DEAL:
        verdict = f"отличная цена, дешевле нижней границы допуска на {PRICE_GREAT_DEAL - price:,.0f} тг"
    else:
        verdict = f"в пределах допуска, дешевле верхней границы на {PRICE_THRESHOLD - price:,.0f} тг"

    return (
        "🏝 Найден тур Алматы → Нячанг дешевле лимита\n"
        f"Отель: {hotel} ({region})\n"
        f"Вылет: {depart}, обратно: {ret} ({days} дней / {nights} ночей)\n"
        f"Цена за двоих: {price:,.0f} тг ({verdict})\n"
        f"Ссылка: {link}"
    )


def main() -> int:
    all_matching = []
    checked_days = 0
    day = DEPARTURE_WINDOW_START

    try:
        while day <= DEPARTURE_WINDOW_END:
            tours = fetch_tours_for_date(day)
            checked_days += 1
            matching = [t for t in tours if matches_duration(t) and t.get("tourist", {}).get("adults") == 2]
            all_matching.extend(matching)
            print(f"[check_tours] {day}: {len(tours)} tours returned, {len(matching)} match {DAYS_MIN}-{DAYS_MAX} days")
            day += dt.timedelta(days=1)
            time.sleep(REQUEST_DELAY_SECONDS)

        if not all_matching:
            print("[check_tours] no matching tours found in the target window at all")
            return 0

        best = min(all_matching, key=lambda t: t["price"]["forTour"])
        print(
            f"[check_tours] cheapest found: {best['price']['forTour']} KZT for 2, "
            f"{best['hotel']['realName']}, depart {best['departDay']}"
        )

        if best["price"]["forTour"] <= PRICE_THRESHOLD:
            send_message(format_alert(best))
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
