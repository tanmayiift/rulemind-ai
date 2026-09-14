"""The sandbox validation cache must not be collidable, and must not grow without bound (T2.5).

validate_source content-addresses the source and returns early on a cache hit -- "this exact
body already passed". The key was md5(source). Chosen-prefix MD5 collisions are cheap and
public, so the attack is: submit a benign body, let it seed the cache, then submit a malicious
body engineered to the same md5 -- validation is skipped and the malicious code runs. Switching
to SHA-256 removes the construction.

Separately, all four code caches were global unbounded dicts shared across tenants, so every
distinct variable body ever submitted was a permanent entry -- a slow leak with no ceiling.
"""
from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import sandbox  # noqa: E402


class CacheKeyTest(unittest.TestCase):
    def test_the_cache_key_is_sha256_not_md5(self):
        source = "result = payload.get('age', 0)"
        key = sandbox._source_key(source)
        self.assertEqual(key, hashlib.sha256(source.encode()).hexdigest())
        self.assertNotEqual(key, hashlib.md5(source.encode()).hexdigest())  # noqa: S324
        self.assertEqual(len(key), 64, "sha-256 hex is 64 chars; md5 is 32")

    def test_no_md5_anywhere_in_the_module(self):
        text = Path(sandbox.__file__).read_text()
        self.assertNotIn("hashlib.md5", text,
                         "an md5 cache key is a validation-skip primitive under a collision")

    def test_validation_is_keyed_by_content_so_distinct_bodies_are_validated_independently(self):
        """The property a collision would break: two different sources must not share a verdict.

        A genuine md5 collision pair is expensive to carry in a test, so this asserts the
        invariant the collision violated -- distinct sources get distinct keys, and a malicious
        body is validated on its own merits regardless of what benign body preceded it.
        """
        benign = "result = 1 + 1"
        malicious = "import os\nresult = os.getcwd()"
        sandbox.validate_source(benign)  # seeds the cache
        self.assertNotEqual(sandbox._source_key(benign), sandbox._source_key(malicious))
        with self.assertRaises(ValueError):
            sandbox.validate_source(malicious)


class CacheBoundTest(unittest.TestCase):
    def test_the_validation_cache_is_bounded(self):
        cache = sandbox._BoundedCache(capacity=3)
        for i in range(10):
            cache["k{0}".format(i)] = True
        self.assertLessEqual(len(cache), 3,
                             "the code cache grew past its capacity -- unbounded per-tenant leak")

    def test_it_evicts_oldest_first(self):
        cache = sandbox._BoundedCache(capacity=2)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3  # should evict "a"
        self.assertNotIn("a", cache)
        self.assertIn("b", cache)
        self.assertIn("c", cache)

    def test_updating_an_existing_key_does_not_evict(self):
        cache = sandbox._BoundedCache(capacity=2)
        cache["a"] = 1
        cache["b"] = 2
        cache["a"] = 99  # re-set, not a new key
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache["a"], 99)
        self.assertIn("b", cache)

    def test_the_real_caches_are_bounded_instances(self):
        for name in ("_validated_code_cache", "_compiled_code_cache",
                     "_param_count_cache", "_function_cache"):
            with self.subTest(cache=name):
                self.assertIsInstance(getattr(sandbox, name), sandbox._BoundedCache,
                                      "{0} is still an unbounded dict".format(name))


if __name__ == "__main__":
    unittest.main()
