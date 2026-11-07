"""LSAI fork CI/CD policy: no CI on normal pushes or pull requests; only a push to release-0ics releases.
Same rules as GDShrapt's GDForkPolicyTests."""
import glob
import os
import re
import unittest

import _forkroot

WORKFLOWS_DIR = os.path.join(_forkroot.FORK_ROOT, ".github", "workflows")
RELEASE_BRANCH = "release-0ics"


def workflows():
    paths = sorted(glob.glob(os.path.join(WORKFLOWS_DIR, "*.yml")) + glob.glob(os.path.join(WORKFLOWS_DIR, "*.yaml")))
    result = []
    for path in paths:
        with open(path) as f:
            result.append((path, f.read()))
    return result


class ForkPolicyTests(unittest.TestCase):
    def test_workflows_have_no_pull_request_trigger(self):
        for path, text in workflows():
            with self.subTest(workflow=path):
                self.assertIsNone(re.search(r"^\s*pull_request(_target)?\s*:", text, re.MULTILINE), path)

    def test_workflows_push_trigger_only_on_release_branch(self):
        for path, text in workflows():
            if not re.search(r"^\s{2}push\s*:", text, re.MULTILINE):
                continue
            with self.subTest(workflow=path):
                push = re.search(r"^\s{2}push\s*:\s*\r?\n\s{4}branches\s*:\s*\[([^\]]*)\]", text, re.MULTILINE)
                self.assertIsNotNone(push, path + ": push trigger without a branch filter")
                branches = [b.strip().strip("'\"") for b in push.group(1).split(",")]
                self.assertEqual(branches, [RELEASE_BRANCH], path)


if __name__ == "__main__":
    unittest.main()
