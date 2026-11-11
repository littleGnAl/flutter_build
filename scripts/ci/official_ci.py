#!/usr/bin/env python3
"""Select the target revision's official Flutter postsubmit test targets."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess

import yaml


OFFICIAL_REPOSITORY = "https://github.com/flutter/flutter.git"
BRANCH_FILE = "bin/internal/release-candidate-branch.version"
ENGINE_CI = "engine/src/flutter/.ci.yaml"
RELEASE_BRANCH = r"flutter-\d+\.\d+-candidate\.\d+"


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def read_configuration(path):
    # Numeric subshards such as 1_1 must remain strings.
    return yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader)


def flag(value, default=False):
    if value is None:
        return default
    if value in (True, "true"):
        return True
    if value in (False, "false"):
        return False
    raise ValueError(f"Invalid CI boolean: {value!r}")


@dataclass
class Policy:
    branch: str
    root_targets: set[str]
    engine_targets: set[str]
    source_sha: str = ""
    official_sha: str = ""
    default_sha: str = ""

    def rejection(self, target, configuration, engine=False):
        name = target["name"]
        active = self.engine_targets if engine else self.root_targets
        if name not in active:
            return "removed_from_official_default_branch"
        branches = target.get("enabled_branches") or configuration.get("enabled_branches", [])
        if not branches or not any(re.fullmatch(pattern, self.branch) for pattern in branches):
            return "official_branch_not_enabled"
        if not flag(target.get("postsubmit"), True):
            return "not_an_official_postsubmit_target"
        if flag(target.get("properties", {}).get("release_build")):
            return "release_build_dependency_not_test_target"
        if re.fullmatch(RELEASE_BRANCH, self.branch) and flag(target.get("bringup")):
            return "bringup_excluded_on_official_release_branch"
        return None

    def targets(self, configuration, engine=False):
        return [target for target in configuration["targets"]
                if self.rejection(target, configuration, engine) is None]

    def engine_enabled(self, root, config_name):
        configuration = read_configuration(Path(root) / ENGINE_CI)
        return any(target.get("properties", {}).get("config_name") == config_name
                   for target in self.targets(configuration, engine=True))

    def document(self):
        document = asdict(self)
        for key in ("root_targets", "engine_targets"):
            document[key] = sorted(document[key])
        return document

    @classmethod
    def load(cls, root):
        path = Path(os.environ.get("FLUTTER_CI_POLICY", Path(root).resolve().parent / "test-results/ci-policy.json"))
        document = json.loads(path.read_text())
        for key in ("root_targets", "engine_targets"):
            document[key] = set(document[key])
        policy = cls(**document)
        if not policy.source_sha or git(root, "rev-parse", "HEAD") != policy.source_sha:
            raise ValueError("Official CI policy does not match the checked-out Flutter SHA")
        return policy


def source_branch(root, revision):
    if not git(root, "ls-tree", "--name-only", revision, "--", BRANCH_FILE):
        return "master"
    branch = git(root, "show", f"{revision}:{BRANCH_FILE}")
    if not re.fullmatch(RELEASE_BRANCH, branch):
        raise ValueError(f"Unsupported official release branch metadata: {branch!r}")
    return branch


def prepare(root):
    root = Path(root).resolve()
    source_sha = git(root, "rev-parse", "HEAD")
    branch = source_branch(root, source_sha)
    if git(root, "rev-parse", "--is-shallow-repository") == "true":
        subprocess.run(["git", "-C", str(root), "fetch", "--unshallow", "--filter=blob:none",
                        "--no-tags", "origin", source_sha], check=True)
    refs = sorted({"master", branch})
    remote = "official-ci"
    if remote in git(root, "remote").splitlines():
        git(root, "remote", "set-url", remote, OFFICIAL_REPOSITORY)
    else:
        git(root, "remote", "add", remote, OFFICIAL_REPOSITORY)
    # Fetch commit history without every historical source blob. Keep a real
    # promisor remote so git show can retrieve the few CI/metadata blobs needed.
    git(root, "config", f"remote.{remote}.promisor", "true")
    git(root, "config", f"remote.{remote}.partialclonefilter", "blob:none")
    subprocess.run(["git", "-C", str(root), "fetch", "--filter=blob:none", "--no-tags",
                    remote,
                    *[f"+refs/heads/{ref}:refs/remotes/official-ci/{ref}" for ref in refs]], check=True)
    official_sha = git(root, "merge-base", source_sha, f"refs/remotes/official-ci/{branch}")
    if source_branch(root, official_sha) != branch:
        raise ValueError("The target's release metadata does not match its official ancestor")
    # Cocoon drops targets removed from the default branch. For a historical
    # release, use the default branch at its official commit time, not today's
    # unrelated target inventory. Record both SHAs so this decision is auditable.
    if branch == "master":
        default_sha = official_sha
    else:
        committed = git(root, "show", "-s", "--format=%cI", official_sha)
        default_sha = git(root, "rev-list", "--first-parent", "-1", f"--before={committed}",
                          "refs/remotes/official-ci/master")
        if not default_sha:
            raise ValueError("Cannot find the official default-branch snapshot for this release")

    def names(path):
        configuration = yaml.load(git(root, "show", f"{default_sha}:{path}"), Loader=yaml.BaseLoader)
        return {target["name"] for target in configuration["targets"]}

    return Policy(branch, names(".ci.yaml"), names(ENGINE_CI), source_sha, official_sha, default_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("test-results/ci-policy.json"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        policy = Policy.load(args.flutter_root)
    else:
        policy = prepare(args.flutter_root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(policy.document(), indent=2) + "\n")
    summary = (f"Official postsubmit policy: {policy.branch}\n"
               f"Target SHA: {policy.source_sha}\nOfficial ancestor: {policy.official_sha}\n"
               f"Default-branch CI snapshot: {policy.default_sha}\n")
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as report:
            report.write("\n```text\n" + summary + "```\n")


if __name__ == "__main__":
    main()
