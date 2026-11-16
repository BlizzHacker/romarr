"""gamesgamesgamesgamesgames video game architecture absorption (Kirby style).

We aren't absorbing generic ATProto itself; we are absorbing gamesgamesgamesgamesgames'
use of it to standardize decentralized video game records:
- Lexicons: games.gamesgamesgamesgames.* (game, release, playState, achievement, rating, review)
  and dev.cartridge.verificationRequest.
- HappyView Gaming AppView: Schema-driven XRPC queries, Jetstream game streaming, PDS backfill.
- Cartridge Gaming Studio: Self-sovereign game collection claims and DAT checksum assertions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlparse

log = logging.getLogger(__name__)

# Official ATProto Lexicon schemas absorbed from gamesgamesgamesgamesgames/lexicon
LEXICONS: dict[str, dict[str, Any]] = {
    "games.gamesgamesgamesgames.game": {
        "lexicon": 1,
        "id": "games.gamesgamesgamesgames.game",
        "defs": {
            "main": {
                "type": "record",
                "description": "Decentralized video game record on ATProto.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["name", "createdAt"],
                    "properties": {
                        "name": {"type": "string", "maxLength": 500},
                        "summary": {"type": "string", "maxLength": 5000},
                        "createdAt": {"type": "string", "format": "datetime"},
                        "releaseDate": {"type": "string", "format": "datetime"},
                        "genres": {"type": "array", "items": {"type": "string"}},
                        "platforms": {"type": "array", "items": {"type": "string"}},
                        "developers": {"type": "array", "items": {"type": "string"}},
                        "publishers": {"type": "array", "items": {"type": "string"}},
                        "igdbId": {"type": "string"},
                        "wikidataId": {"type": "string"},
                        "rommId": {"type": "string"},
                        "bannerUrl": {"type": "string", "format": "uri"},
                        "coverUrl": {"type": "string", "format": "uri"},
                    },
                },
            }
        },
    },
    "games.gamesgamesgamesgames.release": {
        "lexicon": 1,
        "id": "games.gamesgamesgamesgames.release",
        "defs": {
            "main": {
                "type": "record",
                "description": "Specific platform release edition and ROM dump metadata.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["gameUri", "platform", "createdAt"],
                    "properties": {
                        "gameUri": {"type": "string", "format": "at-uri"},
                        "platform": {"type": "string"},
                        "region": {"type": "string"},
                        "format": {"type": "string"},
                        "sha256": {"type": "string"},
                        "crc32": {"type": "string"},
                        "md5": {"type": "string"},
                        "sizeBytes": {"type": "integer"},
                        "verified": {"type": "boolean"},
                        "datSource": {"type": "string"},
                        "createdAt": {"type": "string", "format": "datetime"},
                    },
                },
            }
        },
    },
    "games.gamesgamesgamesgames.playState": {
        "lexicon": 1,
        "id": "games.gamesgamesgamesgames.playState",
        "defs": {
            "main": {
                "type": "record",
                "description": "Player gameplay state and progress across devices.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["gameUri", "status", "updatedAt"],
                    "properties": {
                        "gameUri": {"type": "string", "format": "at-uri"},
                        "status": {
                            "type": "string",
                            "enum": ["playing", "completed", "backlog", "dropped", "wishlist"],
                        },
                        "playTimeMinutes": {"type": "integer", "minimum": 0},
                        "rating": {"type": "number", "minimum": 0, "maximum": 5},
                        "notes": {"type": "string", "maxLength": 2000},
                        "updatedAt": {"type": "string", "format": "datetime"},
                        "lastPlayedAt": {"type": "string", "format": "datetime"},
                    },
                },
            }
        },
    },
    "games.gamesgamesgamesgames.achievement": {
        "lexicon": 1,
        "id": "games.gamesgamesgamesgames.achievement",
        "defs": {
            "main": {
                "type": "record",
                "description": "Decentralized achievement or trophy earned in a game.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["gameUri", "title", "unlockedAt"],
                    "properties": {
                        "gameUri": {"type": "string", "format": "at-uri"},
                        "title": {"type": "string", "maxLength": 200},
                        "description": {"type": "string", "maxLength": 1000},
                        "iconUrl": {"type": "string", "format": "uri"},
                        "points": {"type": "integer", "default": 0},
                        "unlockedAt": {"type": "string", "format": "datetime"},
                    },
                },
            }
        },
    },
    "games.gamesgamesgamesgames.rating": {
        "lexicon": 1,
        "id": "games.gamesgamesgamesgames.rating",
        "defs": {
            "main": {
                "type": "record",
                "description": "Community game rating and score.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["gameUri", "value", "createdAt"],
                    "properties": {
                        "gameUri": {"type": "string", "format": "at-uri"},
                        "value": {"type": "number", "minimum": 1, "maximum": 10},
                        "createdAt": {"type": "string", "format": "datetime"},
                    },
                },
            }
        },
    },
    "dev.cartridge.verificationRequest": {
        "lexicon": 1,
        "id": "dev.cartridge.verificationRequest",
        "defs": {
            "main": {
                "type": "record",
                "description": "Cartridge self-sovereign ROM verification and checksum assertion.",
                "key": "tid",
                "record": {
                    "type": "object",
                    "required": ["sha256", "platform", "createdAt"],
                    "properties": {
                        "sha256": {"type": "string"},
                        "crc32": {"type": "string"},
                        "platform": {"type": "string"},
                        "romTitle": {"type": "string"},
                        "datCatalog": {"type": "string"},
                        "verified": {"type": "boolean"},
                        "createdAt": {"type": "string", "format": "datetime"},
                    },
                },
            }
        },
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_tid() -> str:
    """Generate a base32 ATProto TID timestamp identifier."""
    # Clock microseconds encoded into 13-character base32-sortable TID
    micros = int(time.time() * 1_000_000)
    b32_chars = "234567abcdefghijklmnopqrstuvwxyz"
    res = []
    for _ in range(13):
        res.append(b32_chars[micros & 0x1F])
        micros >>= 5
    return "".join(reversed(res))


def build_at_uri(did: str, collection: str, rkey: str) -> str:
    """Format a valid AT-URI: at://<did>/<collection>/<rkey>."""
    return f"at://{did}/{collection}/{rkey}"


def parse_at_uri(uri: str) -> dict[str, str]:
    """Parse an at:// URI into did, collection, and rkey components."""
    if not uri.startswith("at://"):
        raise ValueError(f"Invalid AT-URI format: {uri}")
    parts = uri[5:].split("/")
    if len(parts) < 3:
        raise ValueError(f"Incomplete AT-URI: {uri}")
    return {"did": parts[0], "collection": parts[1], "rkey": parts[2]}


class ATProtoClient:
    """Client for AT Protocol Personal Data Servers (PDS) and XRPC."""

    def __init__(self, pds_url: str = "https://bsky.social", session: Optional[Any] = None) -> None:
        self.pds_url = pds_url.rstrip("/")
        self.session = session
        self.access_jwt: Optional[str] = None
        self.did: Optional[str] = None
        self.handle: Optional[str] = None

    def _http(self):
        if self.session is not None:
            return self.session
        import requests
        return requests.Session()

    def resolve_handle(self, handle: str) -> str:
        """Resolve an ATProto handle (e.g. user.bsky.social) to its DID."""
        if handle.startswith("did:"):
            return handle
        url = f"{self.pds_url}/xrpc/com.atproto.identity.resolveHandle"
        resp = self._http().get(url, params={"handle": handle}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["did"]

    def create_session(self, identifier: str, password: str) -> dict[str, Any]:
        """Authenticate with an ATProto PDS using identifier and app password."""
        url = f"{self.pds_url}/xrpc/com.atproto.server.createSession"
        payload = {"identifier": identifier, "password": password}
        resp = self._http().post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.access_jwt = data.get("accessJwt")
        self.did = data.get("did")
        self.handle = data.get("handle")
        return data

    def put_record(self, collection: str, record: dict[str, Any], rkey: Optional[str] = None) -> dict[str, Any]:
        """Publish a record to the user's ATProto repository."""
        if not self.did or not self.access_jwt:
            raise PermissionError("Authentication required before writing to ATProto PDS")
        url = f"{self.pds_url}/xrpc/com.atproto.repo.putRecord"
        rkey = rkey or generate_tid()
        payload = {
            "repo": self.did,
            "collection": collection,
            "rkey": rkey,
            "record": record,
        }
        headers = {"Authorization": f"Bearer {self.access_jwt}"}
        resp = self._http().post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def list_records(self, repo: str, collection: str, limit: int = 50) -> list[dict[str, Any]]:
        """List records from an ATProto repository collection."""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.listRecords"
        resp = self._http().get(url, params={"repo": repo, "collection": collection, "limit": limit}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("records", [])


class HappyViewAppView:
    """Absorbed HappyView AppView engine: schema-driven XRPC & Jetstream streaming."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.storage_file = data_dir / "atproto-records.json"
        self._records: dict[str, dict[str, Any]] = self._load_storage()

    def _load_storage(self) -> dict[str, dict[str, Any]]:
        if self.storage_file.is_file():
            try:
                return json.loads(self.storage_file.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Failed to parse atproto records storage: %s", e)
        return {
            "games.gamesgamesgamesgames.game": {},
            "games.gamesgamesgamesgames.release": {},
            "games.gamesgamesgamesgames.playState": {},
            "games.gamesgamesgamesgames.achievement": {},
            "dev.cartridge.verificationRequest": {},
        }

    def _save_storage(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_file.write_text(json.dumps(self._records, indent=2), encoding="utf-8")

    def store_record(self, collection: str, rkey: str, record: dict[str, Any], did: str = "did:plc:romarr-local") -> str:
        """Store a record in the local AppView index."""
        if collection not in self._records:
            self._records[collection] = {}
        uri = build_at_uri(did, collection, rkey)
        self._records[collection][uri] = {
            "uri": uri,
            "cid": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
            "value": record,
        }
        self._save_storage()
        return uri

    def handle_xrpc_get_game(self, params: dict[str, Any]) -> dict[str, Any]:
        """XRPC query: xrpc/games.gamesgamesgamesgames.getGame."""
        uri = params.get("uri")
        games = self._records.get("games.gamesgamesgamesgames.game", {})
        if uri:
            if uri in games:
                return games[uri]
            raise KeyError(f"Game not found for URI: {uri}")
        name = (params.get("name") or "").lower()
        for g_uri, item in games.items():
            if item.get("value", {}).get("name", "").lower() == name:
                return item
        raise KeyError(f"Game not found for name: {name}")

    def handle_xrpc_list_games(self, params: dict[str, Any]) -> dict[str, Any]:
        """XRPC query: xrpc/games.gamesgamesgamesgames.listGames."""
        games = self._records.get("games.gamesgamesgamesgames.game", {})
        limit = int(params.get("limit") or 50)
        items = list(games.values())[:limit]
        return {"games": items, "count": len(items)}

    def handle_xrpc_get_play_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """XRPC query: xrpc/games.gamesgamesgamesgames.getPlayState."""
        states = self._records.get("games.gamesgamesgamesgames.playState", {})
        game_uri = params.get("gameUri")
        if game_uri:
            for item in states.values():
                if item.get("value", {}).get("gameUri") == game_uri:
                    return item
        return {"playStates": list(states.values())}

    def process_jetstream_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Consume and index an incoming ATProto Jetstream commit event."""
        kind = event.get("kind")
        commit = event.get("commit", {})
        collection = commit.get("collection")
        rkey = commit.get("rkey")
        record = commit.get("record", {})
        did = event.get("did", "did:plc:anonymous")

        if kind == "commit" and collection in LEXICONS and rkey:
            operation = commit.get("operation")
            if operation in ("create", "update"):
                uri = self.store_record(collection, rkey, record, did=did)
                return {"indexed": True, "uri": uri, "collection": collection}
            elif operation == "delete":
                uri = build_at_uri(did, collection, rkey)
                if collection in self._records and uri in self._records[collection]:
                    del self._records[collection][uri]
                    self._save_storage()
                    return {"deleted": True, "uri": uri}
        return {"indexed": False, "reason": "unhandled_or_skipped"}


class ATProtoKirbyHub:
    """The central absorption hub for ATProto, HappyView, and Cartridge Studio."""

    def __init__(self, data_dir: Path, pds_url: str = "https://bsky.social", session: Optional[Any] = None) -> None:
        self.data_dir = data_dir
        self.config_file = data_dir / "atproto-config.json"
        self.appview = HappyViewAppView(data_dir)
        self.client = ATProtoClient(pds_url=pds_url, session=session)
        self.config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        if self.config_file.is_file():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Failed to read ATProto config: %s", e)
        return {
            "enabled": True,
            "handle": None,
            "did": None,
            "pds_url": self.client.pds_url,
            "sync_library_to_atproto": False,
            "absorbed_capabilities": [
                "atproto.identity.dids",
                "atproto.repo.xrpc",
                "games.gamesgamesgamesgames.game",
                "games.gamesgamesgamesgames.release",
                "games.gamesgamesgamesgames.playState",
                "games.gamesgamesgamesgames.achievement",
                "happyview.appview.schema_driven",
                "happyview.jetstream.sync",
                "cartridge.atproto.verification_request",
                "cartridge.atproto.sovereign_claims",
            ],
        }

    def _save_config(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def status(self) -> dict[str, Any]:
        """Return ATProto federation and absorption status."""
        return {
            "ok": True,
            "absorption": "Kirby-Style Complete: gamesgamesgamesgamesgames gaming stack",
            "gaming_standard": "games.gamesgamesgamesgames.*",
            "atproto_connected": bool(self.config.get("did")),
            "handle": self.config.get("handle"),
            "did": self.config.get("did"),
            "pds_url": self.config.get("pds_url"),
            "lexicons_loaded": list(LEXICONS.keys()),
            "absorbed_capabilities": self.config.get("absorbed_capabilities", []),
            "stored_records_summary": {k: len(v) for k, v in self.appview._records.items()},
        }

    def connect(self, identifier: str, password: str) -> dict[str, Any]:
        """Connect to an ATProto PDS account."""
        session_data = self.client.create_session(identifier, password)
        self.config["handle"] = session_data.get("handle")
        self.config["did"] = session_data.get("did")
        self._save_config()
        return {
            "ok": True,
            "handle": self.config["handle"],
            "did": self.config["did"],
            "pds_url": self.client.pds_url,
        }

    def export_game(self, game_dict: dict[str, Any]) -> dict[str, Any]:
        """Convert a ROMarr game item into an ATProto games.gamesgamesgamesgames.game record."""
        title = game_dict.get("title") or game_dict.get("name") or "Untitled Game"
        rkey = generate_tid()
        record = {
            "$type": "games.gamesgamesgamesgames.game",
            "name": title,
            "summary": game_dict.get("overview") or game_dict.get("summary") or "",
            "createdAt": _utc_now(),
            "platforms": [str(game_dict.get("platform") or "retro")],
            "genres": game_dict.get("genres") or [],
            "igdbId": str(game_dict.get("igdb_id") or ""),
            "rommId": str(game_dict.get("id") or ""),
        }
        did = self.config.get("did") or "did:plc:romarr-local"
        uri = self.appview.store_record("games.gamesgamesgamesgames.game", rkey, record, did=did)

        # If file verification checksums exist, also record a release
        release_uri = None
        if game_dict.get("sha256") or game_dict.get("crc32"):
            rel_rkey = generate_tid()
            rel_record = {
                "$type": "games.gamesgamesgamesgames.release",
                "gameUri": uri,
                "platform": str(game_dict.get("platform") or "retro"),
                "sha256": game_dict.get("sha256"),
                "crc32": game_dict.get("crc32"),
                "verified": bool(game_dict.get("verified")),
                "createdAt": _utc_now(),
            }
            release_uri = self.appview.store_record("games.gamesgamesgamesgames.release", rel_rkey, rel_record, did=did)

        return {"ok": True, "gameUri": uri, "releaseUri": release_uri, "record": record}

    def export_library(self, games_list: list[dict[str, Any]]) -> dict[str, Any]:
        """Export an entire ROMarr game library into ATProto records."""
        exported = []
        for g in games_list:
            res = self.export_game(g)
            exported.append(res)
        return {
            "ok": True,
            "exported_count": len(exported),
            "games": exported,
        }

    def publish_play_state(self, game_uri: str, status: str, play_time_minutes: int = 0, rating: Optional[float] = None) -> dict[str, Any]:
        """Set decentralized gameplay state (playing, completed, etc.)."""
        if status not in ("playing", "completed", "backlog", "dropped", "wishlist"):
            raise ValueError(f"Invalid status: {status}")
        rkey = generate_tid()
        record = {
            "$type": "games.gamesgamesgamesgames.playState",
            "gameUri": game_uri,
            "status": status,
            "playTimeMinutes": play_time_minutes,
            "updatedAt": _utc_now(),
            "lastPlayedAt": _utc_now(),
        }
        if rating is not None:
            record["rating"] = rating
        did = self.config.get("did") or "did:plc:romarr-local"
        uri = self.appview.store_record("games.gamesgamesgamesgames.playState", rkey, record, did=did)
        return {"ok": True, "playStateUri": uri, "record": record}

    def create_verification_claim(self, rom_title: str, platform: str, sha256: str, crc32: Optional[str] = None, dat_catalog: Optional[str] = None) -> dict[str, Any]:
        """Generate a Cartridge ATProto verification request proving authentic dump."""
        rkey = generate_tid()
        record = {
            "$type": "dev.cartridge.verificationRequest",
            "romTitle": rom_title,
            "platform": platform,
            "sha256": sha256,
            "crc32": crc32 or "",
            "datCatalog": dat_catalog or "No-Intro",
            "verified": True,
            "createdAt": _utc_now(),
        }
        did = self.config.get("did") or "did:plc:romarr-local"
        uri = self.appview.store_record("dev.cartridge.verificationRequest", rkey, record, did=did)
        return {"ok": True, "claimUri": uri, "record": record}
