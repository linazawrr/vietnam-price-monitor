import datetime as dt
import sys
import time

from playwright.sync_api import sync_playwright

from telegram_alert import send_message, should_send_error_alert

# ht.kz's charter-flight search only renders on mobile, and it's a mobile page - so it needs a real
# (headless) browser rather than a plain HTTP request. Under the hood the page calls a third-party
# search API (Bronix) that requires an API key baked into ht.kz's own frontend bundle; rather than
# lifting that key and calling Bronix directly ourselves, we drive the actual ht.kz page and read
# whatever it fetches - the same access pattern a real visitor gets.
PASSENGERS = 3
SEARCH_URL_TEMPLATE = (
    "https://ht.kz/new/search/avia?departure=ALA&destination=CXR&dateFrom={dep}&dateTo={ret}"
    f"&adults={PASSENGERS}&children=0&childAges=&flyType=&from=aviaForm"
)

DEPARTURE_WINDOW_START = dt.date(2026, 8, 1)
DEPARTURE_WINDOW_END = dt.date(2026, 8, 15)
TRIP_DURATIONS = (5, 6)

PRICE_THRESHOLD_PER_PERSON = 330_000  # reference only - the actual gate is the rounded total below
PRICE_THRESHOLD_TOTAL = 1_000_000  # for PASSENGERS people, rounded up from 3 x 330k = 990k

POLL_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 2


def is_ideal_return_date(return_date: dt.date) -> bool:
    """Weekend arrival back in Almaty (Saturday/Sunday) - a nice-to-have, not a filter."""
    return return_date.weekday() >= 5


def search_one_date(page, day: dt.date, duration: int) -> list:
    return_day = day + dt.timedelta(days=duration)
    url = SEARCH_URL_TEMPLATE.format(dep=day.isoformat(), ret=return_day.isoformat())

    captured = {}

    def on_response(response):
        if "bronix.com/flights-search/v1/search/" in response.url:
            try:
                data = response.json()
            except Exception:
                return
            if data.get("status") == "completed":
                captured["data"] = data

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        waited = 0
        while "data" not in captured and waited < POLL_TIMEOUT_SECONDS:
            page.wait_for_timeout(POLL_INTERVAL_SECONDS * 1000)
            waited += POLL_INTERVAL_SECONDS
    finally:
        page.remove_listener("response", on_response)

    if "data" not in captured:
        print(f"[check_charter_flights] {day}: search did not complete within {POLL_TIMEOUT_SECONDS}s")
        return []

    return captured["data"].get("results", {}).get("itineraries", [])


def format_itinerary(day: dt.date, return_day: dt.date, itinerary: dict) -> dict:
    total_price = itinerary["price"]["totalPrice"]["amount"]
    price_per_person = total_price / PASSENGERS
    outbound, inbound = itinerary["legs"][0], itinerary["legs"][1]
    baggage = outbound["segments"][0].get("baggage", {})
    airline = outbound["segments"][0]["airline"]["displayName"]
    return {
        "depart": day.isoformat(),
        "return": return_day.isoformat(),
        "total_price": total_price,
        "price_per_person": price_per_person,
        "airline": airline,
        "baggage_kg": baggage.get("weight"),
        "outbound_time": outbound["departureTime"],
        "inbound_time": inbound["departureTime"],
    }


def format_alert(deal: dict) -> str:
    diff = PRICE_THRESHOLD_TOTAL - deal["total_price"]
    baggage = f"{deal['baggage_kg']} кг багажа включено" if deal["baggage_kg"] else "багаж не указан"
    return_date = dt.date.fromisoformat(deal["return"])
    lines = [
        "✈️ Найден чартерный билет ht.kz Алматы → Нячанг дешевле лимита",
        f"🛫 {deal['airline']}",
        f"📅 Вылет {deal['depart']} → обратно {deal['return']}",
        f"💵 За человека: {deal['price_per_person']:,.0f} тг",
        f"💵 На {PASSENGERS} человек: {deal['total_price']:,.0f} тг (дешевле лимита на {diff:,.0f} тг)",
        f"🧳 {baggage}",
    ]
    if is_ideal_return_date(return_date):
        lines.append("🎯 Идеальные даты — прилёт в Алматы в выходной")
    lines.append(
        f"🔗 https://ht.kz/new/search/avia?departure=ALA&destination=CXR&dateFrom={deal['depart']}"
        f"&dateTo={deal['return']}&adults={PASSENGERS}&children=0&childAges=&flyType=&from=aviaForm"
    )
    return "\n\n".join(lines)


def gather_all_deals() -> list:
    all_deals = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["iPhone 13"])
        page = context.new_page()

        day = DEPARTURE_WINDOW_START
        while day <= DEPARTURE_WINDOW_END:
            for duration in TRIP_DURATIONS:
                return_day = day + dt.timedelta(days=duration)
                itineraries = search_one_date(page, day, duration)
                print(f"[check_charter_flights] {day} (+{duration}d): {len(itineraries)} itineraries found")
                for it in itineraries:
                    all_deals.append(format_itinerary(day, return_day, it))
            day += dt.timedelta(days=1)

        context.close()
        browser.close()
    return all_deals


def format_digest_entry(rank: int, deal: dict) -> str:
    baggage = f"{deal['baggage_kg']} кг" if deal["baggage_kg"] else "не указан"
    ideal = " 🎯" if is_ideal_return_date(dt.date.fromisoformat(deal["return"])) else ""
    return (
        f"{rank}. 🛫 {deal['airline']}\n"
        f"📅 {deal['depart']} → {deal['return']}{ideal}\n"
        f"💵 {deal['price_per_person']:,.0f} тг/чел | На {PASSENGERS}: {deal['total_price']:,.0f} тг | 🧳 {baggage}"
    )


def list_top_charter(n: int = 5) -> str:
    all_deals = gather_all_deals()
    if not all_deals:
        return "✈️ Чартеры HT.KZ: ничего не найдено в текущем окне дат."

    top = sorted(all_deals, key=lambda d: d["price_per_person"])[:n]
    entries = [format_digest_entry(i, d) for i, d in enumerate(top, start=1)]
    header = f"✈️ Чартерные билеты HT.KZ — топ {len(top)} (5-6 дней, 1-15 августа, 🎯 = выходной прилёт)"
    return header + "\n\n" + "\n\n".join(entries)


def main() -> int:
    try:
        all_deals = gather_all_deals()

        if not all_deals:
            print("[check_charter_flights] no charter itineraries found in the target window at all")
            return 0

        best = min(all_deals, key=lambda d: d["price_per_person"])
        print(
            f"[check_charter_flights] cheapest found: {best['price_per_person']:,.0f} KZT/person, "
            f"{best['total_price']:,.0f} for {PASSENGERS}, {best['airline']}, depart {best['depart']}"
        )

        if best["total_price"] <= PRICE_THRESHOLD_TOTAL:
            send_message(format_alert(best))
        else:
            print(
                f"[check_charter_flights] cheapest total {best['total_price']:,.0f} KZT "
                f"is above threshold {PRICE_THRESHOLD_TOTAL}, no alert"
            )

        return 0

    except Exception as exc:
        print(f"[check_charter_flights] error during check: {exc}")
        if should_send_error_alert("charter_flights"):
            send_message(f"⚠️ Мониторинг чартерных билетов ht.kz сломался: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
