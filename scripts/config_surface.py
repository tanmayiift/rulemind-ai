#!/usr/bin/env python3
"""The configuration surface, derived from source rather than maintained by hand.

Two things this answers, and neither was answerable before:

  1. WHICH settings does the backend read?  Parsed from the AST of every module under
     `apps/python-executor/app/`, so a setting cannot be read by the code and missing from
     the inventory -- which is exactly how `.env.example` came to be missing 46 variables.
  2. Which of those can an OPERATOR actually set?  Scanned out of `docker-compose.yml`,
     the Helm chart and `k8s/`.  For a self-hosted product the chart IS the configuration
     API, so a setting the code reads and no artifact delivers is not a tidy-up item: it is
     a feature the customer cannot reach.

This module is the inventory.  `scripts/check_config_surface.py` is the gate on top of it.

AST, not grep, deliberately: a regex pass over these same files earlier in this project
produced a confident and wrong answer (it bled across definitions), and the fix for a
hand-maintained list is not a differently-hand-maintained list.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "apps", "python-executor", "app")

# The helpers in app/env.py, plus the stdlib forms.  All take the variable name first.
ENV_HELPERS = {"env_str", "env_int", "env_float", "env_bool", "env_opt", "env_list"}


@dataclass
class Setting:
    name: str
    readers: List[str] = field(default_factory=list)   # "file.py:line (env_bool)"
    defaults: Set[str] = field(default_factory=set)
    delivered_by: Set[str] = field(default_factory=set)
    documented: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "readers": sorted(self.readers),
            "defaults": sorted(self.defaults),
            "delivered_by": sorted(self.delivered_by),
            "documented_in_env_example": self.documented,
            "deliverable": bool(self.delivered_by),
        }


def _literal(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        value = ast.literal_eval(node)
    except Exception:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    return str(value)


# An environment variable name, as opposed to an ordinary dict key read through the same `.get`.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")


def _environ_aliases(tree: ast.AST) -> Set[str]:
    """Names this file binds to os.environ -- `settings = env if env is not None else os.environ`.

    The archiver reads its ClickHouse connection and every S3 archive option through such an
    alias, so this inventory -- which only knew os.getenv / os.environ.get / env_* -- never saw
    them, and neither did the deployment artifacts: thirteen real settings a self-hosted customer
    could not discover. Found when an env_str() read of three of them tripped this gate.
    """
    aliases: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        reads_environ = any(isinstance(sub, ast.Attribute) and sub.attr == "environ"
                            and isinstance(sub.value, ast.Name) and sub.value.id == "os"
                            for sub in ast.walk(node.value))
        if reads_environ:
            aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return aliases


class _Visitor(ast.NodeVisitor):
    def __init__(self, relpath: str, out: Dict[str, Setting], aliases: Optional[Set[str]] = None) -> None:
        self.relpath = relpath
        self.out = out
        self.aliases = aliases or set()

    def _record(self, name: str, line: int, kind: str, default: Optional[str]) -> None:
        entry = self.out.setdefault(name, Setting(name=name))
        entry.readers.append(f"{self.relpath}:{line} ({kind})")
        if default is not None:
            entry.defaults.add(default)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name_node = node.args[0] if node.args else None
        literal_name = _literal(name_node) if isinstance(name_node, ast.Constant) else None

        kind = None
        if isinstance(func, ast.Attribute) and func.attr in ("getenv", "get"):
            # os.getenv(...) / os.environ.get(...)
            target = func.value
            if isinstance(target, ast.Name) and target.id == "os" and func.attr == "getenv":
                kind = "os.getenv"
            elif (isinstance(target, ast.Attribute) and target.attr == "environ"
                  and func.attr == "get"):
                kind = "os.environ.get"
            elif (isinstance(target, ast.Name) and target.id in self.aliases and func.attr == "get"
                  and literal_name and _ENV_NAME.match(literal_name)):
                kind = "os.environ alias"
        elif isinstance(func, ast.Name) and func.id in ENV_HELPERS:
            kind = func.id
        elif isinstance(func, ast.Attribute) and func.attr in ENV_HELPERS:
            kind = func.attr

        if kind and literal_name:
            default = _literal(node.args[1]) if len(node.args) > 1 else None
            self._record(literal_name, node.lineno, kind, default)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        # os.environ["X"]
        value = node.value
        if (isinstance(value, ast.Attribute) and value.attr == "environ"
                and isinstance(value.value, ast.Name) and value.value.id == "os"):
            literal_name = _literal(node.slice)
            if literal_name:
                self._record(literal_name, node.lineno, "os.environ[]", None)
        self.generic_visit(node)


def read_settings(app_dir: str = APP_DIR) -> Dict[str, Setting]:
    """Every environment variable the backend reads, with every call site."""
    out: Dict[str, Setting] = {}
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            relpath = os.path.relpath(path, ROOT)
            try:
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
            except SyntaxError as exc:  # pragma: no cover - would fail the suite first
                print(f"could not parse {relpath}: {exc}", file=sys.stderr)
                continue
            _Visitor(relpath, out, _environ_aliases(tree)).visit(tree)
    return out


# The backend is not only Python.  `RULEMIND_WORKERS` is read by the Dockerfile CMD and
# `PROMETHEUS_MULTIPROC_DIR` by entrypoint.sh, so a Python-only inventory would report the
# worker count as "not read" while an operator is actively setting it.  Shell reads count.
SHELL_READERS = [
    "apps/python-executor/entrypoint.sh",
    "apps/python-executor/Dockerfile",
]
_SHELL_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]{2,})(?::-[^}]*)?\}")


def read_shell_settings(root: str = ROOT) -> Dict[str, Setting]:
    """Variables the container's own shell reads -- CMD, ENTRYPOINT, entrypoint.sh."""
    out: Dict[str, Setting] = {}
    for rel in SHELL_READERS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for name in _SHELL_REF.findall(line):
                entry = out.setdefault(name, Setting(name=name))
                entry.readers.append(f"{rel}:{lineno} (shell)")
    return out


# A variable is "delivered" by an artifact when that artifact can SET it -- as a compose
# `NAME=` / `NAME:` entry, a ConfigMap/Secret key, or a container `- name: NAME`.  Matching is
# anchored so a mention inside a comment or a prose default does not count as delivery; the
# whole point of the gate is that documentation is not delivery.
_DELIVERY_PATTERNS = (
    re.compile(r"^\s*-?\s*([A-Z][A-Z0-9_]{2,}):\s"),        # yaml key:  NAME: value
    re.compile(r"^\s*-?\s*([A-Z][A-Z0-9_]{2,}):\s*$"),      # yaml key:  NAME:
    re.compile(r"^\s*-\s*([A-Z][A-Z0-9_]{2,})="),           # compose:   - NAME=value
    re.compile(r"^\s*name:\s*\"?([A-Z][A-Z0-9_]{2,})\"?\s*$"),  # k8s env:  - name: NAME
)

DELIVERY_SOURCES = {
    "compose": ["docker-compose.yml"],
    "helm": [
        "helm/rulemind/templates/configmap.yaml",
        "helm/rulemind/templates/secret.yaml",
        "helm/rulemind/templates/deployment-api.yaml",
        "helm/rulemind/templates/deployment-web.yaml",
        "helm/rulemind/templates/deployment-worker.yaml",
        "helm/rulemind/templates/job-migrate.yaml",
    ],
    "k8s": ["k8s/rulemind.yaml"],
}


def _names_in(path: str) -> Set[str]:
    found: Set[str] = set()
    if not os.path.exists(path):
        return found
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for raw in lines:
        line = raw.split("#", 1)[0] if raw.lstrip().startswith("#") else raw
        if not line.strip():
            continue
        for pattern in _DELIVERY_PATTERNS:
            match = pattern.match(line)
            if match:
                found.add(match.group(1))
                break
    return found


def read_delivery(root: str = ROOT) -> Dict[str, Set[str]]:
    """artifact -> the variable names it can set."""
    delivery: Dict[str, Set[str]] = {}
    for artifact, paths in DELIVERY_SOURCES.items():
        names: Set[str] = set()
        for rel in paths:
            names |= _names_in(os.path.join(root, rel))
        delivery[artifact] = names
    return delivery


def read_documented(root: str = ROOT) -> Set[str]:
    """Variables `.env.example` documents -- which is NOT the same as delivering them."""
    documented: Set[str] = set()
    path = os.path.join(root, ".env.example")
    if not os.path.exists(path):
        return documented
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    # A COMMENTED entry counts as documentation too. `.env.example` deliberately comments out
    # the settings whose act of being set IS the change (RULEMIND_ENV=production), and the ones
    # whose safe value is "unset". Counting only uncommented lines would report those as
    # undocumented and push someone to uncomment them, which inverts what the file means.
    for raw in lines:
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", raw.strip())
        if match:
            documented.add(match.group(1))
    return documented


def build(root: str = ROOT) -> Dict[str, Setting]:
    settings = read_settings(os.path.join(root, "apps", "python-executor", "app"))
    for name, shell_entry in read_shell_settings(root).items():
        settings.setdefault(name, Setting(name=name)).readers.extend(shell_entry.readers)
    delivery = read_delivery(root)
    documented = read_documented(root)
    for name, entry in settings.items():
        for artifact, names in delivery.items():
            if name in names:
                entry.delivered_by.add(artifact)
        entry.documented = name in documented
    return settings


def main(argv: List[str]) -> int:
    settings = build()
    if "--json" in argv:
        print(json.dumps([settings[name].as_dict() for name in sorted(settings)], indent=2))
        return 0
    undeliverable = [s for s in settings.values() if not s.delivered_by]
    print(f"settings the backend reads: {len(settings)}")
    print(f"  deliverable by some artifact: {len(settings) - len(undeliverable)}")
    print(f"  settable by nothing:          {len(undeliverable)}")
    print(f"  ...of those, documented in .env.example: "
          f"{sum(1 for s in undeliverable if s.documented)}")
    if "-v" in argv or "--verbose" in argv:
        for entry in sorted(undeliverable, key=lambda s: s.name):
            doc = " [documented]" if entry.documented else ""
            print(f"  {entry.name}{doc}  <- {entry.readers[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
