"""Password hashing, token hashing and the password policy."""

from __future__ import annotations

import pytest

from app.security import (
    DEFAULT_PARAMS,
    ScryptParams,
    hash_password,
    hash_token,
    needs_rehash,
    new_session_token,
    validate_password,
    verify_password,
)


def test_hash_then_verify_round_trip():
    creds = hash_password("correct horse battery")
    assert verify_password("correct horse battery", **creds) is True


def test_wrong_password_fails():
    creds = hash_password("correct horse battery")
    assert verify_password("Correct horse battery", **creds) is False
    assert verify_password("", **creds) is False


def test_salts_differ_between_users_with_the_same_password():
    a = hash_password("identical-password")
    b = hash_password("identical-password")
    assert a["password_salt"] != b["password_salt"]
    assert a["password_hash"] != b["password_hash"]


def test_hash_is_hex_of_the_declared_length():
    creds = hash_password("x" * 20)
    assert len(creds["password_hash"]) == 32 * 2
    assert len(creds["password_salt"]) == 16 * 2
    bytes.fromhex(creds["password_hash"])
    bytes.fromhex(creds["password_salt"])


def test_params_are_recorded_so_cost_can_be_raised_later():
    creds = hash_password("some-password")
    assert creds["password_algo"] == "scrypt"
    assert creds["password_params"] == DEFAULT_PARAMS


def test_verify_rejects_an_unknown_algorithm():
    creds = hash_password("some-password")
    creds["password_algo"] = "md5"
    assert verify_password("some-password", **creds) is False


def test_verify_survives_a_corrupt_salt():
    creds = hash_password("some-password")
    creds["password_salt"] = "not-hex"
    assert verify_password("some-password", **creds) is False


def test_verify_survives_corrupt_params():
    creds = hash_password("some-password")
    creds["password_params"] = "garbage"
    assert verify_password("some-password", **creds) is False


class TestScryptParams:
    def test_round_trip(self):
        p = ScryptParams(n=32768, r=8, p=2, dklen=64)
        assert ScryptParams.decode(p.encode()) == p

    def test_decode_fills_defaults(self):
        assert ScryptParams.decode("n=16384") == ScryptParams(n=16384)


class TestNeedsRehash:
    def test_current_params_do_not_need_rehashing(self):
        assert needs_rehash(DEFAULT_PARAMS) is False

    def test_weaker_params_need_rehashing(self):
        assert needs_rehash("n=1024,r=8,p=1,dklen=32") is True

    def test_foreign_algorithm_needs_rehashing(self):
        assert needs_rehash(DEFAULT_PARAMS, "bcrypt") is True

    def test_unparseable_params_need_rehashing(self):
        assert needs_rehash("???") is True


class TestSessionTokens:
    def test_tokens_are_unique(self):
        tokens = {new_session_token() for _ in range(200)}
        assert len(tokens) == 200

    def test_tokens_are_long_enough_to_be_unguessable(self):
        assert len(new_session_token()) >= 40

    def test_hash_token_is_stable_sha256_hex(self):
        assert hash_token("abc") == hash_token("abc")
        assert len(hash_token("abc")) == 64
        assert hash_token("abc") != hash_token("abd")

    def test_hash_token_is_not_reversible_to_the_raw_token(self):
        token = new_session_token()
        assert token not in hash_token(token)


class TestPasswordPolicy:
    def test_rejects_short_passwords(self):
        assert validate_password("short", min_length=10) is not None

    def test_accepts_a_long_enough_password(self):
        assert validate_password("a" * 10, min_length=10) is None

    def test_rejects_reusing_the_current_password(self):
        msg = validate_password("same-password", min_length=5, current="same-password")
        assert msg is not None
        assert "different" in msg

    def test_allows_a_different_password(self):
        assert validate_password("new-password", min_length=5, current="old-password") is None


@pytest.mark.parametrize("password", ["", " ", "ünïcödé-påsswörd", "a" * 512, "emoji 🔐 pw"])
def test_hashing_handles_awkward_inputs(password):
    creds = hash_password(password)
    assert verify_password(password, **creds) is True
