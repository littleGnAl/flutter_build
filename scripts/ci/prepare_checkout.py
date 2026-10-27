#!/usr/bin/env python3
"""Pin the public bootstrap Engine independently of the tested native Engine."""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def git(root, *arguments, input=None, env=None):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], text=True, input=input, env=env,
    ).strip()


def baseline_engine(root, baseline):
    # Release forks retain the published Engine/Dart pin of their release.
    # Overriding it with an ancestor's hash can select nonexistent artifacts.
    tracked = git(root, "ls-tree", "--name-only", "HEAD", "--", "bin/internal/engine.version")
    if tracked:
        return git(root, "show", "HEAD:bin/internal/engine.version")
    if not git(root, "ls-tree", "--name-only", baseline, "--", "engine"):
        raise ValueError("This revision is not a supported Flutter/Engine monorepo")
    # Historical releases used commit SHAs; newer versions use content hashes
    # whose format has also changed. Let the official revision select its own
    # version in an isolated clone, without touching the tested checkout/cache.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in {
               "FLUTTER_PREBUILT_ENGINE_VERSION", "FLUTTER_REALM", "LUCI_CONTEXT"}}
    with tempfile.TemporaryDirectory(prefix="flutter-bootstrap-") as directory:
        checkout = Path(directory) / "baseline"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                        str(root), str(checkout)], check=True, env=env)
        git(checkout, "update-ref", "--no-deref", "HEAD", baseline, env=env)
        git(checkout, "checkout", baseline, "--", "bin/internal", env=env)
        git(checkout, "remote", "add", "upstream", "https://github.com/flutter/flutter.git", env=env)
        git(checkout, "update-ref", "refs/remotes/upstream/master", baseline, env=env)
        updater = checkout / "bin/internal/update_engine_version.sh"
        if not updater.is_file():
            raise ValueError("The official baseline has no supported Engine version selector")
        subprocess.run(["bash", str(updater)], cwd=checkout, env=env, check=True)
        return (checkout / "bin/cache/engine.stamp").read_text().strip()


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
        "bootstrap_engine_selector": ("target-engine.version" if git(
            root, "ls-tree", "--name-only", "HEAD", "--", "bin/internal/engine.version")
            else "official-baseline-update_engine_version.sh"),
        "sdk_policy": "target Framework/Tools source with official prebuilt Engine",
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
