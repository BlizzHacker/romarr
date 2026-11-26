"""Discover time-limited giveaways and send players to the storefront."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from threading import Lock
from time import monotonic

import requests

EPIC_GIVEAWAYS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
CLAIM_PAGES = (
    {"provider": "Amazon Luna", "url": "https://luna.amazon.com/claims/home"},
    {"provider": "Epic Games", "url": "https://store.epicgames.com/en-US/free-games"},
    {"provider": "GOG", "url": "https://www.gog.com/"},
    {"provider": "Steam", "url": "https://store.steampowered.com/search/?specials=1"},
)
MAX_FEED_BYTES = 2 * 1024 * 1024


def _instant(value) -> datetime | None:
    """Read an aware timestamp from Epic's promotions feed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def epic_offers(payload: dict, *, now: datetime) -> list[dict]:
    """Return active, originally paid base games offered at zero price."""
    catalog = payload["data"]["Catalog"]["searchStore"]["elements"]
    games = {}
    for offer in catalog:
        price = (offer.get("price") or {}).get("totalPrice") or {}
        if (offer.get("offerType") != "BASE_GAME" or price.get("discountPrice") != 0
                or not isinstance(price.get("originalPrice"), int)
                or price["originalPrice"] <= 0):
            continue
        promotions = (offer.get("promotions") or {}).get("promotionalOffers") or []
        ends = []
        for group in promotions:
            for window in group.get("promotionalOffers") or []:
                start, end = _instant(window.get("startDate")), _instant(window.get("endDate"))
                if start and end and start <= now < end:
                    ends.append(end)
        title = str(offer.get("title") or "").strip()
        if not title or not ends:
            continue
        mappings = offer.get("offerMappings") or []
        slug = next((row.get("pageSlug") for row in mappings
                     if row.get("pageType") == "productHome" and row.get("pageSlug")), "")
        if not slug:
            slug = str(offer.get("productSlug") or "").removesuffix("/home")
        url = "https://store.epicgames.com/en-US/free-games"
        if isinstance(slug, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,160}", slug):
            url = f"https://store.epicgames.com/en-US/p/{slug}"
        key = str(offer.get("id") or url + title)
        games[key] = {"provider": "Epic Games", "name": title, "url": url,
                      "ends_at": min(ends).isoformat()}
    return sorted(games.values(), key=lambda game: (game["ends_at"], game["name"]))


class FreeGameCollector:
    """Keep a short, region-specific cache of public offers."""

    def __init__(self, *, session=None, region="US"):
        self.session = session or requests
        region = region.upper()
        self.region = region if re.fullmatch(r"[A-Z]{2}", region) else "US"
        self._lock = Lock()
        self._offers: list[dict] = []
        self._until = 0.0
        self._error = ""

    def view(self) -> dict:
        """Refresh on demand and keep unexpired entries through provider outages."""
        now = datetime.now(timezone.utc)
        with self._lock:
            if monotonic() >= self._until:
                try:
                    with self.session.get(
                        EPIC_GIVEAWAYS, params={"country": self.region, "allowCountries": self.region, "locale": "en-US"},
                        timeout=(3, 7), stream=True, allow_redirects=False,
                    ) as response:
                        if response.status_code != 200:
                            raise ValueError("Offer feed unavailable")
                        body = bytearray()
                        for block in response.iter_content(32 * 1024):
                            body.extend(block)
                            if len(body) > MAX_FEED_BYTES:
                                raise ValueError("Offer feed too large")
                    self._offers = epic_offers(json.loads(body), now=now)
                    self._error = ""
                    self._until = monotonic() + 900
                except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
                    self._error = "Epic offers could not be refreshed. Open the store to check."
                    self._until = monotonic() + 60
            return {"items": [row for row in self._offers if _instant(row["ends_at"]) > now],
                    "region": self.region, "claim_pages": CLAIM_PAGES,
                    "error": self._error}
