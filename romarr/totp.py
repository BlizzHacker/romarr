"""Time-based one-time passwords, RFC 6238.

Thirty lines of HMAC and a lot of care around the edges. The algorithm is the
easy part -- the RFC even ships test vectors, which is why they are pinned in
the tests rather than trusting the implementation to agree with itself.

What implementations get wrong is the other three things:

  * **Replay.** A code is valid for its 30-second step plus the drift
    tolerance either side. Anyone who sees one -- over a shoulder, in a proxy
    log, in a screenshot posted for help -- can use it until it expires.
    Accepting each code exactly once is the entire point of the counter.
  * **Comparison.** Six digits is a small space, and a byte-by-byte `==`
    leaks how much of a guess was right through timing.
  * **Recovery.** A phone gets lost, and a second factor with no way past it
    is a lockout. Backup codes are that way, and each is consumed on use --
    a reusable one is just a password that never expires.

No dependency: `hmac`, `hashlib` and `base64` are enough, and an authenticator
app only needs the `otpauth://` URI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: RFC 6238 defaults, and what every authenticator app assumes.
STEP_SECONDS = 30
DIGITS = 6

#: How many steps either side of now are accepted. Phone clocks drift and
#: people type slowly; zero tolerance produces "my code is right and it says
#: no" in the second either side of every boundary.
DRIFT_STEPS = 1

_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def new_secret(length: int = 32) -> str:
    """A fresh base32 secret, in the alphabet authenticator apps accept."""
    return "".join(secrets.choice(_B32_ALPHABET) for _ in range(length))


def code_at(secret: str, moment: int, *, step: int = STEP_SECONDS,
            digits: int = DIGITS) -> str:
    """The code for a given unix time."""
    key = base64.b32decode(_pad(secret), casefold=True)
    counter = struct.pack(">Q", int(moment) // step)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def provisioning_uri(secret: str, *, account: str, issuer: str = "ROMarr") -> str:
    """The `otpauth://` URI an authenticator scans."""
    label = quote(f"{issuer}:{account}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer, safe='')}&algorithm=SHA1"
            f"&digits={DIGITS}&period={STEP_SECONDS}")


def backup_codes(count: int = 10) -> list[str]:
    """Single-use codes for when the phone is gone.

    Grouped with a hyphen because they get written down, and a wall of
    undifferentiated hex is transcribed wrong.
    """
    out = set()
    while len(out) < count:
        raw = secrets.token_hex(5).upper()
        out.add(f"{raw[:5]}-{raw[5:]}")
    return sorted(out)


def _pad(secret: str) -> str:
    text = str(secret or "").strip().replace(" ", "").upper()
    return text + "=" * (-len(text) % 8)


def _normalise(code) -> str:
    """A code as typed: people add spaces, and backup codes carry hyphens."""
    return "".join(str(code or "").split()).replace("-", "").upper()


class Totp:
    """One user's second factor."""

    def __init__(self, secret: str = "", backup=None,
                 step: int = STEP_SECONDS, drift: int = DRIFT_STEPS):
        self.secret = str(secret or "")
        self.backup = list(backup or [])
        self.step = step
        self.drift = drift
        #: The last counter accepted, so a code cannot be used twice. Held in
        #: memory: a restart is a far smaller window than the 30 seconds this
        #: closes, and persisting it would mean a write on every login.
        self._used: int | None = None

    @property
    def enabled(self) -> bool:
        """No secret means two-factor is *off*, not open."""
        return bool(self.secret)

    def verify(self, code) -> bool:
        if not self.enabled:
            return False
        candidate = _normalise(code)
        if not candidate:
            return False

        # Backup codes first: they are not six digits, so they can never
        # collide with a real one, and checking them here means a spent code
        # is rejected rather than falling through to the TOTP comparison.
        for index, stored in enumerate(self.backup):
            if hmac.compare_digest(_normalise(stored), candidate):
                self.backup.pop(index)
                return True

        now = int(time.time())
        for offset in range(-self.drift, self.drift + 1):
            moment = now + offset * self.step
            counter = moment // self.step
            expected = code_at(self.secret, moment, step=self.step)
            if hmac.compare_digest(expected, candidate):
                if self._used is not None and counter <= self._used:
                    # Already spent. A code stays valid for its whole step
                    # plus the drift window, which is long enough for anyone
                    # who saw it to reuse it.
                    return False
                self._used = counter
                return True
        return False
