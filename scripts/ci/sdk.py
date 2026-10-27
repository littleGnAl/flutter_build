#!/usr/bin/env python3
"""Run the checked-out Flutter SDK's own CI shards and host engine tests."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request


CORE_SDK_SHARDS = {
    "framework_tests", "tool_tests", "tool_integration_tests", "web_tool_tests", "build_tests", "analyze",
}
OPTIONAL_SDK_SHARDS = {
    "tool_tests_commands", "android_preview_tool_integration_tests",
    "android_java11_tool_integration_tests", "android_java17_tool_integration_tests",
}
SDK_SHARDS = CORE_SDK_SHARDS | OPTIONAL_SDK_SHARDS
GPU_EXECUTABLES = {"impeller_unittests", "impeller_dart_unittests"}
GOLDENS_PATH = Path("packages/flutter_goldens/lib/flutter_goldens.dart")


def ci_matrix(configuration: dict, test_source: str) -> dict:
    """Use the requested revision's complete shard partitions, not pinned counts."""
    selected: dict[tuple[str, str, str], tuple[int, dict]] = {}
    for target in configuration["targets"]:
        properties = target.get("properties", {})
        shard = properties.get("shard")
        if shard not in SDK_SHARDS:
            continue
        name = target["name"]
        prefix = name.split()[0]
        if prefix == "Linux":
            runner, preference = "ubuntu-22.04", 0
        elif prefix in {"Mac", "Mac_arm64", "Mac_x64"}:
            runner, preference = "macos-15", {"Mac_arm64": 0, "Mac": 1, "Mac_x64": 2}[prefix]
        else:
            continue
        if shard in {"analyze", "android_preview_tool_integration_tests", "android_java11_tool_integration_tests", "android_java17_tool_integration_tests"} and runner != "ubuntu-22.04":
            continue
        subshard = str(properties.get("subshard", ""))
        dependencies = properties.get("dependencies", [])
        if isinstance(dependencies, str):
            dependencies = json.loads(dependencies)
        jdk = next((dependency["version"] for dependency in dependencies if dependency["dependency"] == "open_jdk"), "")
        if jdk:
            if not jdk.startswith("version:"):
                raise ValueError(f"Unsupported JDK dependency for {name}: {jdk}")
            jdk = jdk.removeprefix("version:")
        key = runner, shard, subshard
        entry = {"runner": runner, "shard": shard, "subshard": subshard,
                 "name": f"{runner}-{shard}-{subshard or 'all'}", "source_target": name, "jdk": jdk}
        if key not in selected or preference < selected[key][0]:
            selected[key] = preference, entry

    # Include every framework subshard exported by the target runner, even if a
    # particular upstream presubmit happens not to schedule it on Linux.
    framework_runner = test_source
    match = re.search(r"await selectSubshard\(<String, ShardRunner>\{(.*?)\}\);", framework_runner, re.S)
    if not match:
        raise ValueError("Cannot identify framework subshards in the requested revision")
    framework_subshards = re.findall(r"'([^']+)'\s*:", match.group(1))
    if not framework_subshards:
        raise ValueError("The framework runner exported no subshards")
    for subshard in framework_subshards:
        # Keep Impeller on the official golden platform. flutter_tester uses
        # statically linked SwiftShader Vulkan here, not native Metal.
        runner = "macos-15" if subshard == "impeller" else "ubuntu-22.04"
        key = runner, "framework_tests", subshard
        selected.setdefault(key, (0, {"runner": runner, "shard": "framework_tests", "subshard": subshard,
                                     "name": f"{runner}-framework_tests-{subshard}",
                                     "source_target": "frameworkTestsRunner", "jdk": ""}))
    entries = [entry for _, entry in selected.values()]
    present = {entry["shard"] for entry in entries}
    missing = CORE_SDK_SHARDS - present
    if missing:
        raise ValueError(f"Requested revision has no supported CI entries for: {sorted(missing)}")
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


def plan_diagnostics(matrix: dict) -> list[str]:
    entries = matrix["include"]
    present = {entry["shard"] for entry in entries}
    diagnostics = []
    for shard in sorted(OPTIONAL_SDK_SHARDS - present):
        message = f"{shard}: not configured by the requested Flutter ref; no job scheduled."
        if shard == "tool_tests_commands" and any(entry["shard"] == "tool_tests" and entry["subshard"] == "commands" for entry in entries):
            message += " Command tests are covered by this ref's legacy tool_tests/commands entry."
        if shard == "android_java17_tool_integration_tests" and "android_java11_tool_integration_tests" in present:
            message += " This ref configures android_java11_tool_integration_tests instead."
        diagnostics.append(message)
    return diagnostics


def report_plan(matrix: dict) -> None:
    diagnostics = plan_diagnostics(matrix)
    for message in diagnostics:
        print(f"::notice::{message}", file=sys.stderr)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as output:
            output.write(f"\nSDK plan: {len(matrix['include'])} partitions from the requested ref.\n")
            for message in diagnostics:
                output.write(f"\n- {message}\n")


def read_configuration(path: Path) -> dict:
    import yaml
    # Flutter properties are consumed as strings. YAML numeric coercion turns
    # unquoted 1_1 into the integer 11 and would select the wrong partition.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def strict_goldens(source: str) -> str:
    """Fail closed on missing baselines and network skips, using injected Platform."""
    patches = [
        ("    log('Skipping \"$golden\" test: $reason');",
         "    if (platform.environment['FLUTTER_STRICT_GOLDENS'] == 'true') {\n"
         "      throw FlutterError('Strict golden comparison skipped: $golden; $reason');\n"
         "    }\n    log('Skipping \"$golden\" test: $reason');"),
        ("      await update(golden, imageBytes);\n      return true;\n    }\n\n    final List<int> goldenBytes",
         "      await update(golden, imageBytes);\n"
         "      if (platform.environment['FLUTTER_STRICT_GOLDENS'] == 'true') {\n"
         "        throw FlutterError('Strict golden comparison has no baseline: $golden');\n"
         "      }\n      return true;\n    }\n\n    final List<int> goldenBytes"),
        ("    if (result.passed) {\n      result.dispose();\n      return true;",
         "    if (result.passed) {\n      result.dispose();\n"
         "      if (platform.environment['FLUTTER_STRICT_GOLDENS'] == 'true') {\n"
         "        log('Strict golden compared: $golden');\n"
         "      }\n      return true;"),
    ]
    legacy_missing = (
        "      update(golden, imageBytes);\n      return true;\n    }\n\n    ComparisonResult result;",
        "      if (platform.environment['FLUTTER_STRICT_GOLDENS'] == 'true') {\n"
        "        await update(golden, imageBytes);\n"
        "        throw FlutterError('Strict golden comparison has no baseline: $golden');\n"
        "      }\n      update(golden, imageBytes);\n      return true;\n    }\n\n    ComparisonResult result;",
    )
    missing_variants = [patches[1], legacy_missing]
    if sum(source.count(old) for old, _ in missing_variants) != 1:
        raise ValueError("Golden comparator differs from the supported contract; refusing a partial strict patch")
    patches[1] = next(patch for patch in missing_variants if source.count(patch[0]) == 1)
    for exception in ("io.OSError", "io.SocketException", "FormatException"):
        old = f"    }} on {exception} catch (_) {{\n      return FlutterSkippingFileComparator("
        new = f"    }} on {exception} catch (_) {{\n" + (
            "      if (platform.environment['FLUTTER_STRICT_GOLDENS'] == 'true') {\n"
            "        rethrow;\n      }\n      return FlutterSkippingFileComparator("
        )
        patches.append((old, new))
    for old, new in patches:
        if source.count(old) != 1:
            raise ValueError("Golden comparator differs from the supported contract; refusing a partial strict patch")
        source = source.replace(old, new, 1)
    return source


def host_configuration(root: Path, mode: str, system: str, machine: str) -> tuple[dict, Path]:
    if system == "Linux":
        filename = "linux_unopt.json" if mode == "debug" else "linux_host_engine_test.json"
        name = "ci/host_debug_unopt" if mode == "debug" else f"ci/host_{mode}_test"
    elif system == "Darwin" and machine == "arm64":
        filename = "mac_unopt.json"
        name = "ci/host_debug_unopt_arm64" if mode == "debug" else f"ci/host_{mode}_arm64_tests"
    else:
        raise ValueError(f"Unsupported host: {system}/{machine}; use Linux x64 or macOS arm64")
    path = root / "engine/src/flutter/ci/builders" / filename
    builds = json.loads(path.read_text())["builds"]
    matches = [build for build in builds if build["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Requested revision is missing host test configuration {name} in {path}")
    return matches[0], path


def local_gn(arguments: list[str]) -> list[str]:
    removed = {"--rbe", "--no-rbe", "--goma", "--no-goma", "--xcode-symlinks"}
    return [arg for arg in arguments if arg not in removed] + ["--no-rbe", "--no-goma"]


def mac_engine_filter(source: str) -> list[str]:
    block = source.split("def run_cc_tests(", 1)[1].split("\ndef run_engine_benchmarks(", 1)[0]
    calls = re.findall(r"make_test\(\s*'([^']+)'|run_engine_executable\(\s*build_dir,\s*'([^']+)'", block)
    names = {a or b for a, b in calls}
    if not GPU_EXECUTABLES <= names:
        raise ValueError("Cannot verify macOS GPU exclusions in requested run_tests.py")
    return sorted(names - GPU_EXECUTABLES)


def software_dart_backends(source: str) -> str:
    original = "  if is_mac():\n    impeller_backends.append('metal')"
    if source.count(original) != 1:
        raise ValueError("Cannot verify the native Metal Dart test variant in requested run_tests.py")
    return source.replace(original, "  # Hosted CI runs all Dart cases with Skia, SwiftShader Vulkan, and SwANGLE.\n"
                          "  # The additional native Metal variant requires a GPU runner.", 1)


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
        patterns = ["error.log", "test_results.json", "**/failures/**", "**/*.xcresult/Info.plist",
                    "engine/src/out/run_tests.log", "bin/cache/pkg/skia_goldens/**/*.png"]
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if path.is_file() and not path.is_symlink() and path.stat().st_size < 20_000_000:
                    destination = self.outputs / "artifacts" / path.relative_to(self.root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
        compared = self.log.read_text().count("Strict golden compared:") if self.log.exists() else 0
        (self.outputs / "golden-count.json").write_text(json.dumps({"successful_comparisons_logged": compared}) + "\n")


def prepare_ktlint(runner: Runner) -> None:
    target = runner.outputs / "bin/ktlint"
    target.parent.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/ktlint/ktlint/releases/download/1.5.0/ktlint"
    urllib.request.urlretrieve(url, target)
    expected = "a16be01dcc480aab2f55f444b620142152f66e31564b3b9376506d624c28a2ad"
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise ValueError("ktlint 1.5.0 checksum mismatch")
    target.chmod(0o755)
    os.environ["PATH"] = str(target.parent) + os.pathsep + os.environ["PATH"]


def run_sdk(runner: Runner, shard: str, subshard: str) -> None:
    if shard not in SDK_SHARDS:
        raise ValueError(f"Unsupported SDK shard: {shard}")
    if not os.environ.get("FLUTTER_PREBUILT_ENGINE_VERSION"):
        raise ValueError("SDK tests require an explicit official FLUTTER_PREBUILT_ENGINE_VERSION")
    flutter = str(runner.root / "bin/flutter")
    runner.manifest(kind="sdk", shard=shard, subshard=subshard, native_engine_coverage=False,
                    golden_policy="strict public baseline; missing/network skip fails",
                    impeller_backend="headless Vulkan with statically linked SwiftShader" if subshard == "impeller" else None)
    runner.run([flutter, "config", "--enable-web", "--enable-macos-desktop" if platform.system() == "Darwin" else "--enable-linux-desktop"])
    runner.run([flutter, "config", f"--jdk-dir={os.environ['JAVA_HOME']}"])
    runner.run([flutter, "precache", "--android", "--web", "--ios" if platform.system() == "Darwin" else "--linux", *(["--macos"] if platform.system() == "Darwin" else [])])
    runner.run([flutter, "update-packages"])
    if shard == "android_preview_tool_integration_tests":
        sdk = runner.outputs / "android-preview-sdk"
        ensure = runner.outputs / "android-sdk.ensure"
        ensure.write_text("flutter/android/sdk/all/linux-amd64 version:baklava_v1\n")
        runner.run(["cipd", "ensure", "-root", str(sdk), "-ensure-file", str(ensure)])
        runner.run([flutter, "config", f"--android-sdk={sdk}"])
        os.environ.update(ANDROID_HOME=str(sdk), ANDROID_SDK_ROOT=str(sdk))
    if shard == "analyze":
        prepare_ktlint(runner)
    env = os.environ.copy()
    env.update(SHARD=shard, SUBSHARD=subshard, FLUTTER_ROOT=str(runner.root))
    if shard == "analyze":
        env["LUCI_CI"] = "true"
    if shard == "framework_tests":
        path = runner.root / GOLDENS_PATH
        original = path.read_text()
        patched = strict_goldens(original)
        (runner.outputs / "strict-goldens.patch").write_text("".join(difflib.unified_diff(
            original.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile=str(GOLDENS_PATH), tofile=str(GOLDENS_PATH))))
        path.write_text(patched)
        env["FLUTTER_STRICT_GOLDENS"] = "true"
    try:
        runner.run([str(runner.root / "bin/dart"), "--enable-asserts", "dev/bots/test.dart"], env=env)
    finally:
        if shard == "framework_tests":
            path.write_text(original)


def run_host(runner: Runner, mode: str) -> None:
    build, builder_path = host_configuration(runner.root, mode, platform.system(), platform.machine())
    source = runner.root / "engine/src"
    gn = local_gn(build["gn"])
    variant = build["ninja"]["config"]
    tests = [test for test in build["tests"] if test.get("script") == "flutter/testing/run_tests.py"]
    if len(tests) != 1:
        raise ValueError("Host builder must export exactly one run_tests.py invocation")
    args = tests[0]["parameters"][:]
    index = args.index("--type") + 1
    supported_types = {"engine", "dart", "dart-host"}
    if "font-subset" in args[index].split(","):
        supported_types.add("font-subset")
    args[index] = ",".join(sorted(supported_types))
    args = [arg.replace("${FLUTTER_LOGS_DIR}", str(runner.outputs)) for arg in args]
    if "--no-skia-gold" in args:
        args.remove("--no-skia-gold")
    excluded = []
    if platform.system() == "Darwin":
        excluded = sorted(GPU_EXECUTABLES)
        args += ["--engine-filter", ",".join(mac_engine_filter((source / "flutter/testing/run_tests.py").read_text()))]
    runner.manifest(kind="host-engine-source", mode=mode, builder=str(builder_path.relative_to(runner.root)),
                    variant=variant, gn=gn, ninja=build["ninja"], tests=args,
                    excluded_gpu_executables=excluded,
                    excluded_dart_backends=["metal"] if excluded else [],
                    exclusion_reason="GitHub-hosted macOS has no guaranteed Metal device; requires a GPU self-hosted runner" if excluded else None)
    runner.run([str(source / "flutter/tools/gn"), *gn], cwd=source)
    runner.run(["ninja", "-C", f"out/{variant}", "-j2", *build["ninja"].get("targets", [])], cwd=source)
    test_script = source / "flutter/testing/run_tests.py"
    original = test_script.read_text()
    if excluded:
        patched = software_dart_backends(original)
        (runner.outputs / "software-dart-backends.patch").write_text("".join(difflib.unified_diff(
            original.splitlines(keepends=True), patched.splitlines(keepends=True),
            fromfile="testing/run_tests.py", tofile="testing/run_tests.py")))
        test_script.write_text(patched)
    try:
        runner.run(["python3", "flutter/testing/run_tests.py", *args], cwd=source)
    finally:
        if excluded:
            test_script.write_text(original)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--outputs", type=Path, default=Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    sdk = subparsers.add_parser("sdk")
    sdk.add_argument("--shard", required=True)
    sdk.add_argument("--subshard", default="")
    host = subparsers.add_parser("engine-host")
    host.add_argument("--mode", choices=["debug", "profile", "release"], required=True)
    args = parser.parse_args()
    if args.command == "plan":
        configuration = read_configuration(args.flutter_root / ".ci.yaml")
        matrix = ci_matrix(configuration, (args.flutter_root / "dev/bots/suite_runners/run_framework_tests.dart").read_text())
        report_plan(matrix)
        print("matrix=" + json.dumps(matrix, separators=(",", ":")))
        return 0
    name = f"sdk-{args.shard}-{args.subshard or 'all'}" if args.command == "sdk" else f"engine-host-{args.mode}"
    runner = Runner(args.flutter_root, args.outputs, name)
    result = "failure"
    try:
        if args.command == "sdk":
            run_sdk(runner, args.shard, args.subshard)
        else:
            run_host(runner, args.mode)
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
                output.write(f"\n- {name}: **{result}**. Commands, engine provenance and golden coverage are in the artifact.\n")


if __name__ == "__main__":
    raise SystemExit(main())
