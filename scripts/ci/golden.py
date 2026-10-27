#!/usr/bin/env python3
"""Compare Android screenshots with a separately built official merge-base."""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

SUITES = (
    "android_engine_vulkan_tests", "android_engine_opengles_tests", "android_engine_hcpp_tests",
    "android_hardware_smoke_vulkan_tests", "android_hardware_smoke_opengles_tests",
    "android_hardware_smoke_vulkan_instrumented_tests",
    "android_hardware_smoke_opengles_instrumented_tests",
)


def golden_inventory(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*.png"))
            if not {"failures", "results"}.intersection(path.relative_to(directory).parts)}


def capture_actuals(source):
    anchor = ("  Future<bool> compare(Uint8List imageBytes, Uri golden) async {\n"
              "    final io.File goldenFile = _getTestFilePath(golden);")
    if source.count(anchor) != 1:
        raise ValueError("Android golden comparator changed; cannot capture comparison evidence")
    addition = '''
    final String? actualsDirectory = io.Platform.environment['FLUTTER_GOLDEN_ACTUALS_DIR'];
    if (actualsDirectory != null) {
      final io.File actual = io.File(path.join(actualsDirectory,
        '${goldenFile.path.hashCode.toUnsigned(32)}.actual.png'));
      await actual.parent.create(recursive: true);
      await actual.writeAsBytes(imageBytes);
      print('Golden actual: ${goldenFile.path} -> ${actual.path}');
    }'''
    return source.replace(anchor, anchor + addition, 1)


def source_matrix(root):
    import yaml
    from android import avd_configuration, dependency

    ci = yaml.load((root / ".ci.yaml").read_text(), Loader=yaml.BaseLoader)
    matrix = []
    for suite in SUITES:
        candidates = [target for target in ci["targets"]
                      if target.get("properties", {}).get("shard") == suite
                      and target["name"].startswith("Linux_android_emu")]
        if len(candidates) != 1:
            raise ValueError(f"Expected one official emulator target for {suite}")
        target = candidates[0]
        platform = target["name"].split()[0].lower()
        platform_props = ci["platform_properties"][platform]["properties"]
        avd_version = (dependency(target["properties"], "android_virtual_device")
                       or dependency(platform_props, "android_virtual_device"))
        avd = avd_configuration(avd_version)
        matrix.append({"suite": suite, **avd})
    return {"include": matrix}


def maven_repository(root, destination):
    """Same local Maven layout as Flutter Tools' _getLocalEngineRepo."""
    engine = root / "engine/src/out/android_debug_x64"
    for artifact in ("flutter_embedding_debug", "x86_64_debug"):
        pom = engine / f"{artifact}.pom"
        project = ET.fromstring(pom.read_text())
        version = next((child.text for child in project
                        if child.tag.rsplit("}", 1)[-1] == "version"), None)
        if not version or "/" in version:
            raise ValueError(f"Missing local Maven artifact version: {pom}")
        directory = destination / "io/flutter" / artifact
        (directory / version).mkdir(parents=True, exist_ok=True)
        for extension in ("pom", "jar"):
            source = engine / f"{artifact}.{extension}"
            if not source.is_file():
                raise ValueError(f"Missing local Engine artifact: {source}")
            target = directory / version / f"{artifact}-{version}.{extension}"
            target.unlink(missing_ok=True)
            target.symlink_to(source)
        metadata = engine / f"{artifact}.maven-metadata.xml"
        if not metadata.is_file():
            raise ValueError(f"Missing Maven metadata: {metadata}")
        link = directory / "maven-metadata.xml"
        link.unlink(missing_ok=True)
        link.symlink_to(metadata)
    return {
        "ORG_GRADLE_PROJECT_local-engine-repo": str(destination),
        "ORG_GRADLE_PROJECT_local-engine-build-mode": "debug",
        "ORG_GRADLE_PROJECT_local-engine-out": str(engine),
        "ORG_GRADLE_PROJECT_local-engine-host-out": str(root / "engine/src/out/host_debug_test"),
        "ORG_GRADLE_PROJECT_target-platform": "android-x64",
    }


class Golden:
    def __init__(self, root, outputs, suite):
        self.root, self.outputs, self.suite = root.resolve(), outputs.resolve(), suite
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.android = Path(__file__).with_name("android.py").resolve()
        self.hardware = "hardware_smoke" in suite
        project = "android_hardware_smoke_test" if self.hardware else "android_engine_test"
        self.project = self.root / "dev/integration_tests" / project
        self.source = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        self.baseline = os.environ["FLUTTER_BASELINE_SHA"]

    def run(self, command, *, cwd=None, env=None, log="run.log", check=True):
        command = list(map(str, command))
        print("$ " + shlex.join(command), flush=True)
        with (self.outputs / log).open("a") as output:
            output.write("$ " + shlex.join(command) + "\n")
            output.flush()
            process = subprocess.Popen(command, cwd=cwd or self.root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors="replace")
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                output.write(line)
                output.flush()
            result = process.wait()
        if result and check:
            raise subprocess.CalledProcessError(result, command)
        return result

    def android_command(self, *arguments):
        self.run([sys.executable, self.android, "--flutter-root", self.root, *arguments])

    def checkout(self, revision):
        # Only operate in the dedicated checkout created by setup-flutter-tests.
        workspace = os.environ.get("GITHUB_WORKSPACE")
        if not workspace or self.root != Path(workspace).resolve() / "flutter":
            raise ValueError("Golden checkout must be the dedicated workspace/flutter clone")
        self.android_command("restore-flutter-wrapper")
        self.run(["git", "checkout", "--force", "--detach", revision])
        self.run(["gclient", "sync", "-D", "--no-history", "-j2"])
        actual = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise ValueError("gclient sync moved the pinned golden source revision")
        self.run([self.root / "bin/flutter", "pub", "get"])
        self.run([self.root / "bin/dart", "pub", "get"], cwd=self.root / "dev/bots")
        self.android_command("build-app", "--mode", "debug")
        self.android_command("install-flutter-wrapper", "--modes", "debug")

    def baseline_images(self):
        env = {**os.environ, "UPDATE_GOLDENS": "1", "SHARD": self.suite}
        env.pop("SWARMING_TASK_ID", None)
        env.pop("LUCI_CONTEXT", None)
        if not self.hardware:
            self.run([self.root / "bin/cache/dart-sdk/bin/dart", "dev/bots/test.dart"],
                     env=env, log="baseline.log")
            return
        # The official hardware shard deletes PNGs in finally. Generate with
        # its driver directly so we can archive the baseline before cleanup.
        self.run([self.root / "bin/flutter", "create", "--platform=android",
                  "--no-overwrite", "."], cwd=self.project, log="baseline.log")
        manifest = self.project / "android/app/src/main/AndroidManifest.xml"
        original = manifest.read_text()
        backend = "opengles" if "opengles" in self.suite else "vulkan"
        patched, count = re.subn(
            r'(<meta-data android:name="io.flutter.embedding.android.ImpellerBackend" android:value=")[^"]+(" />)',
            lambda match: match[1] + backend + match[2], original,
        )
        if count != 1:
            raise ValueError("Hardware smoke backend metadata contract changed")
        manifest.write_text(patched)
        try:
            self.run([self.root / "bin/flutter", "drive", "--driver=test_driver/driver_test.dart",
                      "--target=integration_test/integration_test_wrapper.dart", "--no-dds",
                      "--no-enable-dart-profiling"], cwd=self.project, env=env, log="baseline.log")
        finally:
            manifest.write_text(original)

    def execute(self):
        captured = self.outputs / "baseline-images"
        statuses = {}
        metadata = {"suite": self.suite, "target_sha": self.source, "baseline_sha": self.baseline,
                    "engine_policy": "source Engine built separately for baseline and target",
                    "golden_policy": "same emulator; update enabled only on official baseline"}
        try:
            self.checkout(self.baseline)
            self.baseline_images()
            directory = self.project / "test_driver"
            inventory = golden_inventory(directory)
            if not inventory:
                raise ValueError("Baseline run produced no reference screenshots")
            for filename in inventory:
                destination = captured / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(directory / filename, destination)
            metadata["baseline_images"] = inventory
            self.android_command("restore-flutter-wrapper")
            self.checkout(self.source)
            directory = self.project / "test_driver"
            # Reintroduce only references built from the trusted baseline.
            for filename in inventory:
                destination = directory / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(captured / filename, destination)
            env = os.environ.copy()
            for key in ("UPDATE_GOLDENS", "SWARMING_TASK_ID", "LUCI_CONTEXT"):
                env.pop(key, None)
            env["SHARD"] = self.suite
            actuals = self.outputs / "actual-images"
            env["FLUTTER_GOLDEN_ACTUALS_DIR"] = str(actuals)
            comparator = self.root / "dev/tools/android_driver_extensions/lib/src/goldens.dart"
            original = comparator.read_text()
            patched = capture_actuals(original)
            (self.outputs / "capture-actuals.patch").write_text("".join(difflib.unified_diff(
                original.splitlines(keepends=True), patched.splitlines(keepends=True),
                fromfile=str(comparator.relative_to(self.root)),
                tofile=str(comparator.relative_to(self.root)))))
            comparator.write_text(patched)
            if "instrumented" in self.suite:
                # Direct Gradle calls must also select the source Engine.
                maven = Path(os.environ.get("RUNNER_TEMP", str(self.outputs))) / ("golden-maven-" + self.suite)
                env.update(maven_repository(self.root, maven))
            try:
                statuses["compare"] = self.run(
                    [self.root / "bin/cache/dart-sdk/bin/dart", "dev/bots/test.dart"],
                    env=env, log="target.log", check=False,
                )
            finally:
                comparator.write_text(original)
            metadata["actual_image_count"] = len(list(actuals.glob("*.png")))
            if not statuses["compare"] and not metadata["actual_image_count"]:
                raise ValueError("Target shard reported success without comparing screenshots")
            # Engine tests do not clean PNGs. Ensure comparison did not update
            # any baseline image. Hardware shard removes files by design.
            if not self.hardware:
                current = golden_inventory(directory)
                if any(current.get(name) != digest for name, digest in inventory.items()):
                    raise ValueError("Target run modified the trusted golden references")
            return statuses["compare"]
        finally:
            metadata["statuses"] = statuses
            (self.outputs / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
            self.android_command("restore-flutter-wrapper")
            for pattern in ("**/failures/**/*.png", "**/results/**/*.png", "**/reports/androidTests/**/*"):
                for path in self.project.glob(pattern):
                    if path.is_file() and path.stat().st_size < 20_000_000:
                        destination = self.outputs / "artifacts" / path.relative_to(self.project)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--suite", choices=SUITES)
    selection.add_argument("--plan", action="store_true")
    parser.add_argument("--flutter-root", type=Path, default=Path(os.environ.get("FLUTTER_ROOT", "flutter")))
    parser.add_argument("--outputs", type=Path, default=Path(os.environ.get("FLUTTER_TEST_OUTPUTS_DIR", "test-results")) / "golden")
    args = parser.parse_args()
    if args.plan:
        print("matrix=" + json.dumps(source_matrix(args.flutter_root), separators=(",", ":")))
        return 0
    return Golden(args.flutter_root, args.outputs, args.suite).execute()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
