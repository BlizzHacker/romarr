"""Track time-limited, free-to-keep store offers and their claim windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from threading import Lock
from time import monotonic

import requests

EPIC_GIVEAWAYS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
EPIC_FREE_PAGE = "https://store.epicgames.com/free-games?lang=en-US"
CLAIM_PAGES = (
    {"provider": "Epic Games", "url": EPIC_FREE_PAGE},
    {"provider": "Amazon Luna", "url": "https://luna.amazon.com/claims/home"},
)
MAX_FEED_BYTES = 2 * 1024 * 1024
HISTORY_DAYS = 365
HISTORY_LIMIT = 400


def _instant(value) -> datetime | None:
    """Read an aware timestamp from the promotions feed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def epic_offers(payload: dict, *, now: datetime) -> list[dict]:
    """Parse active and announced free-to-keep base-game windows."""
    catalog = payload["data"]["Catalog"]["searchStore"]["elements"]
    if not isinstance(catalog, list):
        raise ValueError("Invalid Epic catalogue")
    games = {}
    for offer in catalog:
        price = (offer.get("price") or {}).get("totalPrice") or {}
        if (offer.get("offerType") != "BASE_GAME" or not isinstance(price.get("originalPrice"), int)
                or price["originalPrice"] <= 0):
            continue
        title = str(offer.get("title") or "").strip()
        if not title:
            continue
        mappings = offer.get("offerMappings") or []
        slug = next((row.get("pageSlug") for row in mappings
                     if row.get("pageType") == "productHome" and row.get("pageSlug")), "")
        if not slug:
            slug = str(offer.get("productSlug") or "").removesuffix("/home")
        product_page = bool(isinstance(slug, str) and re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9-]{0,160}", slug))
        url = f"https://store.epicgames.com/p/{slug}?lang=en-US" if product_page else EPIC_FREE_PAGE
        promotions = offer.get("promotions") or {}
        for coming, field in ((False, "promotionalOffers"), (True, "upcomingPromotionalOffers")):
            for group in promotions.get(field) or []:
                for window in group.get("promotionalOffers") or []:
                    start, end = _instant(window.get("startDate")), _instant(window.get("endDate"))
                    if not start or not end or end <= start:
                        continue
                    discount = window.get("discountSetting") or {}
                    free_window = (discount.get("discountType") == "PERCENTAGE"
                                   and discount.get("discountPercentage") == 0)
                    if coming:
                        if not free_window or start <= now:
                            continue
                    elif (not start <= now < end or price.get("discountPrice") != 0
                          or (discount and not free_window)):
                        continue
                    offer_id = str(offer.get("id") or url + title)
                    starts_at = start.isoformat()
                    games[(offer_id, starts_at)] = {
                        "id": offer_id, "provider": "Epic Games", "name": title, "url": url,
                        "product_page": product_page, "starts_at": starts_at,
                        "ends_at": end.isoformat(), "confirmed": not coming,
                    }
    return sorted(games.values(), key=lambda game: (game["starts_at"], game["name"]))


class FreeGameCollector:
    """Cache the current Epic feed and retain previous confirmed giveaways."""

    def __init__(self, *, session=None, region="US", store=None):
        self.session = session or requests
        region = region.upper()
        self.region = region if re.fullmatch(r"[A-Z]{2}", region) else "US"
        self.store = store
        self._lock = Lock()
        self._offers: list[dict] = []
        self._retained: dict[tuple[str, str], dict] = {}
        self._until = 0.0
        self._error = ""
        if store is not None:
            saved = store.get_item("giveaway_history", self.region) or {}
            self._retained = {(row["id"], row["starts_at"]): row for row in saved.get("offers", [])
                              if isinstance(row, dict) and row.get("id") and row.get("starts_at")
                              and _instant(row.get("ends_at"))}

    def _remember(self, now: datetime) -> None:
        cutoff = now - timedelta(days=HISTORY_DAYS)
        rows = {key: row for key, row in self._retained.items() if _instant(row["ends_at"]) > cutoff}
        rows.update({(row["id"], row["starts_at"]): row for row in self._offers if row["confirmed"]})
        recent = sorted(rows.items(), key=lambda item: item[1]["ends_at"], reverse=True)[:HISTORY_LIMIT]
        retained = dict(recent)
        if self._retained != retained and self.store is not None:
            self.store.put_item("giveaway_history",
                                {"id": self.region, "offers": list(retained.values())},
                                require_persist=True)
        self._retained = retained

    def _refresh(self) -> None:
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
        now = datetime.now(timezone.utc)
        self._offers = epic_offers(json.loads(body), now=now)
        self._error = ""
        transitions = [_instant(row["ends_at"] if row["confirmed"] else row["starts_at"])
                       for row in self._offers]
        delay = min([900, *((date - now).total_seconds() for date in transitions)])
        self._until = monotonic() + max(1, delay)

    def view(self) -> dict:
        """Return claimable offers, announcements, and recorded giveaways that ended."""
        with self._lock:
            if monotonic() >= self._until:
                try:
                    self._refresh()
                except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
                    self._error = "Epic offers could not be refreshed. Check the Epic giveaways page."
                    self._until = monotonic() + 60
                else:
                    try:
                        self._remember(datetime.now(timezone.utc))
                    except OSError:
                        self._error = "Epic offers were read, but giveaway history could not be saved."
                        self._until = monotonic() + 60
            now = datetime.now(timezone.utc)
            return {
                "items": [row for row in self._offers
                          if row["confirmed"] and _instant(row["ends_at"]) > now],
                "upcoming": [row for row in self._offers
                             if not row["confirmed"] and _instant(row["starts_at"]) > now],
                "ended": sorted((row for row in self._retained.values()
                                 if now - timedelta(days=HISTORY_DAYS) < _instant(row["ends_at"]) <= now),
                                key=lambda row: row["ends_at"], reverse=True),
                "region": self.region, "claim_pages": CLAIM_PAGES, "error": self._error,
            }
