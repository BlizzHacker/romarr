"""Two-factor for the local password login.

RFC 6238, so any authenticator app works and there is no dependency to add.
The interesting parts are not the algorithm -- that is thirty lines and the
RFC ships test vectors -- they are the three things implementations get wrong:
accepting a code twice, comparing it with `==`, and having no way in when the
phone is gone.
"""

from __future__ import annotations

import time

import pytest

from romarr.totp import (
    Totp, backup_codes, code_at, new_secret, provisioning_uri)


# --- RFC 6238 test vectors, so the algorithm is right and not merely ------
#     self-consistent

RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # "12345678901234567890"


@pytest.mark.parametrize("moment,expected", [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
])
def test_against_the_rfc_6238_vectors(moment, expected):
    assert code_at(RFC_SECRET_B32, moment) == expected


# --- secrets and enrolment -------------------------------------------------

def test_a_secret_is_base32_and_long_enough():
    secret = new_secret()
    assert len(secret) >= 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert secret != new_secret()


def test_the_provisioning_uri_is_what_an_authenticator_expects():
    uri = provisioning_uri("GEZDGNBVGY3TQOJQ", account="wade", issuer="ROMarr")
    assert uri.startswith("otpauth://totp/")
    assert "ROMarr" in uri and "wade" in uri
    assert "secret=GEZDGNBVGY3TQOJQ" in uri
    assert "issuer=ROMarr" in uri


def test_the_provisioning_uri_escapes_an_account_with_a_space():
    uri = provisioning_uri("AAAA", account="wade ivy", issuer="ROMarr")
    assert " " not in uri


# --- verification ----------------------------------------------------------

def test_the_current_code_is_accepted():
    totp = Totp(RFC_SECRET_B32)
    assert totp.verify(code_at(RFC_SECRET_B32, int(time.time())))


def test_a_wrong_code_is_not():
    assert not Totp(RFC_SECRET_B32).verify("000000")
    assert not Totp(RFC_SECRET_B32).verify("")
    assert not Totp(RFC_SECRET_B32).verify(None)


def test_one_step_of_drift_is_tolerated_in_both_directions():
    """Phone clocks drift and people type slowly. Zero tolerance produces
    "my code is right and it says no" during the second either side of every
    30-second boundary."""
    now = int(time.time())
    totp = Totp(RFC_SECRET_B32)
    assert totp.verify(code_at(RFC_SECRET_B32, now - 30))
    assert totp.verify(code_at(RFC_SECRET_B32, now + 30))


def test_two_steps_of_drift_is_not():
    now = int(time.time())
    totp = Totp(RFC_SECRET_B32)
    assert not totp.verify(code_at(RFC_SECRET_B32, now - 120))


def test_a_code_cannot_be_used_twice():
    """The replay window is real: a code is valid for 30 seconds plus the
    drift tolerance, so anybody who sees one -- over the shoulder, in a proxy
    log, in a screenshot -- can use it until it expires. Accepting it once is
    the whole point of the counter.
    """
    totp = Totp(RFC_SECRET_B32)
    code = code_at(RFC_SECRET_B32, int(time.time()))
    assert totp.verify(code)
    assert not totp.verify(code), "the same code must not work again"


def test_replay_rejection_does_not_block_the_next_code():
    now = int(time.time())
    totp = Totp(RFC_SECRET_B32)
    assert totp.verify(code_at(RFC_SECRET_B32, now))
    assert totp.verify(code_at(RFC_SECRET_B32, now + 30))


def test_codes_are_compared_in_constant_time():
    """A byte-by-byte `==` leaks how much of a code was right through timing.
    Six digits is a small enough space that this matters."""
    import inspect

    from romarr import totp as module
    assert "compare_digest" in inspect.getsource(module.Totp.verify)


def test_whitespace_and_hyphens_in_a_typed_code_are_tolerated():
    now = int(time.time())
    code = code_at(RFC_SECRET_B32, now)
    assert Totp(RFC_SECRET_B32).verify(f"{code[:3]} {code[3:]}")


# --- backup codes ----------------------------------------------------------

def test_backup_codes_are_generated_and_distinct():
    codes = backup_codes(10)
    assert len(codes) == 10 == len(set(codes))
    assert all(len(c) >= 8 for c in codes)


def test_a_backup_code_works_once_and_is_then_spent():
    """The way back in when the phone is gone. Reusable backup codes are a
    password that never expires, so each one is consumed."""
    codes = backup_codes(3)
    totp = Totp(RFC_SECRET_B32, backup=list(codes))
    assert totp.verify(codes[0])
    assert not totp.verify(codes[0])
    assert codes[0] not in totp.backup


def test_the_other_backup_codes_survive_one_being_used():
    codes = backup_codes(3)
    totp = Totp(RFC_SECRET_B32, backup=list(codes))
    totp.verify(codes[0])
    assert totp.verify(codes[1])


def test_a_backup_code_is_matched_regardless_of_case_and_spacing():
    codes = backup_codes(2)
    totp = Totp(RFC_SECRET_B32, backup=list(codes))
    assert totp.verify(codes[0].lower().replace("-", " "))


def test_no_secret_means_two_factor_is_off_not_open():
    """A user without 2FA enrolled must not have every code accepted."""
    totp = Totp("")
    assert not totp.verify("000000")
    assert not totp.enabled
