#!/usr/bin/env python3
"""Resolve a public GitHub repository/ref once, before fanning out test jobs."""
import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path


def validate_repository(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("flutter_repo must be a GitHub owner/repository")
    if any(part in (".", "..") for part in repository.split("/")):
        raise ValueError("invalid repository name")
    return repository


def validate_ref(ref):
    if not ref or ref.startswith("-") or any(c.isspace() or ord(c) < 32 for c in ref):
        raise ValueError("flutter_ref must be a nonempty branch, tag or commit")
    return ref


def resolve(repository, ref):
    validate_repository(repository)
    validate_ref(ref)
    with tempfile.TemporaryDirectory(prefix="flutter-ref-") as directory:
        subprocess.run(["git", "init", "--quiet", directory], check=True)
        subprocess.run(
            ["git", "-C", directory, "fetch", "--quiet", "--depth=1", "--no-tags",
             f"https://github.com/{repository}.git", ref], check=True,
        )
        sha = subprocess.check_output(
            ["git", "-C", directory, "rev-parse", "FETCH_HEAD^{commit}"], text=True,
        ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("git did not resolve a commit SHA")
    return sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    sha = resolve(args.repo, args.ref)
    print(sha)
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"sha={sha}\n")


if __name__ == "__main__":
    main()
