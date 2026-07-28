"""Policy lifecycle stages — pure transition logic.

Orthogonal to the dev/uat/prod environment (`status`): a policy has both an
environment and a lifecycle stage. Stages model the governance workflow:

    draft -> in_review -> ready -> live
                 |  ^        |        |
                 v  |        v        v
             rejected -> draft   in_review (revise)   archived
"""
from __future__ import annotations

from typing import Dict, List

LIFECYCLE_STAGES: List[str] = ["draft", "in_review", "ready", "live", "rejected", "archived"]

LIFECYCLE_LABELS: Dict[str, str] = {
    "draft": "Draft",
    "in_review": "In Review",
    "ready": "Ready to Deploy",
    "live": "Live",
    "rejected": "Rejected",
    "archived": "Archived",
}

# Allowed forward/back transitions per stage.
_TRANSITIONS: Dict[str, List[str]] = {
    "draft": ["in_review"],
    "in_review": ["ready", "rejected", "draft"],
    "ready": ["live", "in_review"],
    "live": ["archived", "in_review"],
    "rejected": ["draft"],
    "archived": [],
}


def allowed_transitions(stage: str) -> List[str]:
    return list(_TRANSITIONS.get(stage, []))


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, [])
