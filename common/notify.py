"""The Telegram pipe. Every watcher sends through here."""

import os
import time
import urllib.error
import urllib.parse
import urllib.request

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4096


def _chunks(text, limit=TG_LIMIT):
    """Split on line boundaries so an HTML tag is never broken mid-message."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single long line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(text, dry=False):
    if dry:
        print("---- TELEGRAM (dry) ----\n" + text + "\n------------------------")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        # Hard-fail rather than silently swallow: keeps the filing from being
        # marked "seen", so it re-alerts once the secrets are configured.
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    for chunk in _chunks(text, TG_LIMIT):
        payload = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(TG_API.format(token=token, method="sendMessage"), data=payload)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except urllib.error.HTTPError as e:
            print(f"Telegram error {e.code}: {e.read().decode('utf-8','replace')}")
            raise
        time.sleep(0.3)
