"""Credential encryption at rest.

The stakes are higher than the MCP tokens this replaced: these blobs hold Google
refresh tokens and Apple app-specific passwords. The failure that matters most is
not "encryption is weak", it is "the key changed and every account silently broke",
so most of these tests are about key resolution and how a bad key degrades.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app.services import secretstore
from app.services.secretstore import SecretDecryptError, SecretKeyError


@pytest.fixture(autouse=True)
def _fresh_key_cache():
    secretstore.reset_key_cache()
    yield
    secretstore.reset_key_cache()


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_the_original(self):
        payload = {"refresh_token": "1//abc", "access_token": "ya29.xyz", "expires_in": 3599}
        assert secretstore.decrypt(secretstore.encrypt(payload)) == payload

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        """The whole point. A grep of app.db must not yield the token."""
        blob = secretstore.encrypt({"refresh_token": "super-secret-value"})
        assert "super-secret-value" not in blob

    def test_encrypting_twice_gives_different_ciphertext(self):
        """Fernet includes an IV, so identical input must not be recognisable."""
        payload = {"token": "same"}
        assert secretstore.encrypt(payload) != secretstore.encrypt(payload)

    def test_empty_input_decrypts_to_an_empty_dict(self):
        """A row with no secret yet is normal, not an error."""
        assert secretstore.decrypt(None) == {}
        assert secretstore.decrypt("") == {}

    def test_the_envelope_records_the_key_id(self):
        """Stored beside the ciphertext so a future rotation can identify it."""
        envelope = json.loads(secretstore.encrypt({"a": 1}))
        key_id, _ = secretstore.load_key()
        assert envelope["key_id"] == key_id
        assert envelope["ct"]


class TestTampering:
    def test_a_mangled_ciphertext_raises_rather_than_returning_junk(self):
        blob = json.loads(secretstore.encrypt({"token": "value"}))
        blob["ct"] = blob["ct"][:-8] + "AAAAAAAA"
        with pytest.raises(SecretDecryptError):
            secretstore.decrypt(json.dumps(blob))

    def test_a_non_envelope_string_raises_the_typed_error(self):
        """Never a bare KeyError/ValueError -- callers branch on this type."""
        with pytest.raises(SecretDecryptError):
            secretstore.decrypt("not json at all")
        with pytest.raises(SecretDecryptError):
            secretstore.decrypt('{"no_ct_key": true}')

    def test_a_different_key_names_both_key_ids(self, monkeypatch):
        """The actionable case: the key was rotated or lost. The message has to
        say so, because the symptom is otherwise indistinguishable from data
        corruption."""
        blob = secretstore.encrypt({"token": "value"})
        original_id, _ = secretstore.load_key()

        monkeypatch.setenv("MMN_SECRET_KEY", "an-entirely-different-key")
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()
        new_id, _ = secretstore.load_key()

        with pytest.raises(SecretDecryptError) as exc:
            secretstore.decrypt(blob)
        assert original_id in str(exc.value)
        assert new_id in str(exc.value)


class TestKeyResolution:
    def test_an_arbitrary_string_is_accepted_as_key_material(self):
        """Demanding base64 would be a footgun; any passphrase must work."""
        key_id, key = secretstore.load_key()
        assert len(key_id) == 8
        # Whatever we derived has to be a usable Fernet key.
        assert secretstore.decrypt(secretstore.encrypt({"x": "y"})) == {"x": "y"}

    def test_a_real_fernet_key_is_used_verbatim(self, monkeypatch):
        from cryptography.fernet import Fernet

        generated = Fernet.generate_key().decode()
        monkeypatch.setenv("MMN_SECRET_KEY", generated)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()

        _, key = secretstore.load_key()
        assert key.decode() == generated

    def test_the_same_material_always_yields_the_same_key(self, monkeypatch):
        """Otherwise a restart would orphan every stored credential."""
        first_id, first = secretstore.load_key()
        secretstore.reset_key_cache()
        second_id, second = secretstore.load_key()
        assert (first_id, first) == (second_id, second)

    def test_it_generates_a_key_file_when_nothing_is_configured(
        self, monkeypatch, isolated_settings
    ):
        monkeypatch.delenv("MMN_SECRET_KEY", raising=False)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()

        secretstore.load_key()
        path = secretstore.key_path()
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, "must be created 0600, not chmod-ed after"

    def test_a_generated_key_survives_a_restart(self, monkeypatch):
        monkeypatch.delenv("MMN_SECRET_KEY", raising=False)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()

        blob = secretstore.encrypt({"token": "persisted"})
        secretstore.reset_key_cache()  # simulate a fresh process
        assert secretstore.decrypt(blob) == {"token": "persisted"}


class TestKeyRefusals:
    """Both of these are fatal on purpose: a wrong guess loses every credential
    at once, and does so silently until someone runs a match."""

    def test_a_group_readable_key_file_refuses_to_load(self, monkeypatch):
        monkeypatch.delenv("MMN_SECRET_KEY", raising=False)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()

        secretstore.load_key()  # generates it
        path = secretstore.key_path()
        os.chmod(path, 0o644)
        secretstore.reset_key_cache()

        with pytest.raises(SecretKeyError) as exc:
            secretstore.load_key()
        assert "chmod 600" in str(exc.value)

    def test_env_key_conflicting_with_a_key_file_refuses_to_load(self, monkeypatch):
        monkeypatch.delenv("MMN_SECRET_KEY", raising=False)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()
        secretstore.load_key()  # writes data/secret.key

        monkeypatch.setenv("MMN_SECRET_KEY", "a-different-key-entirely")
        reset_settings_cache()
        secretstore.reset_key_cache()

        with pytest.raises(SecretKeyError) as exc:
            secretstore.load_key()
        assert "different key" in str(exc.value)

    def test_an_env_key_matching_the_file_is_not_a_conflict(self, monkeypatch):
        """Belt-and-braces config (same key in both places) must still boot."""
        monkeypatch.delenv("MMN_SECRET_KEY", raising=False)
        from app.config import reset_settings_cache

        reset_settings_cache()
        secretstore.reset_key_cache()
        secretstore.load_key()
        material = secretstore.key_path().read_text().strip()

        monkeypatch.setenv("MMN_SECRET_KEY", material)
        reset_settings_cache()
        secretstore.reset_key_cache()

        assert secretstore.load_key()[1].decode() == material
