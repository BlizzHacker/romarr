"""Believing an identity your reverse proxy already established.

Authentik, Authelia, oauth2-proxy and Cloudflare Access all work the same way:
they authenticate the user, then forward the request with a header naming
them. The application's job is to believe that header -- **and only from the
proxy.**

That qualifier is the entire security of the arrangement, and it is why this
module exists rather than a two-line header read. A header is text anybody can
send. An app that believes `X-authentik-username` from any source has not
added single sign-on; it has added a way to become any user by typing one
header:

    curl -H 'X-authentik-username: admin' http://romarr:6868/api/v1/config

So every identity is gated on the *socket peer* being inside a configured
trusted range. Not `X-Forwarded-For` -- that is client-controlled too, and
reading the peer out of it reintroduces the same bypass through the back door.

It also replaces the blunt instrument it grew out of. `ROMARR_AUTH=disabled`
exists for installs behind an authenticating proxy, and it is honest about
what it does: ROMarr stops checking anything, and any request that reaches the
port is in. Forward auth is the same deployment done properly -- the proxy is
still the authority, but ROMarr verifies the request came *through* it, learns
who the user is, and can require a group.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The headers each product really sends. Getting one of these wrong produces
#: an install that authenticates correctly and then reports every request as
#: anonymous, which reads as "SSO does not work" rather than "wrong header".
FORWARD_PROVIDERS: dict[str, dict] = {
    "authentik": {
        "label": "Authentik",
        "user_header": "X-authentik-username",
        "email_header": "X-authentik-email",
        "groups_header": "X-authentik-groups",
        "help": "Authentik's proxy outpost. Groups arrive pipe-separated, and "
                "only if the provider is configured to send them.",
    },
    "authelia": {
        "label": "Authelia",
        "user_header": "Remote-User",
        "email_header": "Remote-Email",
        "groups_header": "Remote-Groups",
        "help": "Authelia's forward-auth endpoint.",
    },
    "oauth2-proxy": {
        "label": "oauth2-proxy",
        "user_header": "X-Forwarded-User",
        "email_header": "X-Forwarded-Email",
        "groups_header": "X-Forwarded-Groups",
        "help": "Requires --set-xauthrequest and the headers passed through.",
    },
    "cloudflare-access": {
        "label": "Cloudflare Access",
        "user_header": "Cf-Access-Authenticated-User-Email",
        "email_header": "Cf-Access-Authenticated-User-Email",
        "groups_header": "",
        "help": "Trusted proxies must be Cloudflare's ranges, not your LAN.",
    },
    "tinyauth": {
        "label": "Tinyauth",
        "user_header": "Remote-User",
        "email_header": "Remote-Email",
        "groups_header": "Remote-Groups",
        "help": "Tinyauth's forward-auth headers.",
    },
    "custom": {
        "label": "Custom",
        "user_header": "X-Forwarded-User",
        "email_header": "X-Forwarded-Email",
        "groups_header": "X-Forwarded-Groups",
        "help": "Name the headers your proxy sends.",
    },
}


def trusted_peer(peer: str, allowed) -> bool:
    """Whether the immediate peer is one of the configured proxies.

    An empty list trusts nobody. That is deliberate and is the most important
    default in this file: an operator who turned forward auth on and has not
    filled in the ranges yet has an *unfinished* configuration, and reading it
    as "trust everything" turns that into an open install at the exact moment
    they are least likely to notice.
    """
    if not allowed:
        return False
    try:
        address = ipaddress.ip_address(str(peer or "").strip())
    except ValueError:
        return False
    for entry in allowed:
        entry = str(entry or "").strip()
        if not entry:
            continue
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            log.warning("ignoring unparseable trusted proxy %r", entry)
    return False


@dataclass(frozen=True)
class Identity:
    ok: bool
    user: str = ""
    email: str = ""
    groups: tuple[str, ...] = ()
    reason: str = ""


class ForwardAuth:
    """Identity from a trusted proxy's headers."""

    def __init__(self, provider: str = "authentik", trusted_proxies=None,
                 user_header: str = "", groups_header: str = "",
                 email_header: str = "", required_group: str = ""):
        preset = FORWARD_PROVIDERS.get(provider, FORWARD_PROVIDERS["custom"])
        self.provider = provider
        self.trusted_proxies = list(trusted_proxies or [])
        self.user_header = user_header or preset["user_header"]
        self.email_header = email_header or preset["email_header"]
        self.groups_header = groups_header or preset["groups_header"]
        self.required_group = (required_group or "").strip()

    def identify(self, headers, peer: str) -> Identity:
        """Who this request is, or why it is nobody.

        `peer` is the socket's remote address and nothing else. It is a
        parameter rather than something read from the headers because the only
        trustworthy answer comes from the connection itself -- see the module
        docstring.
        """
        if not trusted_peer(peer, self.trusted_proxies):
            return Identity(False, reason=f"{peer or 'the caller'} is not a "
                                          "trusted proxy")
        user = _header(headers, self.user_header)
        if not user:
            return Identity(
                False,
                reason=f"trusted proxy sent no identity header "
                       f"({self.user_header}); is this path protected in the "
                       "proxy configuration?")

        groups = _groups(_header(headers, self.groups_header))
        if self.required_group and self.required_group not in groups:
            # A missing groups header is a refusal, not a pass. Authentik only
            # sends groups when the provider is told to, and treating absence
            # as "no restriction" silently disables the restriction that was
            # asked for.
            return Identity(
                False, user=user, groups=groups,
                reason=f"{user!r} is not in the required group "
                       f"{self.required_group!r}")

        return Identity(True, user=user,
                        email=_header(headers, self.email_header),
                        groups=groups)


def _header(headers, name: str) -> str:
    if not name or headers is None:
        return ""
    got = headers.get(name)
    if got:
        return str(got).strip()
    lowered = name.lower()
    for key, value in getattr(headers, "items", lambda: [])():
        if str(key).lower() == lowered and value:
            return str(value).strip()
    return ""


def _groups(raw: str) -> tuple[str, ...]:
    """Group names, in whichever separator the provider chose.

    Authentik pipes them, oauth2-proxy commas them, and some setups use
    spaces. Supporting one separator means a correctly configured install
    silently fails its group check on the other two.
    """
    text = str(raw or "")
    for separator in ("|", ","):
        text = text.replace(separator, " ")
    return tuple(part for part in text.split() if part)
