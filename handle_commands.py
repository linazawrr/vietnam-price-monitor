import json
import os
import sys
import urllib.parse
import urllib.request

import check_charter_flights
import check_flights
import check_telegram_channel
import check_tours
from telegram_alert import send_message

OFFSET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "telegram_offset.json")

COMMANDS = {
    "/tours": lambda: check_tours.list_top_tours(5),
    "/flights": lambda: check_flights.list_top_flights(5),
    "/charter": lambda: check_charter_flights.list_top_charter(5),
    "/tg": lambda: check_telegram_channel.list_top_telegram(5),
}


def _load_offset() -> int:
    if not os.path.exists(OFFSET_PATH):
        return 0
    try:
        with open(OFFSET_PATH, encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def get_updates(token: str, offset: int) -> list:
    params = {"offset": offset, "timeout": 0}
    url = f"https://api.telegram.org/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data}")
    return data.get("result", [])


def normalize_command(text: str) -> str:
    first_word = text.strip().split()[0] if text.strip() else ""
    return first_word.split("@")[0].lower()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not allowed_chat_id:
        print("[handle_commands] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping")
        return 0

    offset = _load_offset()
    try:
        updates = get_updates(token, offset)
    except Exception as exc:
        print(f"[handle_commands] failed to fetch updates: {exc}")
        return 0

    if not updates:
        print("[handle_commands] no new updates")
        return 0

    max_update_id = offset - 1
    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        message = update.get("message")
        if not message:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != str(allowed_chat_id):
            print(f"[handle_commands] ignoring message from unrecognized chat_id={chat_id}")
            continue

        command = normalize_command(message.get("text", ""))
        handler = COMMANDS.get(command)
        if not handler:
            continue

        print(f"[handle_commands] running command {command}")
        try:
            reply = handler()
        except Exception as exc:
            reply = f"⚠️ Ошибка при выполнении {command}: {exc}"
            print(f"[handle_commands] handler error for {command}: {exc}")
        send_message(reply)

    _save_offset(max_update_id + 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
