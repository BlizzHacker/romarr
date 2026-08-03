"""Telling somebody what happened.

Radarr has twenty-four notification providers and gamarr has two. The number
is not the interesting part -- almost every one of them is an HTTP POST with a
differently-shaped body, which is why this is a driver table over one sender
rather than twenty-four clients, the same way the indexer registry is a table
over two protocol clients.

**What is worth doing better is the message.** Every one of these tools sends
"Grabbed: Chrono Trigger (USA)". ROMarr's scorer already knows *why* it chose
that release and its importer knows whether the bytes verified against a DAT,
so the notification carries both:

    Grabbed Chrono Trigger (USA) [!] for Super Nintendo
    +50 verified good dump [!], +40 region usa, +40 30 seeders
    from RetroWithin

An operator reading that can tell a good pick from a lucky one without
opening the UI, which is the entire reason to send a message at all.

Nothing here retries. A notification that failed is logged and dropped: it is
not worth holding up an import, and a queue of stale "grabbed" messages
arriving an hour late is worse than none.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Events worth telling somebody about.
ON_GRAB = "grab"
ON_IMPORT = "import"
ON_FAILURE = "failure"
ON_UPGRADE = "upgrade"
ON_BAD_DUMP = "bad-dump"

EVENTS = (ON_GRAB, ON_IMPORT, ON_UPGRADE, ON_FAILURE, ON_BAD_DUMP)

TIMEOUT = 15


@dataclass
class Message:
    """One thing that happened, in a shape every provider can render."""

    event: str
    title: str
    body: str = ""
    #: The scorer's reasons, when there are any. This is the part other tools
    #: do not have and cannot add: it comes from `Judgement.why()`.
    reasons: tuple[str, ...] = ()
    url: str = ""

    def text(self) -> str:
        lines = [self.title]
        if self.body:
            lines.append(self.body)
        lines.extend(self.reasons)
        return "\n".join(lines)


def _post(url: str, *, data: bytes | None = None, headers: dict | None = None,
          method: str = "POST") -> bool:
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        log.warning("notification to %s rejected: HTTP %s",
                    _safe(url), exc.code)
    except Exception as exc:                    # a down endpoint is routine
        log.warning("notification to %s failed: %s", _safe(url), exc)
    return False


def _safe(url: str) -> str:
    """A URL fit for a log. Discord and Slack webhook paths ARE the credential."""
    parts = urllib.parse.urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}/…" if parts.netloc else "the endpoint"


def _json_post(url: str, payload: dict, headers: dict | None = None) -> bool:
    return _post(url, data=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json", **(headers or {})})


# --- providers -------------------------------------------------------------
#
# Each takes (config, message) and returns whether it was delivered.

def _discord(cfg: dict, message: Message) -> bool:
    payload = {"content": message.text()[:1900]}
    if cfg.get("username"):
        payload["username"] = cfg["username"]
    return _json_post(cfg.get("url", ""), payload)


def _slack(cfg: dict, message: Message) -> bool:
    return _json_post(cfg.get("url", ""), {"text": message.text()})


def _webhook(cfg: dict, message: Message) -> bool:
    """A generic webhook gets the structured event, not a rendered string.

    Anything consuming this is a program, and handing it prose to parse back
    into fields is the thing that makes generic webhooks useless.
    """
    return _json_post(cfg.get("url", ""), {
        "event": message.event, "title": message.title,
        "body": message.body, "reasons": list(message.reasons),
        "url": message.url,
    })


def _ntfy(cfg: dict, message: Message) -> bool:
    headers = {"Title": message.title, "Content-Type": "text/plain"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    if cfg.get("priority"):
        headers["Priority"] = str(cfg["priority"])
    body = "\n".join([message.body, *message.reasons]).strip() or message.title
    return _post(cfg.get("url", ""), data=body.encode(), headers=headers)


def _gotify(cfg: dict, message: Message) -> bool:
    base = str(cfg.get("url", "")).rstrip("/")
    token = urllib.parse.quote(str(cfg.get("token", "")))
    return _json_post(f"{base}/message?token={token}",
                      {"title": message.title,
                       "message": "\n".join([message.body, *message.reasons]).strip(),
                       "priority": int(cfg.get("priority") or 5)})


def _telegram(cfg: dict, message: Message) -> bool:
    token = str(cfg.get("token", ""))
    return _json_post(f"https://api.telegram.org/bot{token}/sendMessage",
                      {"chat_id": cfg.get("chat_id", ""),
                       "text": message.text(),
                       "disable_web_page_preview": True})


def _pushover(cfg: dict, message: Message) -> bool:
    data = urllib.parse.urlencode({
        "token": cfg.get("token", ""), "user": cfg.get("user", ""),
        "title": message.title,
        "message": "\n".join([message.body, *message.reasons]).strip(),
    }).encode()
    return _post("https://api.pushover.net/1/messages.json", data=data,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})


def _apprise(cfg: dict, message: Message) -> bool:
    """An Apprise API server, which is itself a fan-out to a hundred more.

    Worth carrying because it makes "every provider Radarr has" a
    configuration question rather than a code question.
    """
    base = str(cfg.get("url", "")).rstrip("/")
    payload = {"body": "\n".join([message.body, *message.reasons]).strip()
                       or message.title,
               "title": message.title, "type": "info"}
    if cfg.get("urls"):
        payload["urls"] = cfg["urls"]
    return _json_post(f"{base}/notify", payload)


NOTIFIERS: dict[str, dict] = {
    "discord": {"label": "Discord", "send": _discord,
                "fields": ("url", "username"),
                "help": "A Discord channel webhook URL."},
    "slack": {"label": "Slack", "send": _slack, "fields": ("url",),
              "help": "A Slack incoming-webhook URL."},
    "webhook": {"label": "Webhook", "send": _webhook, "fields": ("url",),
                "help": "Receives the structured event as JSON."},
    "ntfy": {"label": "ntfy.sh", "send": _ntfy,
             "fields": ("url", "token", "priority"),
             "help": "The full topic URL, e.g. https://ntfy.sh/my-topic."},
    "gotify": {"label": "Gotify", "send": _gotify,
               "fields": ("url", "token", "priority"),
               "help": "Server URL plus an application token."},
    "telegram": {"label": "Telegram", "send": _telegram,
                 "fields": ("token", "chat_id"),
                 "help": "A bot token and the chat id to post into."},
    "pushover": {"label": "Pushover", "send": _pushover,
                 "fields": ("token", "user"),
                 "help": "Application token and user key."},
    "apprise": {"label": "Apprise", "send": _apprise,
                "fields": ("url", "urls"),
                "help": "An Apprise API server, which fans out to a hundred "
                        "more services."},
}


@dataclass
class Notifier:
    """Every configured connection, fanned out to."""

    connections: list[dict] = field(default_factory=list)

    def send(self, message: Message) -> list[dict]:
        """Deliver to every connection subscribed to this event.

        Never raises. One provider being down must not fail an import that
        already succeeded -- the delivery is a courtesy, the import is the
        work.
        """
        out = []
        for cfg in self.connections:
            if not cfg.get("enable", True):
                continue
            if not self.wants(cfg, message.event):
                continue
            spec = NOTIFIERS.get(str(cfg.get("type") or "").lower())
            if spec is None:
                log.warning("unknown notification type %r", cfg.get("type"))
                continue
            try:
                ok = bool(spec["send"](cfg, message))
            except Exception as exc:
                log.warning("notification %r raised: %s", cfg.get("name"), exc)
                ok = False
            out.append({"name": cfg.get("name") or spec["label"], "ok": ok})
        return out

    @staticmethod
    def wants(cfg: dict, event: str) -> bool:
        """Whether this connection subscribes to this event.

        A connection with no `events` key gets everything. That is the useful
        default -- somebody who just pasted a Discord webhook wants to be told
        things -- and an empty list means exactly what it says: nothing.
        """
        events = cfg.get("events")
        if events is None:
            return True
        return event in events


def grabbed(title: str, platform: str, indexer: str, reasons) -> Message:
    """The message other tools cannot send.

    Radarr tells you it grabbed something. This tells you what it weighed,
    which is the difference between trusting the automation and checking it.
    """
    return Message(
        event=ON_GRAB,
        title=f"Grabbed {title}",
        body=f"{platform} — from {indexer}" if platform else f"from {indexer}",
        reasons=tuple(reasons or ()),
    )


def imported(title: str, platform: str, verification) -> Message:
    status = getattr(verification, "status", "") or ""
    detail = getattr(verification, "detail", "") or ""
    game = getattr(verification, "game", "") or ""
    if status == "verified":
        note = f"verified against DAT as {game}" if game else "verified"
        event = ON_IMPORT
    elif status == "bad-dump":
        note = f"BAD DUMP — {detail}"
        event = ON_BAD_DUMP
    else:
        note = "not in any loaded DAT"
        event = ON_IMPORT
    return Message(event=event, title=f"Imported {title}",
                   body=f"{platform} — {note}" if platform else note)


def failed(title: str, reason: str) -> Message:
    return Message(event=ON_FAILURE, title=f"Failed {title}", body=reason)
