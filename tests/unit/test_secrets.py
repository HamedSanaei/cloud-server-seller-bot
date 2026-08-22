"""Tests for the encrypted credential envelope (core.secrets)."""

from __future__ import annotations

import base64

import pytest

from cloud_platform.core.secrets import (
    EnvelopeService,
    FernetSecretBox,
    MasterKey,
    SecretBoxError,
    SecretEnvelope,
    master_key_from_env,
)


class TestMasterKey:
    def test_generate_is_32_bytes_and_unique(self) -> None:
        k1 = MasterKey.generate()
        k2 = MasterKey.generate()
        assert len(k1.material) == 32
        assert k1.material != k2.material

    def test_from_passphrase_is_deterministic(self) -> None:
        k1 = MasterKey.from_passphrase("hunter2")
        k2 = MasterKey.from_passphrase("hunter2")
        assert k1 == k2
        assert len(k1.material) == 32

    def test_from_passphrase_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            MasterKey.from_passphrase("")

    def test_material_length_enforced(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            MasterKey(material=b"short")

    def test_repr_never_leaks_material(self) -> None:
        key = MasterKey.from_passphrase("super-secret-passphrase")
        text = repr(key) + str(key)
        assert "super-secret-passphrase" not in text
        assert key.material.hex() not in text


class TestFernetSecretBox:
    def test_round_trip_preserves_unicode(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        envelope = box.encrypt("tokēn-секрет-🔑")
        assert box.decrypt(envelope) == "tokēn-секрет-🔑"

    def test_ciphertext_differs_from_plaintext(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        plaintext = "plaintext-hetzner-token"
        envelope = box.encrypt(plaintext)
        assert isinstance(envelope, SecretEnvelope)
        assert envelope.token != plaintext
        assert plaintext not in envelope.token

    def test_envelope_repr_redacts_token(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        envelope = box.encrypt("visible-secret-value")
        text = repr(envelope) + str(envelope)
        assert "visible-secret-value" not in text
        assert envelope.token not in text

    def test_key_id_is_stable_fingerprint(self) -> None:
        key = MasterKey.from_passphrase("same")
        assert FernetSecretBox(key).key_id == FernetSecretBox(key).key_id
        other = FernetSecretBox(MasterKey.generate())
        assert FernetSecretBox(key).key_id != other.key_id

    def test_decrypt_with_wrong_key_raises_secret_box_error(self) -> None:
        envelope = FernetSecretBox(MasterKey.generate()).encrypt("payload")
        wrong_box = FernetSecretBox(MasterKey.generate())
        with pytest.raises(SecretBoxError):
            wrong_box.decrypt(envelope)

    def test_tampered_token_raises_secret_box_error(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        envelope = box.encrypt("payload")
        tampered_chars = "A" if envelope.token[0] != "A" else "B"
        tampered = SecretEnvelope(
            token=tampered_chars + envelope.token[1:],
            key_id=envelope.key_id,
        )
        with pytest.raises(SecretBoxError):
            box.decrypt(tampered)

    def test_unsupported_algorithm_rejected(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        bogus = SecretEnvelope(token="x", key_id=box.key_id, algorithm="ROT13")
        with pytest.raises(SecretBoxError, match="algorithm"):
            box.decrypt(bogus)

    def test_encrypt_accepts_port_symmetry_kwarg(self) -> None:
        box = FernetSecretBox(MasterKey.generate())
        envelope = box.encrypt("value", key_id="ignored-for-single-key-box")
        assert box.decrypt(envelope) == "value"


class TestEnvelopeService:
    def test_seal_open_round_trip(self) -> None:
        service = EnvelopeService(FernetSecretBox(MasterKey.generate()))
        envelope = service.seal("hetzner-main", "hmem-token")
        assert service.open("hetzner-main", envelope) == "hmem-token"

    def test_rotate_rekeys_envelope(self) -> None:
        old_box = FernetSecretBox(MasterKey.generate())
        new_box = FernetSecretBox(MasterKey.generate())
        service = EnvelopeService(new_box)
        old_envelope = old_box.encrypt("rotatable-secret")

        new_envelope = service.rotate(old_box, new_box, old_envelope)

        assert new_envelope.token != old_envelope.token
        assert new_envelope.key_id == new_box.key_id
        assert new_box.decrypt(new_envelope) == "rotatable-secret"
        with pytest.raises(SecretBoxError):
            old_box.decrypt(new_envelope)


class TestEnvMasterKey:
    def test_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        material = MasterKey.generate().material
        encoded = base64.urlsafe_b64encode(material).decode("ascii")
        monkeypatch.setenv("CLOUD_PLATFORM_SECRET_MASTER_KEY", encoded)
        assert master_key_from_env().material == material

    def test_missing_env_var_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLOUD_PLATFORM_SECRET_MASTER_KEY", raising=False)
        with pytest.raises(SecretBoxError, match="CLOUD_PLATFORM_SECRET_MASTER_KEY"):
            master_key_from_env()

    def test_invalid_base64_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLOUD_PLATFORM_SECRET_MASTER_KEY", "!!!not-base64!!!")
        with pytest.raises(SecretBoxError, match="base64"):
            master_key_from_env()

    def test_wrong_length_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        short = base64.urlsafe_b64encode(b"too-short").decode("ascii")
        monkeypatch.setenv("CLOUD_PLATFORM_SECRET_MASTER_KEY", short)
        with pytest.raises(SecretBoxError, match="32 bytes"):
            master_key_from_env()
