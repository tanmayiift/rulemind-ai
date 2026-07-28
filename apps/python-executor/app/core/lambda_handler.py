"""Optional AWS Lambda adapter over the same stateless core.

Deprioritized vs. the Kubernetes deployment (see the architecture decision in the
plan) — kept as a thin proof of portability: the identical pure `decide()` runs
unchanged behind API Gateway. Cold-start caveats make this a secondary target,
not the primary decision path. The bundle is read from RULEMIND_BUNDLE_PATH
(packaged with the function or mounted from EFS/S3 by the host).
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from . import decide

_BUNDLE: Optional[Dict[str, Any]] = None


def _bundle() -> Dict[str, Any]:
    global _BUNDLE
    if _BUNDLE is None:
        path = os.environ["RULEMIND_BUNDLE_PATH"]
        with open(path, "r", encoding="utf-8") as handle:
            _BUNDLE = json.load(handle)
    return _BUNDLE


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    body = event.get("body") if isinstance(event, dict) else None
    data = json.loads(body) if isinstance(body, str) else (body or event or {})
    payload = data.get("payload", data)
    ctx = {
        "policy_id": data.get("policy_id"),
        "subject_id": data.get("subject_id"),
        "variables": data.get("variables"),
    }
    result = decide(_bundle(), payload, ctx)
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(result),
    }
