#!/usr/bin/env python3
"""Pin the public bootstrap Engine independently of the tested native Engine."""
import argparse
import json
import os
import subprocess
from pathlib import Path


def git(root, *arguments, input=None):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, input=input,
    ).strip()


def baseline_engine(root, baseline):
    tracked = git(root, "ls-tree", "--name-only", baseline, "--", "bin/internal/engine.version")
    if tracked:
        return git(root, "show", f"{baseline}:bin/internal/engine.version")
    tree = git(root, "ls-tree", baseline, "--", "DEPS", "engine",
               "bin/internal/release-candidate-branch.version")
    if not tree or not any(line.endswith("\tengine") for line in tree.splitlines()):
        raise ValueError("This revision is not a supported Flutter/Engine monorepo")
    # Same manifest and trailing newline as bin/internal/content_aware_hash.sh.
    return git(root, "hash-object", "--stdin", input=tree + "\n")


def emit(values):
    for filename in ("GITHUB_ENV", "GITHUB_OUTPUT"):
        if os.environ.get(filename):
            with Path(os.environ[filename]).open("a") as output:
                for key, value in values.items():
                    output.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=True)
    source_sha = git(root, "rev-parse", "HEAD")
    remotes = git(root, "remote").splitlines()
    if "upstream" in remotes:
        git(root, "remote", "set-url", "upstream", "https://github.com/flutter/flutter.git")
    else:
        git(root, "remote", "add", "upstream", "https://github.com/flutter/flutter.git")
    subprocess.run(
        ["git", "-C", str(root), "fetch", "--no-tags", "upstream",
         "+refs/heads/master:refs/remotes/upstream/master"], check=True,
    )
    baseline = git(root, "merge-base", source_sha, "upstream/master")
    engine = baseline_engine(root, baseline)
    if len(engine) != 40 or any(c not in "0123456789abcdef" for c in engine):
        raise ValueError(f"Invalid official bootstrap Engine hash: {engine!r}")
    metadata = {
        "source_sha": source_sha, "upstream_baseline_sha": baseline,
        "bootstrap_engine_version": engine,
        "sdk_policy": "target Framework/Tools source with official baseline Engine",
        "native_policy": "native suites build and select target-revision Engine outputs",
    }
    (results / "revision.json").write_text(json.dumps(metadata, indent=2) + "\n")
    emit({
        "FLUTTER_ROOT": str(root), "FLUTTER_TEST_OUTPUTS_DIR": str(results),
        "FLUTTER_PREBUILT_ENGINE_VERSION": engine, "FLUTTER_BASELINE_SHA": baseline,
        "FLUTTER_SOURCE_SHA": source_sha, "baseline_sha": baseline, "engine_hash": engine,
    })
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
