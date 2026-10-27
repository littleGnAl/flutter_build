#!/usr/bin/env python3
"""Run Android suites from the selected Flutter checkout, without LUCI services."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


BUILD_TASKS = (
    "build_aar_module_test", "engine_dependency_proxy_test",
    "gradle_desugar_classes_test", "gradle_java8_compile_test",
    "gradle_plugin_bundle_test", "gradle_plugin_fat_apk_test",
    "gradle_plugin_light_apk_test", "module_custom_host_app_name_test",
    "module_host_with_custom_build_test", "build_android_host_app_with_module_aar",
    "android_java17_build_android_host_app_with_module_aar",
    "build_android_host_app_with_module_source", "plugin_dependencies_test",
    "plugin_test_android_variants", "plugin_test_android_standard",
    "android_release_builds_exclude_dev_dependencies_test",
    "android_java11_dependency_smoke_tests", "android_java17_dependency_smoke_tests",
)

# All non-golden DeviceLab tasks assigned to a Linux_android_emu platform in
# the researched upstream configuration. Unknown additions fail planning.
EMULATOR_MODES = {
    "android_views": ["debug"],
    "android_display_cutout": ["debug"],
    "android_defines_test": ["debug"],
    "external_textures_integration_test": ["debug"],
    "native_assets_android": ["debug", "profile", "release"],
    "android_engine_flags_debug_test": ["debug"],
    "android_engine_flags_release_test": ["release"],
    "android_intent_security_test": ["release"],
    "android_intent_parsing_test": ["debug", "profile", "release"],
}

# Functional DeviceLab tasks whose official device assignment is physical but
# whose assertions can also run on an emulator. Performance tasks are excluded.
ADAPTED_TASKS = (
    "channels_integration_test", "hybrid_android_views_integration_test",
    "integration_ui_driver", "integration_ui_keyboard_resize", "integration_ui_textfield",
    "platform_channel_sample_test", "platform_interaction_test", "routing_test",
    "service_extensions_test", "flavors_test", "integration_test_test",
    "run_debug_test_android", "run_release_test",
)


def dependencies(value):
    return json.loads(value) if isinstance(value, str) else (value or [])


def dependency(properties, name):
    return next((d["version"] for d in dependencies(properties.get("dependencies"))
                 if d["dependency"] == name), None)


def avd_configuration(version):
    match = re.fullmatch(r"android_(\d+)_(google_apis(?:_ps16k)?)_x64\.textpb", version or "")
    if not match:
        raise ValueError(f"Unsupported upstream AVD configuration: {version!r}")
    # Android 17's stable SDK/platform and system image use 37.0, although the
    # upstream AVD proto and ro.build.version.sdk call it API 37.
    sdk_version = "37.0" if match[1] == "37" else match[1]
    return {"api": sdk_version, "target": match[2], "arch": "x86_64"}


def local_gn(arguments):
    """Keep source flags except remote execution; never use a Google RBE server."""
    result = [arg for arg in arguments if not re.match(r"--(?:no-)?(?:rbe|goma)(?:=|$)", arg)]
    if "--no-lto" not in result:
        result.append("--no-lto")
    return result


def source_plan(root):
    import yaml

    ci = yaml.safe_load((root / ".ci.yaml").read_text())
    platforms = ci["platform_properties"]
    targets = ci["targets"]
    extension = (root / "packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt").read_text()
    ndk = re.search(r'val ndkVersion: String = "([0-9.]+)"', extension)
    if ndk is None:
        raise ValueError("Cannot determine the target ref's Android NDK version")
    supported = {t["properties"]["task_name"]: t for t in targets
                 if t.get("properties", {}).get("task_name")}
    builds = []
    for task in BUILD_TASKS:
        target = next((t for t in targets if t["name"].startswith("Linux ")
                       and t.get("properties", {}).get("task_name") == task), None)
        if target is None:
            raise ValueError(f"Required host build task is absent in target ref: {task}")
        version = dependency(target["properties"], "open_jdk")
        if not version or not version.startswith("version:"):
            raise ValueError(f"No supported JDK dependency for {target['name']}")
        builds.append({"task": task, "jdk": version.removeprefix("version:")})
    emulator = []
    for target in targets:
        platform = target["name"].split(" ", 1)[0].lower()
        if not platform.startswith("linux_android_emu"):
            continue
        task = target.get("properties", {}).get("task_name")
        if not task:  # The golden shards have a separate workflow.
            continue
        if task not in EMULATOR_MODES:
            raise ValueError(f"New Android emulator task needs an explicit mode/coverage decision: {task}")
        avd_version = (dependency(target.get("properties", {}), "android_virtual_device")
                       or dependency(platforms[platform]["properties"], "android_virtual_device"))
        avd = avd_configuration(avd_version)
        emulator.append({"task": task, "id": f"{task}-{platform}",
                         "modes": ",".join(EMULATOR_MODES[task]), "ndk": ndk[1], **avd})
    default_avd = avd_configuration(dependency(platforms["linux_android_emu"]["properties"],
                                               "android_virtual_device"))
    for task in ADAPTED_TASKS:
        if task not in supported or not (root / f"dev/devicelab/bin/tasks/{task}.dart").is_file():
            raise ValueError(f"Required functional task is absent in target ref: {task}")
        mode = ("release" if task == "run_release_test" else
                "debug,release" if task == "flavors_test" else "debug")
        emulator.append({"task": task, "id": f"{task}-adapted", "modes": mode,
                         "ndk": ndk[1], **default_avd})
    return {"build": {"include": builds}, "emulator": {"include": emulator}}


class Android:
    def __init__(self, root, dry_run=False):
        self.root = root.resolve()
        self.src = self.root / "engine/src"
        self.outputs = Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")).resolve()
        self.dry_run = dry_run

    def run(self, command, cwd=None, env=None):
        command = [str(c) for c in command]
        print(f"+ ({cwd or self.root}) {shlex.join(command)}", flush=True)
        if not self.dry_run:
            self.outputs.mkdir(parents=True, exist_ok=True)
            with (self.outputs / "android-commands.log").open("a") as log:
                log.write(f"{cwd or self.root}: {shlex.join(command)}\n")
            with (self.outputs / "android-run.log").open("a") as log:
                with subprocess.Popen(command, cwd=cwd or self.root, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, errors="replace") as process:
                    assert process.stdout is not None
                    for line in process.stdout:
                        print(line, end="", flush=True)
                        log.write(line)
                        log.flush()
                    code = process.wait()
                if code:
                    raise subprocess.CalledProcessError(code, command)

    def config(self, filename, name):
        config = json.loads((self.src / "flutter/ci/builders" / filename).read_text())
        return next(b for b in config["builds"] if b["name"] == name)

    def build(self, filename, name, targets=None):
        config = self.config(filename, name)
        gn = local_gn(config["gn"])
        self.run(["./flutter/tools/gn", *gn], self.src)
        self.run(["ninja", "-j2", "-C", f"out/{name}",
                  *(targets if targets is not None else config["ninja"]["targets"])], self.src)

    def build_app(self, mode):
        filename = "linux_android_debug_engine.json" if mode == "debug" else "linux_android_aot_engine.json"
        self.build(filename, f"ci/android_{mode}_x64")
        # Use the official mode's GN setup; build local tooling, not the host UT suite.
        self.build("linux_host_engine_test.json", f"ci/host_{mode}_test",
                   ["flutter/build/dart:dart_sdk", "flutter/flutter_frontend_server:frontend_server",
                    "flutter/shell/testing", "flutter/tools/font_subset",
                    "flutter/impeller/compiler:impellerc",
                    "flutter/impeller/tessellator:tessellator_shared", "sky_engine"])
        if not self.dry_run:
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
            for name in (f"ci/android_{mode}_x64", f"ci/host_{mode}_test"):
                (self.src / "out" / name / "flutter-ci-build.json").write_text(
                    json.dumps({"sha": sha, "variant": name}))
            self.engine_aliases([mode])

    def native_avd(self):
        config = self.config("linux_android_emulator.json", "ci/android_emulator_debug_x64")
        test = next(t for t in config["tests"] if "android_virtual_device" in t.get("contexts", []))
        version = next(d["version"] for d in test["test_dependencies"]
                       if d["dependency"] == "android_virtual_device")
        return avd_configuration(version)

    def java(self, build=True):
        name = "ci/android_debug_unopt"
        if build:
            self.build("linux_unopt.json", name, ["flutter/shell/platform/android:android_jar"])
        gradle = self.root / "engine/src/flutter/shell/platform/android/test_runner/build.gradle"
        original = gradle.read_text()
        patched = original.replace('jvmArgs "-Xmx8g"', 'jvmArgs "-Xmx2g"').replace(
            'maxHeapSize "8g"', 'maxHeapSize "2g"').replace(
            "maxParallelForks availableProcessors", "maxParallelForks 1")
        if patched == original:
            raise ValueError("Upstream Java memory settings changed; update explicit runner limits")
        if not self.dry_run:
            self.outputs.mkdir(parents=True, exist_ok=True)
            (self.outputs / "java-build.gradle.original").write_text(original)
            (self.outputs / "java-build.gradle.runner").write_text(patched)
            gradle.write_text(patched)
        try:
            self.run([sys.executable, "flutter/testing/run_tests.py", "--type", "java",
                      "--android-variant", name], self.src)
        finally:
            if not self.dry_run:
                gradle.write_text(original)

    def task(self, name, modes):
        if name not in BUILD_TASKS and name not in EMULATOR_MODES and name not in ADAPTED_TASKS:
            raise ValueError(f"Unreviewed DeviceLab task: {name}")
        dart = self.root / "bin/cache/dart-sdk/bin/dart"
        lab = self.root / "dev/devicelab"
        self.run([dart, "pub", "get"], lab)
        if modes:
            self.install_wrapper(modes)
        try:
            # run.dart carries actual TaskResult failures through its exit status.
            command = [dart, "bin/run.dart", "--task", name,
                       "--results-file", self.outputs / f"{name}.json"]
            if modes:
                command.append("--use-emulator")
            env = None
            if name == "flavors_test" and not self.dry_run:
                # This task also starts Gradle directly and then drives that
                # prebuilt APK. The Flutter entrypoint wrapper alone is not
                # enough to select a source Engine for that part of the test.
                from golden import maven_repository
                env = {**os.environ, **maven_repository(self.root, self.outputs / "local-engine-maven")}
            self.run(command, lab, env)
        finally:
            if modes:
                self.collect_adb()
                self.restore_wrapper()

    def collect_adb(self):
        if not self.dry_run and shutil.which("adb"):
            self.outputs.mkdir(parents=True, exist_ok=True)
            with (self.outputs / "logcat.txt").open("w") as output:
                subprocess.run(["adb", "logcat", "-d"], stdout=output,
                               stderr=subprocess.STDOUT, check=False)

    def install_wrapper(self, modes):
        binary = self.root / "bin/flutter"
        original = self.root / "bin/flutter.ci-original"
        if not self.dry_run:
            self.engine_aliases(modes)
            self.outputs.mkdir(parents=True, exist_ok=True)
            if not original.exists():
                shutil.copy2(binary, original)
            elif "FLUTTER_CI_ANDROID_WRAPPER" not in binary.read_text():
                # A new checkout may replace the wrapper between baseline and target.
                shutil.copy2(binary, original)
            config = {"root": str(self.root), "modes": modes, "outputs": str(self.outputs)}
            (self.root / "bin/flutter.ci-wrapper.json").write_text(json.dumps(config))
            script = Path(__file__).resolve()
            binary.write_text("#!/bin/sh\n# FLUTTER_CI_ANDROID_WRAPPER\nexec " +
                              shlex.quote(sys.executable) + " " + shlex.quote(str(script)) +
                              " --flutter-root " + shlex.quote(str(self.root)) +
                              " flutter-command -- \"$@\"\n")
            binary.chmod(0o755)
            (self.outputs / "flutter-wrapper.sh").write_text(binary.read_text())

    def engine_aliases(self, modes):
        # Flutter's Android Gradle plugin derives engine/src from the parent of
        # the output folder. Pass flat aliases so the extra CI directory cannot
        # turn that calculation into engine/src/out/out/<variant>.
        for mode in modes:
            for name in (f"android_{mode}_x64", f"host_{mode}_test"):
                target = self.src / "out/ci" / name
                alias = self.src / "out" / name
                if not target.is_dir():
                    raise ValueError(f"Source engine output is missing: {target}")
                if alias.is_symlink() and alias.resolve() == target.resolve():
                    continue
                if alias.exists() or alias.is_symlink():
                    raise ValueError(f"Refusing to replace existing engine output: {alias}")
                alias.symlink_to(Path("ci") / name, target_is_directory=True)

    def validate_app(self, modes):
        """Fail before launching a test if a transferred artifact is incomplete."""
        self.engine_aliases(modes)
        for mode in modes:
            target = self.src / f"out/android_{mode}_x64"
            host = self.src / f"out/host_{mode}_test"
            required = [target / "flutter.jar", target / "flutter_patched_sdk/platform_strong.dill",
                        host / "dart-sdk/bin/dart", host / "dart-sdk/bin/dartaotruntime",
                        host / "dart-sdk/bin/snapshots/frontend_server_aot.dart.snapshot",
                        host / "gen/dart-pkg/sky_engine/lib/ui/ui.dart",
                        host / "font-subset", host / "impellerc", host / "libtessellator.so"]
            for artifact in (f"flutter_embedding_{mode}", f"x86_64_{mode}"):
                required += [target / f"{artifact}.{ext}" for ext in ("jar", "pom", "maven-metadata.xml")]
            missing = [str(p) for p in required if not p.is_file()]
            if mode != "debug" and not any((target / p / "gen_snapshot").is_file()
                                            for p in (".", "clang_x64")):
                missing.append(str(target / "clang_x64/gen_snapshot"))
            if missing:
                raise ValueError("Incomplete source runtime artifact:\n" + "\n".join(missing))

    def restore_wrapper(self):
        binary = self.root / "bin/flutter"
        original = self.root / "bin/flutter.ci-original"
        if not self.dry_run and original.exists():
            shutil.copy2(original, binary)
            original.unlink()
            (self.root / "bin/flutter.ci-wrapper.json").unlink(missing_ok=True)


def flutter_arguments(args, root, modes):
    """Only select artifacts; preserve all test/app arguments and assertions."""
    command = next((a for a in args if not a.startswith("-")), "")
    if (command not in {"build", "run", "test", "drive", "install", "attach"}
            or "--config-only" in args or "--uninstall-only" in args):
        return args, None
    explicit = next((m for m in ("release", "profile", "debug") if f"--{m}" in args), None)
    mode = explicit or ("release" if command in {"build", "install"} else "debug")
    if mode not in modes:
        raise ValueError(f"Task requested {mode} but only {modes} local artifacts were provided")
    if any(a.startswith("--local-engine") for a in args):
        raise ValueError("Do not mix DeviceLab fixed local-engine flags with mode-selecting wrapper")
    target = f"android_{mode}_x64"
    host = f"host_{mode}_test"
    for variant in (target, host):
        directory = root / "engine/src/out" / variant
        if not directory.is_dir():
            raise ValueError(f"Required source-engine output missing: {variant}")
        manifest = json.loads((directory / "flutter-ci-build.json").read_text())
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        if manifest["sha"] != sha:
            raise ValueError(f"Engine output {variant} belongs to {manifest['sha']}, checkout is {sha}")
    selected = [f"--local-engine-src-path={root / 'engine/src'}",
                f"--local-engine={target}", f"--local-engine-host={host}"]
    # Global artifact flags precede the command, including when run by Gradle.
    return [*selected, *args], mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    sub.add_parser("native-avd")
    sub.add_parser("java")
    sub.add_parser("build-native")
    sub.add_parser("native")
    build_app = sub.add_parser("build-app")
    build_app.add_argument("--mode", choices=["debug", "profile", "release"], required=True)
    wrapper = sub.add_parser("install-flutter-wrapper")
    wrapper.add_argument("--modes", required=True)
    sub.add_parser("restore-flutter-wrapper")
    validate = sub.add_parser("validate-app")
    validate.add_argument("--modes", required=True)
    task = sub.add_parser("task")
    task.add_argument("--name", required=True)
    task.add_argument("--modes", default="")
    flutter = sub.add_parser("flutter-command")
    flutter.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    runner = Android(args.flutter_root, args.dry_run)
    if args.command == "plan":
        for key, value in source_plan(runner.root).items():
            line = f"{key}={json.dumps(value, separators=(',', ':'))}\n"
            if "GITHUB_OUTPUT" in os.environ:
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(line)
            else:
                print(line, end="")
    elif args.command == "native-avd":
        for key, value in runner.native_avd().items():
            print(f"{key}={value}")
            if "GITHUB_OUTPUT" in os.environ:
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"{key}={value}\n")
    elif args.command == "java":
        runner.java()
    elif args.command == "build-native":
        runner.build("linux_android_emulator.json", "ci/android_emulator_debug_x64")
    elif args.command == "native":
        try:
            runner.run([sys.executable, "flutter/testing/run_tests.py", "--type", "android",
                        "--android-variant", "ci/android_emulator_debug_x64"], runner.src)
        finally:
            runner.collect_adb()
    elif args.command == "build-app":
        runner.build_app(args.mode)
    elif args.command == "install-flutter-wrapper":
        runner.install_wrapper(args.modes.split(","))
    elif args.command == "restore-flutter-wrapper":
        runner.restore_wrapper()
    elif args.command == "validate-app":
        runner.validate_app(args.modes.split(","))
    elif args.command == "task":
        runner.task(args.name, args.modes.split(",") if args.modes else [])
    elif args.command == "flutter-command":
        config = json.loads((runner.root / "bin/flutter.ci-wrapper.json").read_text())
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        selected, mode = flutter_arguments(arguments, runner.root, config["modes"])
        with (Path(config["outputs"]) / "flutter-mode-selection.jsonl").open("a") as log:
            log.write(json.dumps({"mode": mode, "arguments": selected}) + "\n")
        original = runner.root / "bin/flutter.ci-original"
        os.execv(str(original), [str(original), *selected])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, StopIteration, KeyError, subprocess.CalledProcessError) as error:
        print(f"Android CI failed: {error}", file=sys.stderr)
        sys.exit(error.returncode if isinstance(error, subprocess.CalledProcessError) else 1)
