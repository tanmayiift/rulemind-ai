"""A wrong RULEMIND_CONFIG_KEY must fail LOUDLY, and the key must be rotatable (Tier 4.2).

Two bugs compounded into one incident shape.

FIRST: every decrypt helper returned a *plausible* value when decryption failed.

    decrypt_secret_text   -> returned THE CIPHERTEXT
    decrypt_model_blob    -> returned the raw ciphertext BYTES
    decrypt_config_payload-> returned {}
    decrypt_decision_field-> returned the raw value

All four were `except Exception: return <something>`. Because the return value looked like data,
nothing raised, nothing logged, and nothing 500'd. The garbage flowed straight outward through
six consumers: a connector's auth header, an LLM provider key, an SMTP password, an SSO client
secret, a webhook signing secret, and -- worst -- a model blob handed to `pickle.loads`.
`decrypt_config_payload` returning `{}` is its own trap: the connector kept working, just with
no configured endpoint and no credentials, so the operator saw confusing third-party 401s
rather than "the key is wrong".

SECOND: the key was unrotatable. `fernet_key()` derived exactly ONE key and every decrypt used
that key alone -- MultiFernet appears nowhere in the repo. So changing RULEMIND_CONFIG_KEY, the
standard response to a suspected key compromise, orphaned every encrypted column at once.

Together: rotating the key did not fail. It "succeeded", and then quietly destroyed access to
every stored secret while continuing to serve traffic.

THIRD, and the reason these tests are careful: key derivation was a bare `sha256(secret)`. It
is now PBKDF2-HMAC-SHA256, which derives a DIFFERENT key from the same secret. Every row
already in a database was written under the old derivation. If the sha256 key is not kept in
the decryption chain, upgrading the code silently renders all existing ciphertext unreadable --
with no error at write time, because writes succeed under the new key regardless.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402

from app import storage as storage_mod  # noqa: E402
from app.models import Connector, HostedModel, Tenant, Webhook  # noqa: E402
from app.storage import Storage  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _legacy_token(secret: str, plaintext: bytes) -> str:
    """A ciphertext exactly as the pre-KDF code would have written it."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key).encrypt(plaintext).decode("utf-8")


class _KeyEnv:
    """Set the key env vars and clear the derived-key chain cache around a block."""

    def __init__(self, primary: str, old: str = ""):
        self._patch = mock.patch.dict(
            os.environ,
            {storage_mod.CONFIG_KEY_VAR: primary, storage_mod.CONFIG_KEY_OLD_VAR: old},
            clear=False,
        )

    def __enter__(self):
        self._patch.__enter__()
        storage_mod._KEY_CHAIN_CACHE.clear()
        return self

    def __exit__(self, *exc):
        self._patch.__exit__(*exc)
        storage_mod._KEY_CHAIN_CACHE.clear()
        return False


class WrongKeyFailsLoudlyTests(unittest.TestCase):
    """Each of the four helpers, given ciphertext it cannot open, must raise rather than return
    something that reads like data."""

    def setUp(self):
        with _KeyEnv("original-master-key"):
            self.secret_token = storage_mod.encrypt_secret_text("sk-ant-super-secret")
            self.config_token = storage_mod.encrypt_config_payload({"base_url": "https://bureau", "token": "t"})
            self.decision_token = storage_mod.encrypt_decision_field({"pan": "ABCDE1234F"})
            self.model_token = storage_mod.encrypt_model_blob(b"\x80\x04pickled-model")

    def test_secret_text_raises_instead_of_returning_the_ciphertext(self):
        # The old behaviour sent this base64 blob out as a live `Authorization:` header / SMTP
        # password / OIDC client_secret. The only symptom was a 401 from a third party.
        with _KeyEnv("a-different-key"):
            with self.assertRaises(storage_mod.DecryptionError) as caught:
                storage_mod.decrypt_secret_text(self.secret_token)
        message = str(caught.exception)
        self.assertIn(storage_mod.CONFIG_KEY_VAR, message)
        self.assertIn(storage_mod.CONFIG_KEY_OLD_VAR, message)
        # And the message must not itself leak the ciphertext it refused to hand back.
        self.assertNotIn(self.secret_token, message)

    def test_config_payload_raises_instead_of_returning_an_empty_dict(self):
        # `{}` meant every connector silently lost its endpoint and credentials while continuing
        # to be "configured" as far as the UI and the executor were concerned.
        with _KeyEnv("a-different-key"):
            with self.assertRaises(storage_mod.DecryptionError):
                storage_mod.decrypt_config_payload(self.config_token)

    def test_decision_field_raises_instead_of_returning_the_raw_value(self):
        with _KeyEnv("a-different-key"):
            with self.assertRaises(storage_mod.DecryptionError):
                storage_mod.decrypt_decision_field(self.decision_token)

    def test_model_blob_raises_instead_of_feeding_ciphertext_to_pickle(self):
        # `pickle.loads(<ciphertext>)` is the consumer. Handing a deserialiser bytes that are
        # neither the intended payload nor validated is exactly the shape you never want.
        with _KeyEnv("a-different-key"):
            with self.assertRaises(storage_mod.DecryptionError):
                storage_mod.decrypt_model_blob(self.model_token)

    def test_correct_key_still_round_trips(self):
        with _KeyEnv("original-master-key"):
            self.assertEqual(storage_mod.decrypt_secret_text(self.secret_token), "sk-ant-super-secret")
            self.assertEqual(storage_mod.decrypt_config_payload(self.config_token)["token"], "t")
            self.assertEqual(storage_mod.decrypt_decision_field(self.decision_token)["pan"], "ABCDE1234F")
            self.assertEqual(storage_mod.decrypt_model_blob(self.model_token), b"\x80\x04pickled-model")


class DeliberateLegacyPathTests(unittest.TestCase):
    """The backward-compat paths that must SURVIVE the fail-loud change -- narrow and explicit,
    keyed on the Fernet version marker rather than on 'an exception happened'."""

    def test_pre_encryption_plaintext_secret_still_reads(self):
        with _KeyEnv("any-key"):
            self.assertEqual(storage_mod.decrypt_secret_text("plain-old-password"), "plain-old-password")

    def test_pre_encryption_json_connector_config_still_reads(self):
        with _KeyEnv("any-key"):
            self.assertEqual(storage_mod.decrypt_config_payload('{"base_url": "https://x"}'),
                             {"base_url": "https://x"})

    def test_pre_encryption_decision_rows_still_read(self):
        with _KeyEnv("any-key"):
            # A JSON column round-trips the old plaintext as a real dict/list...
            self.assertEqual(storage_mod.decrypt_decision_field({"amount": 42}), {"amount": 42})
            self.assertEqual(storage_mod.decrypt_decision_field([{"rule_id": "r1"}]), [{"rule_id": "r1"}])
            # ...or, on some backends, as a JSON string.
            self.assertEqual(storage_mod.decrypt_decision_field('{"amount": 42}'), {"amount": 42})
            self.assertIsNone(storage_mod.decrypt_decision_field(None))

    def test_pre_encryption_model_blob_still_reads(self):
        with _KeyEnv("any-key"):
            self.assertEqual(storage_mod.decrypt_model_blob(b"\x80\x04raw-pickle"), b"\x80\x04raw-pickle")

    def test_legacy_path_is_gated_on_the_marker_not_on_an_exception(self):
        # The distinction that makes fail-loud possible: something that IS ciphertext raises,
        # something that never was ciphertext is returned. A blanket `except` cannot tell them
        # apart, which is precisely why the old code returned garbage.
        with _KeyEnv("key-one"):
            token = storage_mod.encrypt_secret_text("v")
        self.assertTrue(token.startswith(storage_mod._FERNET_PREFIX))
        with _KeyEnv("key-two"):
            with self.assertRaises(storage_mod.DecryptionError):
                storage_mod.decrypt_secret_text(token)
            self.assertEqual(storage_mod.decrypt_secret_text("gABBBB-not-a-token"), "gABBBB-not-a-token")


class KdfMigrationTests(unittest.TestCase):
    """The dangerous half of this change: PBKDF2 derives a different key from the same secret."""

    def test_kdf_key_differs_from_the_old_sha256_key(self):
        # If these were equal the migration risk would not exist -- assert the premise so the
        # rest of this class cannot pass vacuously.
        with _KeyEnv("some-secret"):
            self.assertNotEqual(storage_mod.fernet_key(), storage_mod._legacy_sha256_key("some-secret"))

    def test_data_written_under_the_old_sha256_derivation_still_decrypts(self):
        # THE data-loss test. Every row in every existing deployment was encrypted this way.
        token = _legacy_token("prod-master-key", b"smtp-password")
        with _KeyEnv("prod-master-key"):
            self.assertEqual(storage_mod.decrypt_secret_text(token), "smtp-password")

    def test_new_writes_use_the_kdf_key_not_the_legacy_one(self):
        # The legacy derivation is a DECRYPTION-only fallback. If encryption kept using it, the
        # upgrade would be cosmetic.
        with _KeyEnv("prod-master-key"):
            token = storage_mod.encrypt_secret_text("fresh")
            legacy_only = Fernet(storage_mod._legacy_sha256_key("prod-master-key"))
            with self.assertRaises(InvalidToken):
                legacy_only.decrypt(token.encode("utf-8"))
            self.assertEqual(Fernet(storage_mod.fernet_key()).decrypt(token.encode("utf-8")), b"fresh")


class KeyChainRotationTests(unittest.TestCase):
    """MultiFernet: decrypt tries every configured key, encrypt uses only the primary."""

    def test_old_key_in_the_chain_keeps_reads_working_during_a_rotation(self):
        with _KeyEnv("old-key"):
            token = storage_mod.encrypt_secret_text("during-rotation")
        # Step 2 of the documented rotation: new primary, old key still listed. Reads work.
        with _KeyEnv("new-key", old="old-key"):
            self.assertEqual(storage_mod.decrypt_secret_text(token), "during-rotation")
        # Step 4: drop the old key before re-encrypting -> the data is unreachable, and says so.
        with _KeyEnv("new-key"):
            with self.assertRaises(storage_mod.DecryptionError):
                storage_mod.decrypt_secret_text(token)

    def test_several_old_keys_are_accepted(self):
        with _KeyEnv("key-a"):
            token_a = storage_mod.encrypt_secret_text("a")
        with _KeyEnv("key-b"):
            token_b = storage_mod.encrypt_secret_text("b")
        with _KeyEnv("key-c", old="key-a, key-b"):
            self.assertEqual(storage_mod.decrypt_secret_text(token_a), "a")
            self.assertEqual(storage_mod.decrypt_secret_text(token_b), "b")

    def test_encryption_always_uses_the_primary_key(self):
        with _KeyEnv("primary", old="secondary"):
            token = storage_mod.encrypt_secret_text("x")
        with _KeyEnv("primary"):
            self.assertEqual(storage_mod.decrypt_secret_text(token), "x")


class RotationScriptTests(unittest.TestCase):
    """scripts/rotate_config_key.py: re-encrypts everything, resumable, safe to run twice."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tempdir.name, "rotate.db")
        self.state_path = os.path.join(self.tempdir.name, "state.json")
        # Seed a workspace with something in every encrypted column, under the OLD key.
        with _KeyEnv("old-master-key"):
            storage = Storage(path=self.db_path, force_sqlite=True)
            self.tenant_id = storage.default_tenant_id
            storage.set_email_config({"host": "smtp.acme", "from_addr": "a@acme",
                                      "password": "smtp-secret"}, tenant_id=self.tenant_id)
            storage.set_sso_config({"provider": "oidc", "client_secret": "oidc-secret"},
                                   tenant_id=self.tenant_id)
            storage.set_ai_config({"anthropic": {"key": "sk-ant-rotate"}}, tenant_id=self.tenant_id)
            policy = storage.list_policies(tenant_id=self.tenant_id)[0]
            storage.create_webhook({"policy_id": policy["id"], "endpoint_path": "/hooks/x",
                                    "secret_hash": "whsec-rotate"}, tenant_id=self.tenant_id)
            storage.create_model({"id": "m1", "name": "M", "model_blob": b"\x80\x04model-bytes"},
                                 tenant_id=self.tenant_id)
            storage.add_decision({"policy_id": policy["id"], "outcome": "approve",
                                  "payload": {"pan": "ABCDE1234F"},
                                  "trace": [{"step": {"type": "rule"}, "input": {"pan": "ABCDE1234F"}}]},
                                 tenant_id=self.tenant_id)
        self.storage = storage

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, primary, old, *extra):
        env = dict(os.environ)
        env.update({
            storage_mod.CONFIG_KEY_VAR: primary,
            storage_mod.CONFIG_KEY_OLD_VAR: old,
            # The rotation script is invoked as a subprocess against this literal
            # sqlite URL, so every Storage in this suite is pinned to the same file
            # with force_sqlite -- the Postgres lane would otherwise leave it empty.
            "DATABASE_URL": "sqlite:///{0}".format(self.db_path),
            "RULEMIND_SEED_DEMO": "0",
        })
        return subprocess.run(
            [sys.executable, "-m", "scripts.rotate_config_key", "--state-file", self.state_path, *extra],
            cwd=str(APP_ROOT), env=env, capture_output=True, text=True, timeout=300,
        )

    def _rows_readable_under(self, primary, old=""):
        with _KeyEnv(primary, old=old):
            storage = Storage(path=self.db_path, force_sqlite=True)
            self.assertEqual(storage.get_email_credentials(tenant_id=self.tenant_id)["password"], "smtp-secret")
            self.assertEqual(storage.get_sso_config_internal(tenant_id=self.tenant_id)["client_secret"], "oidc-secret")
            self.assertEqual(storage.get_ai_credentials("anthropic", tenant_id=self.tenant_id)["api_key"], "sk-ant-rotate")
            model = storage.get_model("m1", include_blob=True, tenant_id=self.tenant_id)
            self.assertEqual(model["model_blob"], b"\x80\x04model-bytes")
            decision = storage.list_decisions(tenant_id=self.tenant_id)[0]
            self.assertEqual(decision["payload"]["pan"], "ABCDE1234F")
            self.assertEqual(decision["trace"][0]["input"]["pan"], "ABCDE1234F")
            with storage.connect() as session:
                hook = session.scalar(select(Webhook).where(Webhook.tenant_id == self.tenant_id))
                self.assertEqual(storage_mod.decrypt_secret_text(hook.secret_hash), "whsec-rotate")
                connector = session.scalar(select(Connector).where(Connector.tenant_id == self.tenant_id))
                if connector is not None:
                    storage_mod.decrypt_config_payload(connector.encrypted_config)

    def test_rotation_moves_every_column_onto_the_new_key(self):
        result = self._run("new-master-key", "old-master-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The point of the whole exercise: the OLD key can now be retired and everything reads.
        self._rows_readable_under("new-master-key")

    def test_before_rotation_the_new_key_alone_cannot_read_the_data(self):
        # Guards against a vacuous pass above -- proves the rotation is what made it readable,
        # not that the data was somehow key-independent.
        with _KeyEnv("new-master-key"):
            storage = Storage(path=self.db_path, force_sqlite=True)
            with self.assertRaises(storage_mod.DecryptionError):
                storage.get_email_credentials(tenant_id=self.tenant_id)

    def test_running_twice_is_a_no_op_rather_than_a_corruption(self):
        self.assertEqual(self._run("new-master-key", "old-master-key").returncode, 0)
        # --restart discards the checkpoint, so the second pass genuinely RE-SCANS every table
        # with the old key now retired. That is the real idempotency question: does the script
        # recognise already-rotated ciphertext and leave it alone, or does it try to decrypt it
        # with a chain that no longer contains the key it was written under?
        second = self._run("new-master-key", "", "--restart")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("0 row(s) re-encrypted", second.stdout + second.stderr)
        self._rows_readable_under("new-master-key")

    def test_checkpoint_records_progress_and_a_resume_finishes_the_job(self):
        # --batch-size 1 forces many pages, so the checkpoint is exercised rather than written
        # once at the end.
        first = self._run("new-master-key", "old-master-key", "--batch-size", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        state = json.loads(Path(self.state_path).read_text("utf-8"))
        self.assertEqual(set(state["done"]), {name for name, _ in __import__(
            "scripts.rotate_config_key", fromlist=["TABLES"]).TABLES})
        # Now simulate a run interrupted partway through `decisions`: drop it from `done` and
        # rewind its cursor. The resume must skip the finished tables, re-scan decisions, find
        # the rows already on the new key, and exit cleanly.
        state["done"] = [name for name in state["done"] if name != "decisions"]
        state["cursors"].pop("decisions", None)
        Path(self.state_path).write_text(json.dumps(state), "utf-8")
        resumed = self._run("new-master-key", "", "--batch-size", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = resumed.stdout + resumed.stderr
        self.assertIn("already complete", output)          # the finished tables were skipped
        self.assertIn("0 row(s) re-encrypted", output)     # decisions were already done
        self._rows_readable_under("new-master-key")

    def test_dry_run_changes_nothing(self):
        result = self._run("new-master-key", "old-master-key", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout + result.stderr)
        # Still only readable with the old key in the chain.
        with _KeyEnv("new-master-key"):
            storage = Storage(path=self.db_path, force_sqlite=True)
            with self.assertRaises(storage_mod.DecryptionError):
                storage.get_email_credentials(tenant_id=self.tenant_id)

    def test_migrates_legacy_sha256_data_onto_the_kdf_key_without_changing_the_secret(self):
        # A supported invocation: same secret, no old key -- upgrade the derivation in place.
        with self.storage.connect() as session:
            row = session.scalar(select(Tenant).where(Tenant.id == self.tenant_id))
            config = json.loads(json.dumps(row.config or {}))
            config.setdefault("email", {})["password_encrypted"] = _legacy_token("old-master-key", b"legacy-pw")
            row.config = config
        result = self._run("old-master-key", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        with _KeyEnv("old-master-key"):
            storage = Storage(path=self.db_path, force_sqlite=True)
            self.assertEqual(storage.get_email_credentials(tenant_id=self.tenant_id)["password"], "legacy-pw")
            with storage.connect() as session:
                row = session.scalar(select(Tenant).where(Tenant.id == self.tenant_id))
                token = row.config["email"]["password_encrypted"]
            # Now on the KDF key: the legacy key alone must no longer open it.
            with self.assertRaises(InvalidToken):
                Fernet(storage_mod._legacy_sha256_key("old-master-key")).decrypt(token.encode("utf-8"))

    def test_hosted_model_blob_is_rotated_too(self):
        self.assertEqual(self._run("new-master-key", "old-master-key").returncode, 0)
        with _KeyEnv("new-master-key"):
            storage = Storage(path=self.db_path, force_sqlite=True)
            with storage.connect() as session:
                row = session.scalar(select(HostedModel).where(HostedModel.tenant_id == self.tenant_id))
                blob = row.model_blob
            self.assertTrue(blob.startswith(storage_mod._FERNET_PREFIX_BYTES))
            self.assertEqual(storage_mod.decrypt_model_blob(blob), b"\x80\x04model-bytes")


if __name__ == "__main__":
    unittest.main()
