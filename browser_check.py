#!/usr/bin/env python3
"""No-login Instagram *web-page* visibility check.

Only detects visible private notices or a public post grid.
Never attempts to access private posts, log in, or fetch media.
When Instagram shows a login wall, a block, or an empty/unknown page,
returns UNKNOWN instead of guessing a status.
"""
from __future__ import annotations

import re

import check  # Keep existing Telegram messaging and state-transition logic.
from playwright.sync_api import sync_playwright

PRIVATE_NOTICE = re.compile(
    r"\bthis account is private\b|\bbu hesap gizli(?:dir)?\b",
    re.IGNORECASE,
)


def classify_visible_page(text: str, media_link_count: int) -> str:
    """Pure detection rule that is safe to run on incomplete HTML."""
    private_notice = bool(PRIVATE_NOTICE.search(text))
    if private_notice and media_link_count:
        raise RuntimeError("Çelişkili Instagram sayfası: özel hesap uyarısı ve gönderi bağlantıları bir arada")
    if private_notice:
        return "private"
    if media_link_count > 0:
        return "public"
    raise RuntimeError("Girişsiz sayfada açık/gizli durumu doğrulayacak işaret yok")


def fetch_visible_status(username: str) -> str:
    url = f"https://www.instagram.com/{username}/"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="en-US",
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Let the page load its profile content normally; do not dismiss log-in walls.
            page.wait_for_timeout(3500)

            code = response.status if response else None
            if code is None or code >= 400:
                raise RuntimeError(f"Instagram web yanıtı uygun değil: HTTP {code}")
            if not page.url.rstrip("/").endswith("instagram.com/" + username):
                raise RuntimeError("Instagram giriş veya başka sayfaya yönlendirdi")

            body = page.locator("body").inner_text(timeout=10000)
            # A public post/reel visible in the profile's main feed is positive evidence.
            link_count = page.locator(
                'main a[href*="/p/"], main a[href*="/reel/"]'
            ).count()
            print(f"Instagram web: HTTP {code}; görünür gönderi bağlantısı {link_count}; "
                  f"gizli uyarısı {bool(PRIVATE_NOTICE.search(body))}.")
            return classify_visible_page(body, link_count)
        finally:
            browser.close()


# The existing check.main() uses this function for status; keep all
# previously configured Secrets, state.json, and Telegram logic unchanged.
check.fetch_status = fetch_visible_status

if __name__ == "__main__":
    raise SystemExit(check.main())
