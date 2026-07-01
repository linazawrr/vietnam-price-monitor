import json
import os
import time
import urllib.request
import urllib.parse

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "last_alerts.json")
ERROR_ALERT_MIN_INTERVAL_SECONDS = 20 * 60 * 60  # ~20h, keeps errors to at most once/day


def send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram_alert] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping send")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            ok = resp.status == 200
            print(f"[telegram_alert] sendMessage status={resp.status}")
            return ok
    except Exception as exc:
        print(f"[telegram_alert] failed to send message: {exc}")
        return False


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def should_send_error_alert(source: str) -> bool:
    """Throttle error alerts per source to at most once every ~20h. Records the attempt regardless."""
    state = _load_state()
    key = f"{source}_last_error_alert"
    now = time.time()
    last = state.get(key, 0)
    if now - last < ERROR_ALERT_MIN_INTERVAL_SECONDS:
        return False
    state[key] = now
    _save_state(state)
    return True
