"""The config-surface inventory and its gate.

Why this file exists, stated plainly because the gate is only as good as the parse underneath
it: `scripts/config_surface.py` decides which settings the backend reads by walking the AST of
every module under `app/`. If that walk silently misses a form -- `os.environ["X"]`, a helper
called as `env.env_bool(...)` rather than `env_bool(...)` -- then the settings it misses are
invisible to the gate, the gate passes, and "every setting is reachable" becomes a claim rather
than a fact. That is the exact failure this project keeps finding in its own checks, so the
parse is tested against each form separately.

The numbers that motivated it, measured 2026-09-24: 111 settings read, 27 deliverable by a
named key in an artifact, 84 settable by nothing -- including DECISION_ENCRYPT_AT_REST,
AUDIT_ENCRYPT_AT_REST and LOG_LEVEL.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))

import config_surface  # noqa: E402


class InventoryParseTests(unittest.TestCase):
    """Each reader form, separately. A form the walk cannot see is a setting the gate cannot
    protect, and nothing else in the repo would notice."""

    def _parse(self, source: str):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mod.py").write_text(source, encoding="utf-8")
            return config_surface.read_settings(tmp)

    def test_os_getenv_with_default(self):
        found = self._parse('import os\nx = os.getenv("A_SETTING", "7")\n')
        self.assertIn("A_SETTING", found)
        self.assertIn("7", found["A_SETTING"].defaults)

    def test_an_alias_of_os_environ(self):
        """The archiver's `settings = env if env is not None else os.environ` hid thirteen settings,
        and mtls.py / security_config.py / quota.py hid ten more, including the mutual-TLS config
        and the production secret-check escape hatches."""
        found = self._parse('import os\ndef f(env=None):\n    settings = env if env is not None else os.environ\n'
                            '    return settings.get("ALIASED_SETTING", "x")\n')
        self.assertIn("ALIASED_SETTING", found)

    def test_an_ordinary_dict_get_is_not_a_setting(self):
        """The same file also reads `settings.get("audit_retention_months")` off a plain dict; a
        lower-case key is not an environment variable, so it must not be inventoried."""
        found = self._parse('import os\nsettings = os.environ\nsettings.get("audit_retention_months")\n'
                            'other = {}\nother.get("NOT_AN_ALIAS")\n')
        self.assertNotIn("audit_retention_months", found)
        self.assertNotIn("NOT_AN_ALIAS", found)

    def test_os_environ_get(self):
        self.assertIn("B_SETTING", self._parse('import os\nos.environ.get("B_SETTING")\n'))

    def test_os_environ_subscript(self):
        """`os.environ["X"]` is a READ that raises when unset -- a required setting, and the
        form most likely to be missed because it is not a call."""
        self.assertIn("C_SETTING", self._parse('import os\nos.environ["C_SETTING"]\n'))

    def test_env_helpers_bare_and_attribute(self):
        """app/env.py's helpers are imported both ways across the codebase."""
        bare = self._parse('from .env import env_bool\nenv_bool("D_SETTING", True)\n')
        self.assertIn("D_SETTING", bare)
        attr = self._parse('from . import env\nenv.env_int("E_SETTING", 5)\n')
        self.assertIn("E_SETTING", attr)
        self.assertIn("5", attr["E_SETTING"].defaults)

    def test_a_non_literal_name_is_not_invented(self):
        """`os.getenv(name)` names no specific setting. Recording something here would put a
        variable in the inventory that no operator can ever satisfy, and the gate would then
        demand it be documented."""
        found = self._parse('import os\nname = "X"\nos.getenv(name)\nos.getenv(f"P_{name}")\n')
        self.assertEqual(found, {})

    def test_every_call_site_is_recorded_not_just_the_first(self):
        found = self._parse('import os\nos.getenv("F_SETTING")\nos.getenv("F_SETTING", "2")\n')
        self.assertEqual(len(found["F_SETTING"].readers), 2)

    def test_shell_readers_are_counted(self):
        """RULEMIND_WORKERS is read by the Dockerfile CMD, not by Python. A Python-only
        inventory reported the single largest throughput lever as 'not read'."""
        settings = config_surface.build(str(REPO))
        self.assertIn("RULEMIND_WORKERS", settings)
        self.assertTrue(any("shell" in reader
                            for reader in settings["RULEMIND_WORKERS"].readers))


class DeliverabilityTests(unittest.TestCase):
    def setUp(self):
        self.settings = config_surface.build(str(REPO))

    def test_documentation_is_not_delivery(self):
        """The distinction the whole gate rests on. `.env.example` documented 68 settings that
        no artifact could deliver -- an operator could read about encryption at rest and had no
        way to switch it on."""
        entry = self.settings["DECISION_ENCRYPT_AT_REST"]
        self.assertTrue(entry.documented)
        self.assertEqual(entry.delivered_by, set(),
                         "DECISION_ENCRYPT_AT_REST now has a NAMED key in an artifact -- if that "
                         "is deliberate, update this test; it is here to pin that `documented` "
                         "and `delivered_by` are computed independently")

    def test_a_prose_mention_does_not_count_as_delivery(self):
        """Delivery is matched ANCHORED, so a variable merely named in a comment is not
        reported as settable. This matters more than it sounds: almost every one of the 84
        undeliverable settings was mentioned SOMEWHERE in the artifacts -- in a comment
        explaining why it was not set. A substring match would have reported the whole
        population as delivered and the gate would have passed on day one."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(
                "services:\n"
                "  api:\n"
                "    environment:\n"
                "      # LOG_LEVEL: see .env.example -- deliberately not set here\n"
                "      # We also considered DECISION_ENCRYPT_AT_REST=1\n"
                "      DATABASE_URL: postgresql://x\n",
                encoding="utf-8")
            names = config_surface._names_in(str(path))
        self.assertEqual(names, {"DATABASE_URL"},
                         "a commented mention was counted as delivery")


class GateTests(unittest.TestCase):
    def _run(self, cwd=None):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "check_config_surface.py")],
                              capture_output=True, text=True, cwd=cwd or str(REPO))

    def test_the_gate_passes_on_the_repo_as_it_stands(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_gate_reports_the_real_totals(self):
        """A gate that prints 'OK' while counting nothing is the failure mode this repo keeps
        hitting. Assert it actually saw a three-figure inventory."""
        result = self._run()
        settings = config_surface.build(str(REPO))
        self.assertIn(f"settings the backend reads:            {len(settings)}", result.stdout)
        self.assertGreater(len(settings), 100)

    def test_the_allow_list_carries_a_reason_for_every_entry(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import check_config_surface

        for name, reason in check_config_surface.INTERNAL.items():
            self.assertGreater(len(reason), 40,
                               f"{name} is allow-listed with no real justification -- an "
                               f"allow-list without reasons is where settings go to stop "
                               f"being counted")

    def test_the_passthrough_contract_is_present(self):
        """Checked here as well as in the gate, because the gate's every other verdict is
        vacuous without it: remove the passthrough and all 84 settings are undeliverable again
        while the gate still reports them reachable."""
        sys.path.insert(0, str(REPO / "scripts"))
        import check_config_surface

        for relpath, marker, label in check_config_surface.PASSTHROUGH:
            text = (REPO / relpath).read_text(encoding="utf-8")
            self.assertIn(marker, text, f"{label} is missing from {relpath}")


class SandboxFallbackPostureTests(unittest.TestCase):
    def test_the_example_env_does_not_ship_the_inline_fallback(self):
        """`.env.example` shipped SANDBOX_FALLBACK_MODE=inline against a code default of
        `fail`. `inline` runs untrusted customer Python inside the API process when no sandbox
        subprocess is available -- so the file an operator copies re-enabled the posture the
        code default was changed to stop, silently, and only under the load that exhausts the
        pool."""
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("SANDBOX_FALLBACK_MODE=inline", text)
        self.assertIn("SANDBOX_FALLBACK_MODE=fail", text)

    def test_the_chart_pins_the_same_posture(self):
        values = (REPO / "helm" / "rulemind" / "values.yaml").read_text(encoding="utf-8")
        self.assertIn('SANDBOX_FALLBACK_MODE: "fail"', values)


if __name__ == "__main__":
    unittest.main()
