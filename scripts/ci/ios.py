#!/usr/bin/env python3
"""Run Flutter's iOS CI entry points from the selected Flutter checkout."""

import argparse
import difflib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys

import yaml

from official_ci import Policy


BASELINE_TASKS = (
    "module_test_ios",
    "build_ios_framework_module_test",
    "plugin_test_ios",
    "plugin_test_darwin",
)
# These tasks create their own simulator. The selected ref's .ci.yaml and task
# source are checked before execution, rather than silently skipping old refs.
SIMULATOR_TASKS = (
    "channels_integration_test_ios",
    "integration_test_test_ios",
    "integration_ui_ios_driver",
    "integration_ui_ios_frame_number",
    "integration_ui_ios_keyboard_resize",
    "integration_ui_ios_textfield",
    "keyboard_hot_restart_ios",
    "platform_channel_sample_test_ios",
    "platform_channel_sample_test_swift",
    "platform_interaction_test_ios",
    "native_platform_view_ui_tests_ios",
    "ios_universal_link_test",
    "ios_defines_test",
    "route_test_ios",
    "flavors_test_ios",
    "native_assets_ios_simulator",
    "module_uiscene_test_ios",
    "uiscene_plugin_lifecycle_test_ios",
    "hello_world_impeller_ios_sdfs",
    "hot_mode_dev_cycle_ios_simulator",
    "external_textures_integration_test_ios",
)


def run(command, *, cwd, log=None, env=None, check=True):
    """Stream output while preserving the command's status and complete log."""
    command = [str(value) for value in command]
    print("+ " + shlex.join(command), flush=True)
    if log is None:
        result = subprocess.run(command, cwd=cwd, env=env, check=False)
        code = result.returncode
    else:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as output:
            output.write("+ " + shlex.join(command) + "\n")
            output.flush()
            process = subprocess.Popen(
                command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in process.stdout:
                print(line, end="", flush=True)
                output.write(line)
            code = process.wait()
    if check and code:
        raise subprocess.CalledProcessError(code, command)
    return code


def local_gn_flags(flags):
    """Keep the ref's build configuration, removing Google remote execution."""
    result = []
    skip_next = False
    for flag in flags:
        if skip_next:
            skip_next = False
            continue
        name = flag.split("=", 1)[0]
        if name in ("--rbe", "--goma", "--xcode-symlinks"):
            continue
        if name.startswith("--rbe-") or name in ("--goma-dir", "--depot-tools"):
            skip_next = "=" not in flag
            continue
        result.append(flag)
    for flag in ("--no-rbe", "--no-goma"):
        if flag not in result:
            result.append(flag)
    return result


def find_build(root, kind, extension_safe=False):
    document = json.loads((root / "engine/src/flutter/ci/builders/mac_unopt.json").read_text())
    candidates = []
    for build in document["builds"]:
        flags = build["gn"]
        if kind == "ios":
            matches = (
                "--ios" in flags and "--simulator" in flags
                and "--unoptimized" in flags and "arm64" in flags
                and ("--darwin-extension-safe" in flags) == extension_safe
            )
        else:
            matches = (
                "--ios" not in flags and "--mac" not in flags
                and "--unoptimized" in flags and "arm64" in flags
                and "debug" in flags
            )
        if matches:
            candidates.append(build)
    if len(candidates) != 1:
        raise ValueError(f"Expected one {kind} arm64 unoptimized config, found {len(candidates)}")
    build = candidates[0]
    variant = build["ninja"]["config"]
    if not re.fullmatch(r"[A-Za-z0-9_/-]+", variant) or ".." in variant.split("/"):
        raise ValueError(f"Unsafe engine variant: {variant!r}")
    return build


def build_engine(root, build, results, jobs):
    src = root / "engine/src"
    variant = build["ninja"]["config"]
    flags = local_gn_flags(build["gn"])
    results.mkdir(parents=True, exist_ok=True)
    (results / (variant.replace("/", "_") + "-build.json")).write_text(
        json.dumps({"variant": variant, "gn": flags,
                    "targets": build["ninja"]["targets"]}, indent=2)
        + "\n"
    )
    run(["./flutter/tools/gn", *flags], cwd=src,
        log=results / (variant.replace("/", "_") + "-gn.log"))
    targets = build["ninja"]["targets"]
    run(["ninja", "-j", str(jobs), "-C", "out/" + variant, *targets],
        cwd=src, log=results / (variant.replace("/", "_") + "-ninja.log"))
    return variant


def select_runtime(document, device_type="com.apple.CoreSimulator.SimDeviceType.iPhone-SE-3rd-generation"):
    runtimes = [
        runtime for runtime in document["runtimes"]
        if runtime.get("isAvailable") and runtime["identifier"].startswith("com.apple.CoreSimulator.SimRuntime.iOS-")
        and (not runtime.get("supportedDeviceTypes") or any(
            device.get("identifier") == device_type for device in runtime["supportedDeviceTypes"]
        ))
    ]
    if not runtimes:
        raise ValueError(f"No available iOS simulator runtime supports {device_type}")
    return max(runtimes, key=lambda runtime: tuple(int(x) for x in runtime["version"].split(".")))


def patch_scenario_text(content):
    # Current upstream validates argument count before parsing named options.
    # Remove only that obsolete count check, leaving test code/assertions intact.
    return content.replace("args.length > 1 || ", "", 1)


def embedding_runtime_options(content, runtime):
    if re.search(r"add_argument\(\s*['\"]--ios-runtime['\"]", content):
        return content, ["--ios-runtime", runtime]
    # Older runners use either %-formatting or an f-string without a runtime
    # CLI option. Give their simctl command the selected runtime explicitly.
    if not re.fullmatch(r"com\.apple\.CoreSimulator\.SimRuntime\.iOS-\d+(?:-\d+)*", runtime):
        raise ValueError(f"Invalid iOS simulator runtime identifier: {runtime!r}")
    expressions = (
        "'%s com.apple.CoreSimulator.SimDeviceType.iPhone-11' % new_simulator_name",
        "f'{new_simulator_name} com.apple.CoreSimulator.SimDeviceType.iPhone-11'",
    )
    if sum(content.count(expression) for expression in expressions) != 1:
        raise ValueError("This ref's embedding runner has an unsupported simulator creation command")
    expression = next(expression for expression in expressions if expression in content)
    selected = expression.replace("iPhone-11'", "iPhone-11 " + runtime + "'", 1)
    patched = content.replace(expression, selected, 1)
    compile(patched, "run_tests.py", "exec")
    return patched, []


def patch_config_only_codesigning(content):
    # These module setup commands only generate Xcode configuration. Keep the
    # subsequent builds and tests intact, including their modes and assertions.
    command = r"(flutter\(\s*'build',\s*options: <String>\['ios', '--config-only')(, '-v')?\]"
    return re.sub(command, lambda match: match[1] + ", '--no-codesign'" + (match[2] or "") + "]", content)


def scenario_working_directory(src, shell):
    # Older wrappers invoke this Dart path relative to the working directory;
    # newer wrappers use $SCRIPT_DIR and remain runnable from engine/src.
    relative_entrypoint = r"^\s*testing/ios_scenario_app/bin/run_ios_tests\.dart(?:\s|$)"
    return src / "flutter" if re.search(relative_entrypoint, shell, re.MULTILINE) else src


def save_patch(root, path, old, new, results, label):
    if old == new:
        return
    name = str(path.relative_to(root))
    patch = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="a/" + name, tofile="b/" + name,
    ))
    results.mkdir(parents=True, exist_ok=True)
    (results / (label + ".patch")).write_text(patch)
    path.write_text(new)


def run_native(root, results, jobs, extension_safe, policy=None):
    policy = policy or Policy.load(root)
    if not policy.engine_enabled(root, "mac_unopt"):
        raise ValueError("The selected official CI does not schedule mac_unopt")
    build = find_build(root, "ios", extension_safe)
    suites = native_suites(build, policy)
    if not suites:
        raise ValueError("The selected official CI schedules no tests for this iOS variant")
    runtime_document = json.loads(subprocess.check_output(
        ["xcrun", "simctl", "list", "runtimes", "--json"], text=True
    ))
    runtime = select_runtime(runtime_document)
    embedding_runtime = select_runtime(
        runtime_document, "com.apple.CoreSimulator.SimDeviceType.iPhone-11"
    )
    (results / "simulator-runtime.json").write_text(json.dumps(
        {"scenario": runtime, "embedding": embedding_runtime}, indent=2
    ) + "\n")
    build_engine(root, build, results, jobs)
    src = root / "engine/src"
    statuses = {}
    # Preserve the official test selection and run the remaining suite after a failure.
    if "embedding" in suites:
        embedding_runner = src / "flutter/testing/run_tests.py"
        embedding_original = embedding_runner.read_text()
        embedding_patched, runtime_options = embedding_runtime_options(
            embedding_original, embedding_runtime["identifier"]
        )
        save_patch(root, embedding_runner, embedding_original, embedding_patched, results, "embedding-runner")
        try:
            statuses["embedding"] = run([
                "python3", "flutter/testing/run_tests.py", *suites["embedding"]["parameters"],
                *runtime_options,
            ], cwd=src, log=results / "embedding.log", check=False)
        finally:
            if embedding_patched != embedding_original:
                embedding_runner.write_text(embedding_original)
    if "scenario" in suites:
        runner = src / "flutter/testing/ios_scenario_app/bin/run_ios_tests.dart"
        old = runner.read_text()
        new = patch_scenario_text(old)
        if "addOption(" not in new or "'os-runtime'" not in new or "'os-version'" not in new:
            raise ValueError("This ref's Scenario runner does not support runtime selection")
        scenario_shell = src / "flutter/testing/ios_scenario_app/run_ios_tests.sh"
        scenario_cwd = scenario_working_directory(src, scenario_shell.read_text())
        save_patch(root, runner, old, new, results, "scenario-runner")
        try:
            statuses["scenario"] = run([
                str(scenario_shell), *suites["scenario"]["parameters"],
                "--os-runtime=" + runtime["identifier"],
                "--os-version=" + runtime["version"],
                "--dump-xcresult-on-failure=" + str(results),
            ], cwd=scenario_cwd, log=results / "scenario.log", check=False)
        finally:
            runner.write_text(old)
    (results / "results.json").write_text(json.dumps(statuses, indent=2) + "\n")
    return int(any(statuses.values()))


def read_configuration(root):
    return yaml.safe_load((root / ".ci.yaml").read_text())


def target_runner(configuration, target):
    """Honor the official target CPU; an unconstrained Mac can use arm64."""
    prefix = target["name"].split()[0].lower()
    properties = {**configuration.get("platform_properties", {}).get(prefix, {}).get("properties", {}),
                  **target.get("properties", {})}
    if not prefix.startswith("mac") or properties.get("device_type", "none") != "none":
        return None
    cpu = properties.get("cpu", "x86" if prefix == "mac_x64" else "arm64")
    if cpu == "arm64":
        return "macos-15", "arm64"
    if cpu in {"x86", "x64", "x86_64"}:
        return "macos-15-intel", "x64"
    raise ValueError(f"Unsupported iOS task CPU for {target['name']}: {cpu}")


def task_entries(root, candidates, *, simulator=False, unavailable=None, policy=None):
    policy = policy or Policy.load(root)
    configuration = read_configuration(root)
    configured = configuration["targets"]
    selected = policy.targets(configuration)
    entries = []
    for task in candidates:
        source = root / "dev/devicelab/bin/tasks" / (task + ".dart")
        declared = [target for target in configured
                    if target.get("properties", {}).get("task_name") == task]
        targets = [target for target in selected
                   if target.get("properties", {}).get("task_name") == task]
        reason = None
        if not source.is_file():
            reason = "task_file_absent"
        elif not declared:
            reason = "not_configured_in_ci"
        elif not targets:
            reason = "; ".join(sorted({policy.rejection(target, configuration) for target in declared}))
        elif simulator and "testWithNewIOSSimulator" not in source.read_text():
            reason = "requires_physical_device_in_this_ref"
        if reason:
            if unavailable is not None:
                unavailable.append({"task": task, "reason": reason})
            continue
        hosted = False
        for target in targets:
            runner = target_runner(configuration, target)
            if runner is None:
                continue
            hosted = True
            entries.append({"task": task, "runner": runner[0], "cpu": runner[1],
                            "source_target": target["name"],
                            "id": re.sub(r"[^A-Za-z0-9_-]", "-", target["name"])})
        if not hosted and unavailable is not None:
            unavailable.append({"task": task, "reason": "requires_physical_device_in_this_ref"})
    if simulator:
        scheduled = {target.get("properties", {}).get("task_name") for target in selected}
        for source in sorted((root / "dev/devicelab/bin/tasks").glob("*.dart")):
            if (source.stem in scheduled and source.stem not in SIMULATOR_TASKS
                    and source.stem not in BASELINE_TASKS
                    and "testWithNewIOSSimulator" in source.read_text()):
                raise ValueError(f"New simulator task requires a build-mode review: {source.stem}")
    return entries


def simulator_tasks(root, unavailable=None, policy=None):
    return list(dict.fromkeys(entry["task"] for entry in task_entries(
        root, SIMULATOR_TASKS, simulator=True, unavailable=unavailable, policy=policy)))


def baseline_tasks(root, unavailable=None, policy=None):
    return list(dict.fromkeys(entry["task"] for entry in task_entries(
        root, BASELINE_TASKS, unavailable=unavailable, policy=policy)))


def native_suites(build, policy):
    """Use the builder's scheduled suites, including its per-test branch guard."""
    selected = {}
    for test in build.get("tests", []):
        if not re.match(".*" + test.get("test_if", ""), policy.branch):
            continue
        script = test.get("script")
        if script == "flutter/testing/run_tests.py" and "objc" in test.get("parameters", []):
            selected["embedding"] = test
        elif script == "flutter/testing/ios_scenario_app/run_ios_tests.sh":
            selected["scenario"] = test
        else:
            raise ValueError(f"Unreviewed iOS native test in {build['ninja']['config']}: {script}")
    return selected


def native_matrix(root, policy):
    if not policy.engine_enabled(root, "mac_unopt"):
        return {"include": []}
    entries = []
    for extension in (False, True):
        build = find_build(root, "ios", extension)
        if native_suites(build, policy):
            entries.append({"variant": "extension-safe" if extension else "normal"})
    return {"include": entries}


def coverage_summary(tasks, unavailable):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
        output.write("### iOS task availability\n\n")
        output.write("Enabled: " + ", ".join("`" + task + "`" for task in tasks) + "\n\n")
        if unavailable:
            output.write("Tasks unavailable in the selected ref:\n\n")
            for item in unavailable:
                output.write("- `" + item["task"] + "`: " + item["reason"] + "\n")


def plan_tasks(root, results, policy=None):
    policy = policy or Policy.load(root)
    unavailable = []
    simulator = task_entries(root, SIMULATOR_TASKS, simulator=True, unavailable=unavailable, policy=policy)
    baseline = task_entries(root, BASELINE_TASKS, unavailable=unavailable, policy=policy)
    plan = {"simulator": {"include": simulator}, "baseline": {"include": baseline},
            "native": native_matrix(root, policy), "unavailable": unavailable}
    results.mkdir(parents=True, exist_ok=True)
    (results / "ios-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    coverage_summary([entry["source_target"] for entry in [*simulator, *baseline]], unavailable)
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            for name in ("simulator", "baseline", "native"):
                output.write(name + "=" + json.dumps(plan[name], separators=(",", ":")) + "\n")
                output.write("has-" + name + "=" + str(bool(plan[name]["include"])).lower() + "\n")
    print(json.dumps(plan, indent=2))
    return plan


def run_devicelab(root, results, task, source_target, *, simulator=False, policy=None):
    policy = policy or Policy.load(root)
    candidates = SIMULATOR_TASKS if simulator else BASELINE_TASKS
    entries = task_entries(root, candidates, simulator=simulator, policy=policy)
    entry = next((entry for entry in entries
                  if entry["task"] == task and entry["source_target"] == source_target), None)
    if entry is None:
        raise ValueError(f"Task is not scheduled in the selected official CI: {source_target}/{task}")
    expected_cpu = "arm64" if entry["cpu"] == "arm64" else "x86_64"
    if sys.platform != "darwin" or platform.machine() != expected_cpu:
        raise ValueError(f"{source_target} requires macOS/{expected_cpu}")
    directory = root / "dev/devicelab"
    dart = root / "bin/cache/dart-sdk/bin/dart"
    run([dart, "pub", "get"], cwd=directory, log=results / "pub-get.log")
    # Official DeviceLab tasks normally use published Engine artifacts. Keep
    # their original build modes and CPU instead of injecting a local variant.
    run([root / "bin/flutter", "precache", "--ios", "--macos"],
        cwd=root, log=results / "precache.log")
    (results / "coverage.json").write_text(json.dumps({
        "engine_origin": "selected-framework-and-tools-with-baseline-engine",
        "task": task, "source_target": source_target, "cpu": entry["cpu"],
    }, indent=2) + "\n")
    task_source = directory / "bin/tasks" / (task + ".dart")
    original = None
    if task in {"module_test_ios", "module_uiscene_test_ios"}:
        original = task_source.read_bytes()
        content = original.decode("utf-8")
        patched = patch_config_only_codesigning(content)
        if patched == content:
            original = None
    try:
        if original is not None:
            save_patch(root, task_source, content, patched, results, task + "-config-only")
        code = run([dart, "bin/test_runner.dart", "test", "--exit", "--task", task,
                    "--results-file", str(results / (task + "-devicelab.json"))],
                   cwd=directory, log=results / (task + ".log"), check=False)
    finally:
        if original is not None:
            task_source.write_bytes(original)
    (results / "results.json").write_text(json.dumps({task: code}, indent=2) + "\n")
    return int(bool(code))


def select_xcode(version):
    candidates = []
    for app in Path("/Applications").glob("Xcode*.app"):
        if "beta" in app.name.lower():
            continue
        developer = app / "Contents/Developer"
        env = {**os.environ, "DEVELOPER_DIR": str(developer)}
        output = subprocess.check_output(["xcodebuild", "-version"], env=env, text=True)
        match = re.search(r"^Xcode ([\d.]+)$", output, re.MULTILINE)
        if match and (not version or match.group(1) == version):
            candidates.append((tuple(int(x) for x in match.group(1).split(".")), developer))
    if not candidates:
        raise ValueError(f"No installed stable Xcode matches {version or 'the requested environment'}")
    developer = max(candidates)[1]
    run(["sudo", "xcode-select", "--switch", developer], cwd=Path.cwd())
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        output.write("DEVELOPER_DIR=" + str(developer) + "\n")
    print("Selected " + str(developer), flush=True)


def diagnostics(results):
    results.mkdir(parents=True, exist_ok=True)
    for name, command in (
        ("xcode", ["xcodebuild", "-version"]),
        ("simulators", ["xcrun", "simctl", "list", "--json"]),
        ("disk", ["df", "-h"]),
        ("simulator-errors", ["log", "show", "--last", "15m", "--style", "compact",
                              "--predicate", 'process == "Simulator" OR process == "CoreSimulatorService"']),
    ):
        try:
            run(command, cwd=Path.cwd(), log=results / (name + ".log"), check=False)
        except OSError as error:
            (results / (name + ".log")).write_text(str(error) + "\n")


def prepare_native_tools(results):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("Source iOS suites require an arm64 macOS runner")
    run(["ninja", "--version"], cwd=Path.cwd(), log=results / "ninja-version.log")
    if run(["xcrun", "--sdk", "macosx", "metal", "--version"], cwd=Path.cwd(),
           log=results / "metal-version.log", check=False):
        run(["xcodebuild", "-downloadComponent", "MetalToolchain"], cwd=Path.cwd(),
            log=results / "metal-toolchain-download.log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select-xcode", "native", "simulator", "baseline", "diagnostics", "plan"))
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--results", type=Path, default=Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")))
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--extension-safe", action="store_true")
    parser.add_argument("--task", choices=(*BASELINE_TASKS, *SIMULATOR_TASKS))
    parser.add_argument("--source-target")
    parser.add_argument("--xcode-version", default="")
    args = parser.parse_args()
    if args.command == "select-xcode":
        select_xcode(args.xcode_version)
        return 0
    root, results = args.flutter_root.resolve(), args.results.resolve()
    results.mkdir(parents=True, exist_ok=True)
    if args.command == "diagnostics":
        diagnostics(results)
        return 0
    if args.command == "plan":
        plan_tasks(root, results)
        return 0
    os.environ["FLUTTER_TEST_OUTPUTS_DIR"] = str(results)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.command == "native":
        prepare_native_tools(results)
    if args.command == "native":
        return run_native(root, results, args.jobs, args.extension_safe)
    if not args.task or not args.source_target:
        parser.error("simulator/baseline requires --task and --source-target")
    return run_devicelab(root, results, args.task, args.source_target,
                        simulator=args.command == "simulator")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
