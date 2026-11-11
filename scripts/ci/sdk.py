#!/usr/bin/env python3
"""Run the checked-out Flutter SDK's Android tool integration shards."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys


ANDROID_TOOL_SHARDS = {
    "android_preview_tool_integration_tests",
    "android_java11_tool_integration_tests", "android_java17_tool_integration_tests",
}


def empty_tool_runner(root: Path, shard: str) -> bool:
    variant = shard.removeprefix("android_").removesuffix("_tool_integration_tests")
    path = root / f"dev/bots/suite_runners/run_android_{variant}_integration_tool_tests.dart"
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S)
    function = f"android{variant.title()}IntegrationToolTestsRunner"
    declaration = rf"Future<void>\s+{function}\s*\([^)]*\)\s*async\s*\{{"
    if not re.search(declaration, source):
        raise ValueError(f"Cannot identify Android tool runner {function} in {path}")
    return bool(re.search(declaration + r"\s*\}", source))


def target_dependencies(configuration: dict, target: dict) -> dict:
    platform = target["name"].split()[0].lower()
    inherited = configuration.get("platform_properties", {}).get(platform, {}).get("properties", {})
    result = {}
    for properties in (inherited, target.get("properties", {})):
        dependencies = properties.get("dependencies", [])
        if isinstance(dependencies, str):
            dependencies = json.loads(dependencies)
        result.update((dependency["dependency"], dependency.get("version", ""))
                      for dependency in dependencies)
    return result


def android_sdk_version(configuration: dict, target: dict) -> str:
    version = target_dependencies(configuration, target).get("android_sdk", "")
    if not re.fullmatch(r"version:[A-Za-z0-9_.+-]+", version):
        raise ValueError(f"Missing or unsupported Android SDK dependency for {target['name']}: {version!r}")
    return version


def ci_matrix(configuration: dict, root: Path, policy=None, unavailable=None) -> dict:
    """Select complete Android tool partitions from the requested revision."""
    if policy is None:
        from official_ci import Policy
        policy = Policy.load(root)
    selected: dict[tuple[str, str], dict] = {}
    for target in configuration["targets"]:
        properties = target.get("properties", {})
        shard = properties.get("shard")
        if shard not in ANDROID_TOOL_SHARDS:
            continue
        name = target["name"]
        if name.split()[0] != "Linux":
            continue
        reason = policy.rejection(target, configuration)
        if reason is None and empty_tool_runner(root, shard):
            reason = "official runner is an empty function"
        if reason is not None:
            if unavailable is not None:
                unavailable.append({"target": name, "shard": shard, "reason": reason})
            continue
        runner = "ubuntu-22.04"
        subshard = str(properties.get("subshard", ""))
        dependencies = target_dependencies(configuration, target)
        jdk = dependencies.get("open_jdk", "")
        if jdk:
            if not jdk.startswith("version:"):
                raise ValueError(f"Unsupported JDK dependency for {name}: {jdk}")
            jdk = jdk.removeprefix("version:")
        key = shard, subshard
        sdk_version = (android_sdk_version(configuration, target)
                       if shard == "android_preview_tool_integration_tests" else dependencies.get("android_sdk", ""))
        entry = {"runner": runner, "shard": shard, "subshard": subshard,
                 "name": f"Android tools {shard.removeprefix('android_')} {subshard or 'all'}",
                 "source_target": name, "jdk": jdk, "android_sdk": sdk_version}
        selected.setdefault(key, entry)

    entries = list(selected.values())
    groups: dict[tuple[str, str], set[str]] = {}
    for entry in entries:
        groups.setdefault((entry["runner"], entry["shard"]), set()).add(entry["subshard"])
    for group, values in groups.items():
        numeric = [re.fullmatch(r"(\d+)_(\d+)", value) for value in values]
        if numeric and all(numeric):
            totals = {int(match[2]) for match in numeric}
            if len(totals) != 1 or {int(match[1]) for match in numeric} != set(range(1, next(iter(totals)) + 1)):
                raise ValueError(f"Incomplete or conflicting CI partition {group}: {values}")
    return {"include": sorted(entries, key=lambda entry: entry["name"])}


def plan_diagnostics(matrix: dict, unavailable=None) -> list[str]:
    entries = matrix["include"]
    present = {entry["shard"] for entry in entries}
    diagnostics = [f"{item['target']}: {item['reason']}; no job scheduled."
                   for item in unavailable or []]
    excluded = {item["shard"] for item in unavailable or []}
    for shard in sorted(ANDROID_TOOL_SHARDS - present):
        if shard in excluded:
            continue
        message = f"{shard}: no eligible nonempty runner in the requested Flutter CI; no job scheduled."
        if shard == "android_java17_tool_integration_tests" and "android_java11_tool_integration_tests" in present:
            message += " This ref configures android_java11_tool_integration_tests instead."
        diagnostics.append(message)
    return diagnostics


def report_plan(matrix: dict, unavailable=None) -> None:
    diagnostics = plan_diagnostics(matrix, unavailable)
    for message in diagnostics:
        print(f"::notice::{message}", file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as output:
            output.write(f"\nAndroid tool plan: {len(matrix['include'])} partitions from the requested ref.\n")
            for message in diagnostics:
                output.write(f"\n- {message}\n")


def read_configuration(path: Path) -> dict:
    import yaml
    # Flutter properties are consumed as strings. YAML numeric coercion turns
    # unquoted 1_1 into the integer 11 and would select the wrong partition.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


class Runner:
    def __init__(self, root: Path, outputs: Path, name: str):
        self.root = root.resolve()
        self.outputs = outputs.resolve() / name
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.log = self.outputs / "run.log"

    def run(self, arguments: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
        with self.log.open("a") as log:
            line = f"$ {shlex.join(map(str, arguments))}\n"
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
            process = subprocess.Popen(arguments, cwd=cwd or self.root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace")
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, arguments)

    def manifest(self, **details) -> None:
        details.update(flutter_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip(),
                       prebuilt_engine=os.environ.get("FLUTTER_PREBUILT_ENGINE_VERSION"),
                       host=f"{platform.system()}/{platform.machine()}")
        (self.outputs / "manifest.json").write_text(json.dumps(details, indent=2) + "\n")

    def collect(self) -> None:
        patterns = ["error.log", "test_results.json", "**/failures/**"]
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if path.is_file() and not path.is_symlink() and path.stat().st_size < 20_000_000:
                    destination = self.outputs / "artifacts" / path.relative_to(self.root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)


def run_sdk(runner: Runner, shard: str, subshard: str, android_sdk: str = "") -> None:
    if shard not in ANDROID_TOOL_SHARDS:
        raise ValueError(f"Unsupported Android tool shard: {shard}")
    if not os.environ.get("FLUTTER_PREBUILT_ENGINE_VERSION"):
        raise ValueError("Android tool tests require an explicit official FLUTTER_PREBUILT_ENGINE_VERSION")
    if empty_tool_runner(runner.root, shard):
        raise ValueError(f"Official Android tool runner is empty: {shard}")
    if shard == "android_preview_tool_integration_tests":
        configuration = read_configuration(runner.root / ".ci.yaml")
        targets = [target for target in configuration["targets"]
                   if target["name"].startswith("Linux ")
                   and target.get("properties", {}).get("shard") == shard
                   and str(target["properties"].get("subshard", "")) == subshard]
        if not targets:
            raise ValueError(f"Requested Android preview partition is not configured: {subshard!r}")
        versions = {android_sdk_version(configuration, target) for target in targets}
        if len(versions) != 1:
            raise ValueError(f"Conflicting Android SDK dependencies for {shard}: {sorted(versions)}")
        configured_sdk = versions.pop()
        if android_sdk and android_sdk != configured_sdk:
            raise ValueError(f"Planned Android SDK {android_sdk!r} differs from target CI {configured_sdk!r}")
        android_sdk = configured_sdk
    flutter = str(runner.root / "bin/flutter")
    runner.manifest(kind="android-tools", shard=shard, subshard=subshard, native_engine_coverage=False,
                    android_sdk=android_sdk)
    runner.run([flutter, "config", f"--jdk-dir={os.environ['JAVA_HOME']}"])
    runner.run([flutter, "precache", "--android"])
    runner.run([flutter, "update-packages"])
    env = os.environ.copy()
    if shard == "android_preview_tool_integration_tests":
        sdk = runner.outputs / "android-preview-sdk"
        ensure = runner.outputs / "android-sdk.ensure"
        ensure.write_text(f"flutter/android/sdk/all/linux-amd64 {android_sdk}\n")
        runner.run(["cipd", "ensure", "-root", str(sdk), "-ensure-file", str(ensure)])
        runner.run([flutter, "config", f"--android-sdk={sdk}"])
        env.update(ANDROID_HOME=str(sdk), ANDROID_SDK_ROOT=str(sdk))
    env.update(SHARD=shard, FLUTTER_ROOT=str(runner.root))
    env.pop("SUBSHARD", None)
    if subshard:
        env["SUBSHARD"] = subshard
    runner.run([str(runner.root / "bin/dart"), "--enable-asserts", "dev/bots/test.dart"], env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--outputs", type=Path, default=Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    sdk = subparsers.add_parser("sdk")
    sdk.add_argument("--shard", required=True)
    sdk.add_argument("--subshard", default="")
    sdk.add_argument("--android-sdk", default="")
    args = parser.parse_args()
    if args.command == "plan":
        configuration = read_configuration(args.flutter_root / ".ci.yaml")
        unavailable = []
        matrix = ci_matrix(configuration, args.flutter_root, unavailable=unavailable)
        report_plan(matrix, unavailable)
        args.outputs.mkdir(parents=True, exist_ok=True)
        (args.outputs / "android-tools-plan.json").write_text(json.dumps({
            "matrix": matrix, "excluded": unavailable,
        }, indent=2) + "\n")
        print("matrix=" + json.dumps(matrix, separators=(",", ":")))
        print("has-tests=" + str(bool(matrix["include"])).lower())
        return 0
    name = f"android-tools-{args.shard}-{args.subshard or 'all'}"
    runner = Runner(args.flutter_root, args.outputs, name)
    result = "failure"
    try:
        run_sdk(runner, args.shard, args.subshard, args.android_sdk)
        result = "success"
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    finally:
        runner.collect()
        (runner.outputs / "result.json").write_text(json.dumps({"result": result}) + "\n")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as output:
                output.write(f"\n- {name}: **{result}**. Commands and engine provenance are in the artifact.\n")


if __name__ == "__main__":
    raise SystemExit(main())
