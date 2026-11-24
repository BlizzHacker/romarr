"""Relay authenticated library WebSocket upgrades without interpreting frames."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import io
import os
import re
import select
import socket
import ssl
import threading
from urllib.parse import urlsplit

import requests

SOCKET_PATHS = frozenset(
    route + suffix
    for route in ("/ws/socket.io", "/netplay/socket.io", "/socket.io")
    for suffix in ("", "/")
)
CONNECTIONS = threading.BoundedSemaphore(64)
IO_TIMEOUT = 10
IDLE_TIMEOUT = 90
HEADER_LIMIT = 16384
WS_VERSION = "13"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class HandshakeError(Exception):
    """An upstream response that cannot be upgraded."""


def connection_tokens(value: str) -> set[str]:
    """Read the case-insensitive tokens in an HTTP Connection header."""
    return {token.strip().lower() for token in value.split(",")}


def valid_key(key: str) -> bool:
    """A WebSocket key is the standard Base64 encoding of 16 random bytes."""
    try:
        return len(base64.b64decode(key, validate=True)) == 16
    except (ValueError, binascii.Error):
        return False


def protocols(value: str) -> tuple[str, ...]:
    """Return the client's subprotocol list, rejecting malformed tokens."""
    if not value:
        return ()
    names = tuple(name.strip() for name in value.split(","))
    if not all(TOKEN.fullmatch(name) for name in names):
        raise ValueError("invalid WebSocket subprotocol")
    return names


def origin_allowed(origin: str, host: str, forwarded_proto: str, public_url: str,
                   request_scheme: str = "http") -> bool:
    """Only an explicitly configured or same-host browser origin can connect."""
    try:
        incoming = urlsplit(origin)
        public = urlsplit(public_url)
    except ValueError:
        return False
    if (incoming.scheme not in ("http", "https") or not incoming.hostname
            or incoming.username is not None or incoming.path or incoming.query or incoming.fragment):
        return False
    actual = (incoming.scheme, incoming.netloc.lower())
    registered = (public.scheme, public.netloc.lower())
    host = host.strip().lower()
    if registered[0] in ("http", "https") and (actual == registered or host == registered[1]):
        return actual == registered
    forwarded = forwarded_proto.strip().lower()
    expected_scheme = forwarded if forwarded in ("http", "https") else request_scheme
    return actual == (expected_scheme, host)


def valid_path(path: str) -> bool:
    """Permit only the known library Socket.IO transports."""
    if not path.startswith("/") or path.startswith("//") or any(ord(c) <= 32 or ord(c) >= 127 for c in path):
        return False
    parts = urlsplit(path)
    return parts.path in SOCKET_PATHS and not parts.fragment


def _response(upstream: socket.socket, key: str, offered: tuple[str, ...]) -> tuple[str, bytes]:
    """Validate a bounded handshake and retain any WebSocket bytes read with it."""
    data = bytearray()
    separator = b"\r\n\r\n"
    while separator not in data:
        block = upstream.recv(4096)
        if not block:
            raise HandshakeError("library closed the WebSocket handshake")
        data.extend(block)
        if len(data) > HEADER_LIMIT and separator not in data[:HEADER_LIMIT]:
            raise HandshakeError("library WebSocket headers are too large")
    head, frames = data.split(separator, 1)
    status, _, raw_headers = head.partition(b"\r\n")
    fields = status.split(maxsplit=2)
    if fields[:2] != [b"HTTP/1.1", b"101"]:
        raise HandshakeError("library did not accept the WebSocket upgrade")
    headers = http.client.parse_headers(io.BytesIO(raw_headers + separator))
    if (headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in connection_tokens(headers.get("Connection", ""))
            or headers.get_all("Sec-WebSocket-Accept") != [accept_key(key)]
            or headers.get("Sec-WebSocket-Extensions")):
        raise HandshakeError("library returned an invalid WebSocket handshake")
    selected = headers.get_all("Sec-WebSocket-Protocol", [])
    if selected and (len(selected) != 1 or selected[0] not in offered):
        raise HandshakeError("library selected an unoffered WebSocket subprotocol")
    return selected[0] if selected else "", bytes(frames)


def accept_key(key: str) -> str:
    """Compute the RFC 6455 challenge response."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii"), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


def _tls_context() -> ssl.SSLContext:
    """Use the same CA bundle selection as the library HTTP relay."""
    bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or requests.certs.where()
    if os.path.isdir(bundle):
        return ssl.create_default_context(capath=bundle)
    return ssl.create_default_context(cafile=bundle)


def open_upstream(origin: str, path: str, key: str, offered: tuple[str, ...],
                  credential_headers: dict[str, str]) -> tuple[http.client.HTTPConnection, str, bytes]:
    """Open one connection to the selected library and validate its response."""
    target = urlsplit(origin)
    if target.scheme == "https":
        upstream = http.client.HTTPSConnection(
            target.hostname, target.port, timeout=IO_TIMEOUT, context=_tls_context())
    else:
        upstream = http.client.HTTPConnection(target.hostname, target.port, timeout=IO_TIMEOUT)
    headers = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Origin": origin,
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": WS_VERSION,
        **credential_headers,
    }
    if offered:
        headers["Sec-WebSocket-Protocol"] = ", ".join(offered)
    try:
        upstream.request("GET", path, headers=headers)
        selected, initial = _response(upstream.sock, key, offered)
    except (OSError, ValueError, http.client.HTTPException, HandshakeError):
        upstream.close()
        raise
    return upstream, selected, initial


def relay(client: socket.socket, upstream: socket.socket, initial: bytes) -> None:
    """Copy frames both ways with bounded writes and an idle deadline."""
    client.settimeout(IO_TIMEOUT)
    if initial:
        client.sendall(initial)
    peers = {client: upstream, upstream: client}
    while True:
        buffered = isinstance(upstream, ssl.SSLSocket) and upstream.pending() > 0
        readable, _, _ = select.select(list(peers), [], [], 0 if buffered else IDLE_TIMEOUT)
        if buffered and upstream not in readable:
            readable.append(upstream)
        if not readable:
            return
        for source in readable:
            block = source.recv(16384)
            if not block:
                return
            peers[source].sendall(block)
