#!/usr/bin/env python3
"""Run Flutter's iOS CI entry points from the selected Flutter checkout."""

import argparse
from contextlib import contextmanager, nullcontext
import difflib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


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
    "uiscene_plugin_lifecycle_test_ios",
    "hello_world_impeller_ios_sdfs",
    "hot_mode_dev_cycle_ios_simulator",
    "external_textures_integration_test_ios",
)
CORE_SIMULATOR_TASKS = ("native_assets_ios_simulator", "hot_mode_dev_cycle_ios_simulator")


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


def build_engine(root, build, results, jobs, *, app_artifacts=False):
    src = root / "engine/src"
    variant = build["ninja"]["config"]
    flags = local_gn_flags(build["gn"])
    if app_artifacts:
        # Flutter's Xcode settings compute the engine src root as ../.. from
        # the output directory. Nested CI variants would produce a wrong root.
        original_variant = variant
        variant = variant.rsplit("/", 1)[-1]
        flags = [variant if value == original_variant else value for value in flags]
    results.mkdir(parents=True, exist_ok=True)
    (results / (variant.replace("/", "_") + "-build.json")).write_text(
        json.dumps({"variant": variant, "gn": flags,
                    "targets": build["ninja"]["targets"], "app_artifacts": app_artifacts}, indent=2)
        + "\n"
    )
    run(["./flutter/tools/gn", *flags], cwd=src,
        log=results / (variant.replace("/", "_") + "-gn.log"))
    targets = build["ninja"]["targets"]
    if app_artifacts and "default" not in targets:
        targets = [*targets, "default"]
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
    # Flutter 3.35's runner has no runtime CLI option. Its shell command must
    # receive the selected runtime explicitly rather than simctl's default.
    if not re.fullmatch(r"com\.apple\.CoreSimulator\.SimRuntime\.iOS-\d+(?:-\d+)*", runtime):
        raise ValueError(f"Invalid iOS simulator runtime identifier: {runtime!r}")
    legacy = "'%s com.apple.CoreSimulator.SimDeviceType.iPhone-11' % new_simulator_name"
    if content.count(legacy) != 1:
        raise ValueError("This ref's embedding runner has an unsupported simulator creation command")
    selected = "'%s com.apple.CoreSimulator.SimDeviceType.iPhone-11 " + runtime + "' % new_simulator_name"
    patched = content.replace(legacy, selected, 1)
    compile(patched, "run_tests.py", "exec")
    return patched, []


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


def run_native(root, results, jobs, extension_safe):
    build = find_build(root, "ios", extension_safe)
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
    variant = build_engine(root, build, results, jobs)
    src = root / "engine/src"
    # Run both suites even when the first suite fails.
    embedding_runner = src / "flutter/testing/run_tests.py"
    embedding_original = embedding_runner.read_text()
    embedding_patched, runtime_options = embedding_runtime_options(
        embedding_original, embedding_runtime["identifier"]
    )
    save_patch(root, embedding_runner, embedding_original, embedding_patched, results, "embedding-runner")
    try:
        statuses = {"embedding": run([
            "python3", "flutter/testing/run_tests.py", "--variant", variant,
            "--type", "objc", "--ios-variant", variant, *runtime_options,
        ], cwd=src, log=results / "embedding.log", check=False)}
    finally:
        if embedding_patched != embedding_original:
            embedding_runner.write_text(embedding_original)
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
            str(scenario_shell), variant,
            "--os-runtime=" + runtime["identifier"],
            "--os-version=" + runtime["version"],
            "--dump-xcresult-on-failure=" + str(results),
        ], cwd=scenario_cwd, log=results / "scenario.log", check=False)
    finally:
        runner.write_text(old)
    (results / "results.json").write_text(json.dumps(statuses, indent=2) + "\n")
    return int(any(statuses.values()))


def task_is_configured(root, task):
    # task_name is a scalar everywhere in .ci.yaml; no general YAML parser is
    # needed for this membership check.
    config = (root / ".ci.yaml").read_text()
    return bool(re.search(r"^\s+task_name:\s*[\"']?" + re.escape(task) + r"[\"']?\s*$", config, re.MULTILINE))


def task_unavailability(root, task, *, simulator=False):
    source = root / "dev/devicelab/bin/tasks" / (task + ".dart")
    if not source.is_file():
        return "task_file_absent"
    if not task_is_configured(root, task):
        return "not_configured_in_ci"
    if simulator and "testWithNewIOSSimulator" not in source.read_text():
        return "requires_physical_device_in_this_ref"
    return None


def simulator_tasks(root, unavailable=None):
    directory = root / "dev/devicelab/bin/tasks"
    tasks = []
    missing = []
    for task in SIMULATOR_TASKS:
        reason = task_unavailability(root, task, simulator=True)
        if reason:
            missing.append({"task": task, "reason": reason})
        else:
            tasks.append(task)
    for task in CORE_SIMULATOR_TASKS:
        if task not in tasks:
            raise ValueError(f"Required core simulator task is unavailable in this ref: {task}")
    if not tasks:
        raise ValueError("No configured simulator tasks are available in this ref")
    # A simulator task can still perform device/profile/release builds inside
    # its helpers. Require a mode review for upstream additions before passing
    # a fixed debug local engine; silently guessing would misreport coverage.
    for source in sorted(directory.glob("*.dart")):
        if ("testWithNewIOSSimulator" in source.read_text()
                and source.stem not in tasks and source.stem not in BASELINE_TASKS
                and task_is_configured(root, source.stem)):
            raise ValueError(f"New simulator task requires a build-mode review: {source.stem}")
    if unavailable is not None:
        unavailable.extend(missing)
    return tasks


def baseline_tasks(root, unavailable=None):
    tasks = []
    for task in BASELINE_TASKS:
        reason = task_unavailability(root, task)
        if reason:
            if unavailable is not None:
                unavailable.append({"task": task, "reason": reason})
        else:
            tasks.append(task)
    if not tasks:
        raise ValueError("No configured baseline compatibility tasks are available in this ref")
    return tasks


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


def plan_tasks(root, results):
    unavailable = []
    simulator = simulator_tasks(root, unavailable=unavailable)
    baseline = baseline_tasks(root, unavailable=unavailable)
    plan = {"simulator_tasks": simulator, "baseline": {"task": baseline}, "unavailable": unavailable}
    (results / "ios-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    coverage_summary([*simulator, *baseline], unavailable)
    value = json.dumps(plan["baseline"], separators=(",", ":"))
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("baseline=" + value + "\n")
    print(json.dumps(plan, indent=2))


def validate_source_command(arguments, config, environment=None):
    """Reject an app build that would silently use the wrong local variant."""
    commands = {"build", "run", "test", "drive", "install", "attach", "assemble"}
    command = next((argument for argument in arguments if argument in commands), None)
    if command is None or "--config-only" in arguments:
        return None
    explicit = next((mode for mode in ("release", "profile", "debug")
                     if "--" + mode in arguments or "-dBuildMode=" + mode in arguments), None)
    default = "release" if command == "build" and "--simulator" not in arguments else "debug"
    mode = explicit or default
    if mode != "debug":
        raise ValueError(f"Source simulator task requested {mode}; only debug simulator artifacts were built")
    if command == "build" and ("ios" not in arguments or "--simulator" not in arguments):
        raise ValueError("Source simulator build must explicitly target ios --simulator")
    environment = os.environ if environment is None else environment
    sdk = environment.get("SDK_NAME", "")
    sdk += " ".join(argument for argument in arguments if argument.startswith("-dSdkRoot="))
    if "iphoneos" in sdk.lower():
        raise ValueError("Source simulator task requested a physical iOS device SDK")
    options = {}
    expected = {
        "--local-engine-src-path": str(Path(config["root"]) / "engine/src"),
        "--local-engine": config["ios"],
        "--local-engine-host": config["host"],
    }
    for index, argument in enumerate(arguments):
        name, separator, value = argument.partition("=")
        if name in expected:
            if not separator:
                value = arguments[index + 1] if index + 1 < len(arguments) else ""
            options[name] = value
    if any(options.get(name) != value for name, value in expected.items()):
        raise ValueError("Source simulator app command did not select the built local host and simulator Engines")
    return mode


@contextmanager
def source_launcher(root, results, ios, host):
    binary = root / "bin/flutter"
    original = root / "bin/flutter.ci-ios-original"
    configuration = root / "bin/flutter.ci-ios-guard.json"
    if original.exists():
        raise ValueError("An iOS source launcher is already installed")
    config = {"root": str(root), "results": str(results), "ios": ios, "host": host}
    configuration.write_text(json.dumps(config) + "\n")
    shutil.copy2(binary, original)
    old = binary.read_text()
    wrapper = ("#!/bin/sh\n# FLUTTER_CI_IOS_SOURCE_GUARD\nexec "
               + shlex.join([sys.executable, str(Path(__file__).resolve()),
                             "flutter-command", str(configuration)]) + ' "$@"\n')
    save_patch(root, binary, old, wrapper, results, "source-flutter-launcher")
    binary.chmod(0o755)
    try:
        yield
    finally:
        shutil.copy2(original, binary)
        original.unlink()
        configuration.unlink()


def dispatch_flutter(configuration, arguments):
    config = json.loads(configuration.read_text())
    mode = validate_source_command(arguments, config)
    with (Path(config["results"]) / "source-flutter-commands.jsonl").open("a") as output:
        output.write(json.dumps({"mode": mode, "arguments": arguments}) + "\n")
    binary = Path(config["root"]) / "bin/flutter.ci-ios-original"
    os.execv(str(binary), [str(binary), *arguments])


def run_devicelab(root, results, jobs, baseline_task=None):
    directory = root / "dev/devicelab"
    dart = root / "bin/cache/dart-sdk/bin/dart"
    run([dart, "pub", "get"], cwd=directory, log=results / "pub-get.log")
    flags = []
    unavailable = []
    if baseline_task:
        if baseline_task not in baseline_tasks(root, unavailable=unavailable):
            raise ValueError(f"Task is not configured in the selected ref: {baseline_task}")
        tasks = [baseline_task]
        # These tasks intentionally exercise all build modes/platforms in one
        # process. A single debug local engine would invalidate their coverage.
        origin = "selected-framework-and-tools-with-baseline-engine"
        run([root / "bin/flutter", "precache", "--ios", "--macos"],
            cwd=root, log=results / "precache.log")
    else:
        tasks = simulator_tasks(root, unavailable=unavailable)
        ios = build_engine(root, find_build(root, "ios"), results, jobs, app_artifacts=True)
        host = build_engine(root, find_build(root, "host"), results, jobs, app_artifacts=True)
        flags = ["--local-engine-src-path", str(root / "engine/src"),
                 "--local-engine", ios, "--local-engine-host", host]
        origin = "selected-ref-local-host-and-ios-engine"
    (results / "coverage.json").write_text(json.dumps(
        {"engine_origin": origin, "tasks": tasks, "local_engine_flags": flags,
         "unavailable": unavailable}, indent=2
    ) + "\n")
    coverage_summary(tasks, unavailable)
    statuses = {}
    guard = nullcontext() if baseline_task else source_launcher(root, results, ios, host)
    with guard:
        for task in tasks:
            statuses[task] = run([
                dart, "bin/test_runner.dart", "test", "--exit", "--task", task,
                *flags, "--results-file", str(results / (task + "-devicelab.json")),
            ], cwd=directory, log=results / (task + ".log"), check=False)
        if not baseline_task:
            # Exercise a module build with the actual source engine as well as
            # the full upstream multi-mode compatibility task above.
            with tempfile.TemporaryDirectory(prefix="flutter-ios-source-module-") as temporary:
                module = Path(temporary) / "source_module"
                statuses["source_module_create"] = run([
                    root / "bin/flutter", "create", "--template=module",
                    "--project-name=ci_source_module", str(module)
                ], cwd=root, log=results / "source-module-create.log", check=False)
                if not statuses["source_module_create"]:
                    statuses["source_module_build"] = run([
                        root / "bin/flutter", "build", "ios", "--simulator", "--debug",
                        "--no-codesign", *flags,
                    ], cwd=module, log=results / "source-module-build.log", check=False)
                    generated = module / ".ios/Flutter/Generated.xcconfig"
                    if generated.is_file():
                        shutil.copy2(generated, results / "source-module-Generated.xcconfig")
    (results / "results.json").write_text(json.dumps(statuses, indent=2) + "\n")
    return int(any(statuses.values()))


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
    if len(sys.argv) > 2 and sys.argv[1] == "flutter-command":
        dispatch_flutter(Path(sys.argv[2]), sys.argv[3:])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select-xcode", "native", "simulator", "baseline", "diagnostics", "plan"))
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--results", type=Path, default=Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")))
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--extension-safe", action="store_true")
    parser.add_argument("--task", choices=BASELINE_TASKS)
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
    if args.command in ("native", "simulator"):
        prepare_native_tools(results)
    if args.command == "native":
        return run_native(root, results, args.jobs, args.extension_safe)
    if args.command == "baseline" and not args.task:
        parser.error("baseline requires --task")
    return run_devicelab(root, results, args.jobs, args.task)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
