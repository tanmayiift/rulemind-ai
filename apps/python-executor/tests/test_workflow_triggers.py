"""Every self-hosted workflow must be `workflow_dispatch` (or `schedule`) only.

This is the repository's oldest standing security constraint and it has never been enforced by
anything. `CLAUDE.md` states it and then says, correctly, that it is "the decision most likely to
be casually reversed":

    The runner executes as the maintainer's user and there are open bot-authored dependency PRs.

A `pull_request` trigger on a self-hosted runner means a Dependabot PR -- or anyone's fork PR --
executes arbitrary code as the maintainer, with their SSH keys, their gh token, their Docker
socket and their home directory. A `push` trigger is the same thing one merge later. Neither is
a subtle failure: it is full local compromise, from a one-line diff that looks like an
improvement ("run tests automatically!").

The rule is written down in three places and checked in none, which is the shape of every other
finding in this codebase. So: checked.

`schedule` is allowed -- security.yml has a weekly cron and nothing about a timer lets an
outsider choose what runs. `workflow_call` is allowed for the same reason.

Since the repository went public (2026-10), two more rules apply to EVERY workflow, hosted or not:
`pull_request_target` is banned (it runs with this repository's token and secrets, and checking
out the fork's head inside it hands both over), and every third-party action is pinned to a full
commit SHA (a tag like `@v4` is a pointer whoever owns that action can move).
"""
from __future__ import annotations

import os
import re
import unittest

import yaml


def _repo_root() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    while here != os.path.dirname(here):
        if os.path.exists(os.path.join(here, "docker-compose.yml")):
            return here
        here = os.path.dirname(here)
    raise RuntimeError("could not locate the repository root")


REPO = _repo_root()
WORKFLOWS = os.path.join(REPO, ".github", "workflows")

# Triggers an outsider can cause to fire. `schedule` and `workflow_call` cannot be.
UNSAFE_TRIGGERS = {
    "pull_request", "pull_request_target", "push", "issue_comment", "issues",
    "fork", "public", "watch", "repository_dispatch",
}


def _workflows():
    for name in sorted(os.listdir(WORKFLOWS)):
        if not name.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
            yield name, yaml.safe_load(fh)


def _triggers(doc) -> set:
    # PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1). Accept both spellings
    # rather than silently finding no triggers and passing.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, dict):
        return set(raw)
    return set()


PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _uses(doc):
    for job in (doc.get("jobs") or {}).values():
        if isinstance(job.get("uses"), str):  # reusable workflow call
            yield job["uses"]
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                yield step["uses"]


def _runs_self_hosted(doc) -> bool:
    for job in (doc.get("jobs") or {}).values():
        runs_on = job.get("runs-on")
        blob = runs_on if isinstance(runs_on, str) else " ".join(map(str, runs_on or []))
        if "self-hosted" in blob:
            return True
    return False


class WorkflowTriggerTest(unittest.TestCase):
    def test_there_are_workflows_to_check(self):
        """Guard on the guard: if the directory moves, the tests below pass by finding nothing."""
        self.assertGreater(len(list(_workflows())), 0, "no workflows found under %s" % WORKFLOWS)

    def test_no_self_hosted_workflow_has_an_externally_triggerable_event(self):
        offenders = []
        for name, doc in _workflows():
            if not _runs_self_hosted(doc):
                continue
            bad = _triggers(doc) & UNSAFE_TRIGGERS
            if bad:
                offenders.append("%s: %s" % (name, ", ".join(sorted(bad))))
        self.assertEqual(
            offenders, [],
            "self-hosted workflow(s) can be triggered by someone outside this machine: %s. The "
            "runner executes as the maintainer's user and there are bot-authored dependency PRs, "
            "so this is arbitrary code execution on their laptop. Use workflow_dispatch."
            % "; ".join(offenders),
        )

    def test_no_workflow_uses_pull_request_target(self):
        offenders = [name for name, doc in _workflows() if "pull_request_target" in _triggers(doc)]
        self.assertEqual(offenders, [], "pull_request_target runs fork code with this repository's "
                         "token and secrets: %s" % offenders)

    def test_every_action_is_pinned_to_a_commit_sha(self):
        unpinned = ["%s: %s" % (name, ref) for name, doc in _workflows() for ref in _uses(doc)
                    if not ref.startswith(("./", "docker://")) and not PINNED.match(ref)]
        self.assertEqual(unpinned, [], "action(s) not pinned to a 40-char commit SHA: %s" % unpinned)

    def test_the_pull_request_gate_is_hosted_and_read_only(self):
        """It replaced "every workflow is self-hosted", which held only while hosted jobs were
        billing-blocked. The PR gate runs code from any fork, so it must never move to the
        self-hosted runner, never get a writable token, and never see a secret."""
        docs = dict(_workflows())
        self.assertIn("ci.yml", docs, "the pull-request gate is gone")
        gate = docs["ci.yml"]
        self.assertIn("pull_request", _triggers(gate))
        self.assertFalse(_runs_self_hosted(gate))
        self.assertEqual(gate.get("permissions"), {"contents": "read"})
        with open(os.path.join(WORKFLOWS, "ci.yml"), encoding="utf-8") as fh:
            self.assertNotIn("secrets.", fh.read())

    def test_every_job_has_a_timeout(self):
        """A hung job on a self-hosted runner holds the maintainer's machine for the 6-hour
        default."""
        missing = []
        for name, doc in _workflows():
            for job_name, job in (doc.get("jobs") or {}).items():
                if "timeout-minutes" not in job:
                    missing.append("%s:%s" % (name, job_name))
        self.assertEqual(missing, [], "job(s) with no timeout-minutes: %s" % missing)


if __name__ == "__main__":
    unittest.main()
