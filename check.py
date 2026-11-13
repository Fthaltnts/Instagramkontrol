#!/usr/bin/env python3
"""Scheduled Instagram public/private status check; no media collection.

Data retrieval uses Instaloader's public profile metadata, subject to Instagram's
access restrictions and terms. Never interprets an access failure as 'private'.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

STATE_PATH = Path(__file__).resolve().parent / "state.json"
USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
BOT_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
CHAT_ID_RE = re.compile(r"-?[0-9]+\Z")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def read_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def write_state(value: dict[str, Any]) -> None:
    # Serialized state contains no Instagram username, token, or Telegram chat ID.
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def notify(token: str, chat_id: str, text: str) -> bool:
    data = urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        return result.get("ok") is True
    except HTTPError as exc:
        print(f"Telegram mesajı gönderilemedi (HTTP {exc.code}).")
    except (URLError, TimeoutError, OSError, ValueError):
        print("Telegram mesajı gönderilemedi (bağlantı veya yanıt hatası).")
    return False


def fetch_status(username: str) -> str:
    import instaloader

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=1,
        request_timeout=20,
        quiet=True,
    )
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        return "private" if profile.is_private else "public"
    finally:
        loader.close()


def perform_check(
    username: str,
    token: str,
    chat_id: str,
    observed: str | None,
    state: dict[str, Any],
    now: datetime,
    sender: Callable[[str, str, str], bool],
) -> tuple[dict[str, Any], bool]:
    """Pure-ish state transition core; caller persists returned state.

    observed=None means unavailable or blocked, never 'private'.
    Returns (new state, send attempt successful / no notification needed).
    """
    next_state = dict(state)
    identity = hmac.new(token.encode(), username.encode(), hashlib.sha256).hexdigest()
    if next_state.get("identity") != identity:
        next_state = {"version": 1, "identity": identity, "status": None, "errors": 0}

    # Weekly heartbeat also keeps public scheduled GitHub repos active.
    heartbeat = parse_stamp(next_state.get("heartbeat_utc"))
    if heartbeat is None or now - heartbeat >= timedelta(days=7):
        next_state["heartbeat_utc"] = stamp(now)

    if observed not in ("public", "private", None):
        raise ValueError("Geçersiz gözlem")

    if observed is None:
        count = min(3, int(next_state.get("errors", 0)) + 1)
        next_state["errors"] = count
        previous_alert = parse_stamp(next_state.get("last_error_alert_utc"))
        if count >= 3 and (previous_alert is None or now - previous_alert >= timedelta(hours=12)):
            text = ("⚠️ Instagram durum kontrolü üst üste başarısız oldu. "
                    "Açık/gizli durumu bilinmiyor. Instagram otomatik erişimi engelliyor olabilir.")
            if sender(token, chat_id, text):
                next_state["last_error_alert_utc"] = stamp(now)
                print("Tekrarlanan erişim hatası için uyarı gönderildi.")
            else:
                print("Tekrarlanan erişim hatası uyarısı gönderilemedi.")
                return next_state, False
        return next_state, True

    next_state["errors"] = 0
    next_state.pop("last_error_alert_utc", None)
    prev = next_state.get("status")
    if prev == observed:
        print("Sonuç: Değişiklik yok.")
        return next_state, True

    label = "HERKESE AÇIK ✅" if observed == "public" else "GİZLİ 🔒"
    prefix = "İlk güvenilir tespit" if prev not in ("public", "private") else "Durum değişti"
    msg = f"Instagram: {prefix}\n@{username} → {label}\nhttps://www.instagram.com/{username}/"
    if not sender(token, chat_id, msg):
        print("Telegram bildirim başarısız: durum güncellenmedi, sonra tekrar denenecek.")
        return next_state, False
    next_state["status"] = observed
    next_state["last_change_utc"] = stamp(now)
    print("Telegram durum bildirimi gönderildi.")
    return next_state, True


def main() -> int:
    username = os.getenv("TARGET_USERNAME", "").strip().lstrip("@").lower()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not USERNAME_RE.fullmatch(username):
        print("Eksik veya geçersiz TARGET_USERNAME (GitHub Actions secret).")
        return 1
    if not BOT_TOKEN_RE.fullmatch(token) or not CHAT_ID_RE.fullmatch(chat_id):
        print("Telegram bot tokenı veya chat ID eksik/geçersiz (GitHub Actions secrets).")
        return 1

    state = read_state()
    observed: str | None = None
    try:
        observed = fetch_status(username)
        print(f"Instagram profil durumu okunabildi: {observed}.")
    except Exception as exc:
        # An Instagram 403 / 429 / login challenge, or other error, is NOT 'private'.
        print(f"Instagram durumu BELİRSİZ ({type(exc).__name__}); gizli sayılmıyor.")
    updated, success = perform_check(username, token, chat_id, observed, state, utcnow(), notify)
    if updated != state:
        write_state(updated)
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
