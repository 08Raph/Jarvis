"""Well-known websites Jarvis can open in the default browser when asked ("open Instagram"), and the rules for turning what a person said into a safe address.

Only ``https://`` addresses are ever produced. A bare word is looked up in the table below; something that looks like a domain (``example.com``) becomes ``https://example.com``;
anything else is not treated as a website. Opening a page is handed to Windows (the default browser): Jarvis does not read the page or sign in to anything."""
from __future__ import annotations

import re
from urllib.parse import quote_plus

SITES: dict[str, str] = {
    "instagram": "https://www.instagram.com/", "youtube": "https://www.youtube.com/", "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/", "x": "https://x.com/", "tiktok": "https://www.tiktok.com/", "reddit": "https://www.reddit.com/",
    "netflix": "https://www.netflix.com/", "twitch": "https://www.twitch.tv/", "discord": "https://discord.com/app",
    "spotify": "https://open.spotify.com/", "gmail": "https://mail.google.com/", "google": "https://www.google.com/",
    "google maps": "https://maps.google.com/", "maps": "https://maps.google.com/", "github": "https://github.com/",
    "amazon": "https://www.amazon.com/", "whatsapp": "https://web.whatsapp.com/", "snapchat": "https://www.snapchat.com/",
    "pinterest": "https://www.pinterest.com/", "linkedin": "https://www.linkedin.com/", "wikipedia": "https://www.wikipedia.org/",
    "disney plus": "https://www.disneyplus.com/", "disney+": "https://www.disneyplus.com/", "hulu": "https://www.hulu.com/",
    "prime video": "https://www.primevideo.com/", "steam store": "https://store.steampowered.com/",
    "epic games store": "https://store.epicgames.com/", "fortnite tracker": "https://fortnitetracker.com/", "chatgpt": "https://chatgpt.com/",
    "outlook": "https://outlook.live.com/", "onedrive": "https://onedrive.live.com/", "google drive": "https://drive.google.com/",
    "google docs": "https://docs.google.com/", "paypal": "https://www.paypal.com/", "ebay": "https://www.ebay.com/", "weather": "https://weather.com/",
    "news": "https://news.google.com/", "translate": "https://translate.google.com/",
}
_DOMAIN = re.compile(r"^(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|net|org|io|gg|tv|co|ca|uk|app|dev|me|us|edu|gov|info|xyz|ai|fm|ly|be|de|fr|es|it|jp|au|nz))(?:/[\w\-./?=&%#+]*)?$", re.I)


def https_url_for(text: str) -> tuple[str, str] | None:
    """(display name, https address) for what the person said, or None. Never returns a non-https address."""
    t = re.sub(r"\s+", " ", str(text or "").strip().lower()).strip(".!? ")
    t = re.sub(r"\s+(?:website|site|app|dot com)$", "", t).strip()
    if t in SITES:
        return t, SITES[t]
    m = _DOMAIN.match(t.replace(" dot ", "."))
    if m and len(t) <= 120:
        url = "https://" + t.split("://", 1)[-1]
        return m.group(1), url
    return None


def search_url(query: str, engine: str = "google") -> str | None:
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q or len(q) > 200:
        return None
    if engine == "youtube":
        return "https://www.youtube.com/results?search_query=" + quote_plus(q)
    return "https://www.google.com/search?q=" + quote_plus(q)


def is_https(url: str) -> bool:
    return str(url or "").lower().startswith("https://") and not any(c in url for c in ("\n", "\r", "\x00", " ", '"', "'", "|"))
