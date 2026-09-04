"""A set-but-EMPTY environment variable is not the same as an unset one (Tier 4.1 / 4.9).

`os.getenv("X", default)` returns the default only when X is *unset*. Set-but-blank returns
`""`, and `""` is falsy, empty, unparseable and — critically — NOT the default.

This is the normal case in a Kubernetes deploy, not an edge case. The Helm chart renders every
optional value through `| quote`, so an unconfigured setting arrives as `X=""` rather than
absent. Three failure shapes follow, and every one of them fires at MODULE IMPORT, before any
request handler or health check exists to report it:

  1. `create_engine("")` raises. `DATABASE_URL: ""` therefore produced CrashLoopBackOff with a
     SQLAlchemy stack trace that never named DATABASE_URL. And the "fixed" version — falling
     back to SQLite — is worse than crashing: a production pod would quietly write decisions
     and audit rows to a container-local file that dies with the pod, each replica with its own.
  2. `float(os.getenv("API_KEY_CACHE_TTL", "300"))` raises ValueError on "". Same shape:
     process dead at import, no useful log. Every `int(...)`/`float(...)` wrapper had it.
  3. The quiet ones. A blank RULEMIND_CONFIG_KEY derived the master encryption key from
     `sha256("")` — a publicly computable value protecting every tenant's stored secrets. A
     blank RULEMIND_ADMIN_PASSWORD seeded the admin account with an empty password. Neither
     crashes; both just run insecure.

The cookie half (4.9) is a different variable but the same class of "the default is wrong":
`_secure()` asked NODE_ENV, defaulting to "development", so a deployment that sets
RULEMIND_ENV=production (which the manifests do) shipped the httpOnly session cookie and its
CSRF partner WITHOUT `Secure` behind TLS.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from app import db as db_mod  # noqa: E402
from app import env as env_mod  # noqa: E402
from app import runtime as runtime_mod  # noqa: E402
from app import session_cookie  # noqa: E402
from app import sso as sso_mod  # noqa: E402
from app import storage as storage_mod  # noqa: E402
from app.auth import create_admin_jwt, decode_admin_jwt  # noqa: E402


class BlankEnvHelperTests(unittest.TestCase):
    """The helpers themselves. `env_int`/`env_float` must FALL BACK, never raise: a malformed
    number is a misconfiguration to log, not a reason to refuse to boot."""

    def test_blank_is_treated_as_absent(self):
        with mock.patch.dict(os.environ, {"RM_TEST_BLANK": ""}, clear=False):
            self.assertEqual(env_mod.env_str("RM_TEST_BLANK", "fallback"), "fallback")
            self.assertEqual(env_mod.env_int("RM_TEST_BLANK", 7), 7)
            self.assertEqual(env_mod.env_float("RM_TEST_BLANK", 1.5), 1.5)
            self.assertTrue(env_mod.env_bool("RM_TEST_BLANK", True))
            self.assertIsNone(env_mod.env_opt("RM_TEST_BLANK"))

    def test_whitespace_only_is_also_absent(self):
        # `DATABASE_URL: " "` from a YAML template is just as broken as `""`.
        with mock.patch.dict(os.environ, {"RM_TEST_BLANK": "   "}, clear=False):
            self.assertEqual(env_mod.env_str("RM_TEST_BLANK", "fallback"), "fallback")
            self.assertIsNone(env_mod.env_opt("RM_TEST_BLANK"))

    def test_env_list_drops_blank_entries(self):
        # This one carries SECRETS (RULEMIND_CONFIG_KEY_OLD). "a,,b," naively split yields two
        # empty strings, and an empty string derives a key anyone can compute — which would then
        # silently join the decryption chain.
        with mock.patch.dict(os.environ, {"RM_TEST_LIST": "a, ,b,"}, clear=False):
            self.assertEqual(env_mod.env_list("RM_TEST_LIST"), ["a", "b"])
        with mock.patch.dict(os.environ, {"RM_TEST_LIST": ""}, clear=False):
            self.assertEqual(env_mod.env_list("RM_TEST_LIST"), [])


class NoRawNumericEnvReadsTests(unittest.TestCase):
    """No `int(os.getenv(...))` / `float(os.getenv(...))` anywhere under `app/`.

    The helper tests above prove `env_int` falls back on a blank value. This one proves the
    codebase actually uses it. Twenty-four call sites still wrapped `os.getenv` in `int()` or
    `float()` directly -- `SANDBOX_POOL_SIZE`, `MODEL_TIMEOUT_MS`, `SCHEDULER_LEASE_TTL`,
    `RULEMIND_ACTION_MAX_TIMEOUT_MS` and the rest -- and most were at MODULE SCOPE, so a
    set-but-blank value took the process down at import with a ValueError that never named the
    variable. The Helm chart renders every optional value through `| quote`, so blank is what an
    unconfigured setting actually looks like in a deploy.

    A scan rather than one test per variable: the failure is a pattern, and enumerating call
    sites by hand is how twenty-four of them accumulated in the first place.
    """

    def test_no_int_or_float_wrapped_os_getenv_remains(self):
        import ast

        app_root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for source_file in sorted(app_root.rglob("*.py")):
            if source_file.name == "env.py":
                continue  # env.py is the thing that makes this safe; it reads os.environ itself
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id not in ("int", "float") or not node.args:
                    continue
                inner = node.args[0]
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "getenv"):
                    offenders.append("{0}:{1}".format(
                        source_file.relative_to(app_root.parent), node.lineno))

        self.assertEqual(offenders, [],
                         "use env.env_int / env.env_float -- a set-but-blank value makes these "
                         "raise ValueError, and at module scope that is a boot failure:\n  "
                         + "\n  ".join(offenders))


class DatabaseUrlTests(unittest.TestCase):
    """`DATABASE_URL=""` must not reach create_engine, and must not silently become SQLite in
    production."""

    def test_blank_database_url_does_not_reach_create_engine(self):
        # The original bug verbatim: DATABASE_URL set to "" (as `| quote` renders it) meant
        # os.getenv returned "" instead of the default, and create_engine("") raised at import.
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            url = db_mod.database_url()
        self.assertTrue(url.startswith("sqlite:///"), url)
        self.assertNotEqual(url, "")

    def test_unset_database_url_in_production_refuses_to_boot(self):
        # Falling back to SQLite here is the dangerous outcome, not the safe one: the pod comes
        # up healthy and writes production decisions to ephemeral local storage. Refuse, and say
        # which variable is missing.
        env = {"RULEMIND_ENV": "production"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            os.environ.pop(db_mod.ALLOW_SQLITE_VAR, None)
            with self.assertRaises(db_mod.DatabaseNotConfiguredError) as caught:
                db_mod.database_url()
        message = str(caught.exception)
        self.assertIn("DATABASE_URL", message)
        self.assertIn(db_mod.ALLOW_SQLITE_VAR, message)

    def test_blank_database_url_in_production_also_refuses(self):
        # Blank must be treated identically to unset -- the whole point of 4.1.
        with mock.patch.dict(os.environ, {"RULEMIND_ENV": "production", "DATABASE_URL": ""}, clear=False):
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            os.environ.pop(db_mod.ALLOW_SQLITE_VAR, None)
            with self.assertRaises(db_mod.DatabaseNotConfiguredError):
                db_mod.database_url()

    def test_sqlite_stays_the_default_for_local_dev(self):
        # ~1051 existing tests and every `pnpm dev` run rely on this. The fix must not make the
        # out-of-the-box path require configuration.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("RULEMIND_ENV", None)
            os.environ.pop("NODE_ENV", None)
            self.assertTrue(db_mod.database_url().startswith("sqlite:///"))

    def test_production_sqlite_is_available_by_explicit_opt_in(self):
        # A single-replica appliance install is a legitimate deployment. It just has to say so.
        env = {"RULEMIND_ENV": "production", db_mod.ALLOW_SQLITE_VAR: "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("DATABASE_URL", None)
            self.assertTrue(db_mod.database_url().startswith("sqlite:///"))

    def test_configured_database_url_wins_everywhere(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://u:p@h:5432/rm"}, clear=False):
            self.assertEqual(db_mod.database_url(), "postgresql+psycopg://u:p@h:5432/rm")


class SecureCookieTests(unittest.TestCase):
    """4.9: the Secure flag must follow RULEMIND_ENV, not NODE_ENV."""

    def test_production_via_rulemind_env_sets_secure(self):
        # This is the deployed configuration: the manifests set RULEMIND_ENV=production and say
        # nothing about NODE_ENV (it is a Node variable; this is a Python service). Under the old
        # `os.getenv("NODE_ENV", "development") != "development"` this returned False, so the
        # session cookie -- the one deliberately made unreadable to JS -- shipped without Secure
        # and would be sent over any plaintext http:// request to the same host.
        with mock.patch.dict(os.environ, {"RULEMIND_ENV": "production"}, clear=False):
            os.environ.pop("NODE_ENV", None)
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            self.assertTrue(session_cookie._secure())

    def test_rulemind_env_beats_a_stale_node_env(self):
        # Base images routinely leave NODE_ENV=development lying around. RULEMIND_ENV is the
        # deployment's own statement and must win.
        env = {"RULEMIND_ENV": "production", "NODE_ENV": "development"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            self.assertTrue(session_cookie._secure())

    def test_local_dev_stays_insecure_so_http_localhost_works(self):
        with mock.patch.dict(os.environ, {"RULEMIND_ENV": "development"}, clear=False):
            self.assertFalse(session_cookie._secure())

    def test_secure_flag_is_actually_emitted_on_the_cookies(self):
        from fastapi import Response

        with mock.patch.dict(os.environ, {"RULEMIND_ENV": "production"}, clear=False):
            os.environ.pop("NODE_ENV", None)
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            response = Response()
            session_cookie.set_session_cookies(response, "rms_token")
        headers = [value for key, value in response.raw_headers if key == b"set-cookie"]
        self.assertEqual(len(headers), 2)
        for header in headers:
            self.assertIn("Secure", header.decode("latin-1"))


class JwtSecretAgreementTests(unittest.TestCase):
    """A blank RULEMIND_ADMIN_JWT_SECRET made two readers of the SAME variable disagree."""

    def test_sso_state_and_admin_jwt_resolve_the_same_secret_when_blank(self):
        # auth.py already used env.env_str (-> dev fallback); sso.py used os.getenv (-> ""). So
        # with the variable set-but-empty, the admin session JWT was signed with the fallback
        # while the SSO login-state token was signed with the EMPTY STRING: a signing key any
        # attacker can guess, on the token that carries the OIDC/SAML nonce and redirect_uri.
        with mock.patch.dict(os.environ, {"RULEMIND_ADMIN_JWT_SECRET": ""}, clear=False):
            self.assertEqual(sso_mod._state_secret(), "rulemind-admin-dev-secret")
            self.assertNotEqual(sso_mod._state_secret(), "")
            # And end to end: a state token issued under the blank value must verify.
            token = sso_mod.issue_state("t1", "oidc", "nonce", "https://app/cb")
            self.assertEqual(sso_mod.verify_state(token)["t"], "t1")

    def test_admin_jwt_round_trips_under_a_blank_secret(self):
        with mock.patch.dict(os.environ, {"RULEMIND_ADMIN_JWT_SECRET": ""}, clear=False):
            claims = decode_admin_jwt(create_admin_jwt("u1", "a@b.com"))
        self.assertEqual(claims["email"], "a@b.com")


class BlankSecretDerivationTests(unittest.TestCase):
    """The quiet failures: blank values that produce a *working but insecure* system."""

    def test_blank_config_key_falls_back_instead_of_keying_on_empty_string(self):
        # sha256("") / PBKDF2("") are fixed, publicly computable values. Encrypting every
        # tenant's connector credentials and decision payloads under one is indistinguishable
        # from not encrypting them.
        with mock.patch.dict(os.environ, {"RULEMIND_CONFIG_KEY": ""}, clear=False):
            primary, _ = storage_mod._config_key_secrets()
        self.assertEqual(primary, storage_mod._CONFIG_KEY_DEFAULT)
        self.assertNotEqual(primary, "")

    def test_blank_api_key_cache_ttl_does_not_kill_the_process(self):
        # `float("")` raises ValueError at module scope. env_float falls back instead.
        with mock.patch.dict(os.environ, {"API_KEY_CACHE_TTL": ""}, clear=False):
            self.assertEqual(env_mod.env_float("API_KEY_CACHE_TTL", 300.0), 300.0)
        with mock.patch.dict(os.environ, {"API_KEY_CACHE_TTL": "not-a-number"}, clear=False):
            self.assertEqual(env_mod.env_float("API_KEY_CACHE_TTL", 300.0), 300.0)

    def test_blank_redis_url_does_not_permanently_latch_the_fallback(self):
        # REDIS_URL="" used to set _REDIS_UNAVAILABLE=True forever. "Not configured" is not a
        # failure, and latching it pinned the process to per-replica rate limits and a
        # single-replica decision bus for its whole life, with no way back short of a restart.
        runtime_mod.reset_redis_client_for_tests()
        try:
            with mock.patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
                self.assertIsNone(runtime_mod.redis_client())
            self.assertFalse(runtime_mod._REDIS_UNAVAILABLE)
        finally:
            runtime_mod.reset_redis_client_for_tests()


if __name__ == "__main__":
    unittest.main()
