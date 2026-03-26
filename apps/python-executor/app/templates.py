from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Optional, Set


PLACEHOLDER_RE = re.compile(r"\{\{(.+?)\}\}")


def resolve_path(path: str, context: Dict[str, Any]) -> Any:
    parts = [part for part in path.split(".") if part]
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return ""
    return current if current is not None else ""


def resolve_template(template: Any, context: Dict[str, Any], secret_values: Optional[Set[str]] = None) -> Any:
    if isinstance(template, str):
        def replacer(match: re.Match[str]) -> str:
            raw = match.group(1).strip()
            if raw == "timestamp":
                return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            value = str(resolve_path(raw, context))
            if secret_values is not None and raw.startswith("secrets.") and value:
                secret_values.add(value)
            return value

        return PLACEHOLDER_RE.sub(replacer, template)
    if isinstance(template, dict):
        return {key: resolve_template(value, context, secret_values) for key, value in template.items()}
    if isinstance(template, list):
        return [resolve_template(value, context, secret_values) for value in template]
    return template
