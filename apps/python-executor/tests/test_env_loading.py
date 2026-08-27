"""The .env loader must not assume a repo-shaped directory tree.

`app/__init__.py` walks up from itself looking for a .env. It originally indexed
`parents[3]` directly, which is fine in the repo (apps/python-executor/app) but
raises IndexError in the Docker image, where the package sits at /app/app — so the
API container failed at import and could not start.
"""
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


class EnvLoaderDepthTests(unittest.TestCase):
    def test_package_imports_from_a_shallow_path(self):
        """Import the loader logic with only two ancestors above it, mirroring /app/app."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "app" / "app"
            root.mkdir(parents=True)
            (root / "__init__.py").write_text(
                (pathlib.Path(__file__).resolve().parents[1] / "app" / "__init__.py").read_text()
            )
            proc = subprocess.run(
                [sys.executable, "-c", "import app; print('ok')"],
                cwd=str(root.parent), capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, f"import failed:\n{proc.stderr}")
            self.assertIn("ok", proc.stdout)

    def test_a_dotenv_beside_the_service_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp) / "svc"
            pkg = base / "app"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(
                (pathlib.Path(__file__).resolve().parents[1] / "app" / "__init__.py").read_text()
            )
            (base / ".env").write_text("RULEMIND_ENV_LOADER_PROBE=loaded\n")
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import app, os; print(os.getenv('RULEMIND_ENV_LOADER_PROBE'))"],
                cwd=str(base), capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("loaded", proc.stdout)


if __name__ == "__main__":
    unittest.main()
