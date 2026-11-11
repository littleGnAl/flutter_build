import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
from official_ci import BRANCH_FILE, ENGINE_CI, Policy, prepare


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.config = {"enabled_branches": ["master", r"flutter-\d+\.\d+-candidate\.\d+"],
                       "targets": [
                           {"name": "Linux normal"},
                           {"name": "Linux bringup", "bringup": "true"},
                           {"name": "Linux removed"},
                           {"name": "Linux master only", "enabled_branches": ["master"]},
                           {"name": "Linux disabled", "postsubmit": "false"},
                           {"name": "Linux build", "properties": {"release_build": "true"}},
                       ]}
        self.names = {t["name"] for t in self.config["targets"]} - {"Linux removed"}

    def test_release_excludes_bringup_retired_disabled_and_master_only_targets(self):
        policy = Policy("flutter-7.42-candidate.3", self.names, set())
        self.assertEqual([t["name"] for t in policy.targets(self.config)], ["Linux normal"])

    def test_master_keeps_bringup_postsubmits_without_scheduling_release_builds(self):
        policy = Policy("master", self.names, set())
        self.assertEqual([t["name"] for t in policy.targets(self.config)],
                         ["Linux normal", "Linux bringup", "Linux master only"])

    def test_branch_regex_matches_whole_official_branch_name(self):
        policy = Policy("custom-flutter-7.42-candidate.3", self.names, set())
        self.assertEqual(policy.targets(self.config), [])

    def test_empty_target_branch_override_inherits_global_branches(self):
        self.config["targets"][0]["enabled_branches"] = []
        policy = Policy("flutter-7.42-candidate.3", self.names, set())
        self.assertEqual([t["name"] for t in policy.targets(self.config)], ["Linux normal"])

    def test_engine_targets_use_the_engine_inventory_and_config_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / ENGINE_CI
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps({"enabled_branches": ["master"], "targets": [
                {"name": "Linux native", "properties": {"config_name": "android_emulator"}},
                {"name": "Mac retired", "properties": {"config_name": "mac_unopt"}},
            ]}))
            policy = Policy("master", {"Mac retired"}, {"Linux native"})
            self.assertTrue(policy.engine_enabled(root, "android_emulator"))
            self.assertFalse(policy.engine_enabled(root, "mac_unopt"))


class SourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.upstream = self.directory / "upstream"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "--quiet")
        self.git(self.upstream, "symbolic-ref", "HEAD", "refs/heads/master")
        self.git(self.upstream, "config", "user.name", "CI test")
        self.git(self.upstream, "config", "user.email", "ci@example.invalid")
        self.write_config(["Linux keep", "Linux retired"])
        self.initial = self.commit("initial", "2025-08-01T00:00:00Z")
        self.branch = "flutter-7.42-candidate.3"
        self.git(self.upstream, "checkout", "--quiet", "-b", self.branch)
        branch_file = self.upstream / BRANCH_FILE
        branch_file.parent.mkdir(parents=True)
        branch_file.write_text(self.branch + "\n")
        self.release = self.commit("release metadata", "2025-10-01T00:00:00Z")
        self.git(self.upstream, "checkout", "--quiet", "master")
        self.write_config(["Linux keep"])
        self.historical_main = self.commit("retire old target", "2025-09-01T00:00:00Z")
        self.write_config(["Linux future"])
        self.current_main = self.commit("unrelated future CI", "2026-01-01T00:00:00Z")
        self.checkout = self.directory / "flutter"
        subprocess.run(["git", "clone", "--quiet", str(self.upstream), str(self.checkout)], check=True)
        self.git(self.checkout, "config", "user.name", "CI test")
        self.git(self.checkout, "config", "user.email", "ci@example.invalid")
        self.env = {"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.{self.upstream}.insteadOf",
                    "GIT_CONFIG_VALUE_0": "https://github.com/flutter/flutter.git"}

    def git(self, root, *arguments):
        return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()

    def write_config(self, names):
        for relative in (".ci.yaml", ENGINE_CI):
            file = self.upstream / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps({"targets": [{"name": name} for name in names]}))

    def commit(self, message, date):
        self.git(self.upstream, "add", ".")
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}):
            self.git(self.upstream, "commit", "--quiet", "-m", message)
        return self.git(self.upstream, "rev-parse", "HEAD")

    def test_release_policy_uses_tracked_metadata_and_historical_official_main(self):
        self.git(self.checkout, "checkout", "--quiet", "-b", "custom-fork-name", self.release)
        (self.checkout / "fork.cc").write_text("unpublished engine change\n")
        self.git(self.checkout, "add", "fork.cc")
        self.git(self.checkout, "commit", "--quiet", "-m", "fork change")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env):
            policy = prepare(self.checkout)
        self.assertEqual(policy.branch, self.branch)
        self.assertEqual(policy.official_sha, self.release)
        self.assertEqual(policy.default_sha, self.historical_main)
        self.assertEqual(policy.root_targets, {"Linux keep"})
        self.assertEqual(policy.source_sha, head)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_development_policy_uses_the_actual_official_ancestor(self):
        self.git(self.checkout, "checkout", "--quiet", "--detach", self.current_main)
        with mock.patch.dict(os.environ, self.env):
            policy = prepare(self.checkout)
        self.assertEqual(policy.branch, "master")
        self.assertEqual(policy.official_sha, self.current_main)
        self.assertEqual(policy.default_sha, self.current_main)
        self.assertEqual(policy.root_targets, {"Linux future"})

    def test_shallow_action_checkout_can_resolve_the_release_policy(self):
        shallow = self.directory / "shallow"
        subprocess.run(["git", "clone", "--quiet", "--depth=1", "--branch", self.branch,
                        self.upstream.as_uri(), str(shallow)], check=True)
        self.assertEqual(self.git(shallow, "rev-parse", "--is-shallow-repository"), "true")
        with mock.patch.dict(os.environ, self.env):
            policy = prepare(shallow)
        self.assertEqual(policy.official_sha, self.release)
        self.assertEqual(policy.default_sha, self.historical_main)
        self.assertEqual(policy.root_targets, {"Linux keep"})
        self.assertEqual(self.git(shallow, "rev-parse", "HEAD"), self.release)

    def test_saved_policy_cannot_be_reused_for_a_different_source_sha(self):
        policy = Policy("master", {"Linux future"}, set(), self.current_main, self.current_main, self.current_main)
        path = self.directory / "test-results/ci-policy.json"
        path.parent.mkdir()
        path.write_text(json.dumps(policy.document()))
        with mock.patch.dict(os.environ):
            os.environ.pop("FLUTTER_CI_POLICY", None)
            self.assertEqual(Policy.load(self.checkout).source_sha, self.current_main)
            self.git(self.checkout, "checkout", "--quiet", "--detach", self.initial)
            with self.assertRaisesRegex(ValueError, "does not match"):
                Policy.load(self.checkout)


if __name__ == "__main__":
    unittest.main()
