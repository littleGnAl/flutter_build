#!/usr/bin/env python3
"""Inspect complete supported coverage without building or executing Flutter."""
import argparse
import json
from pathlib import Path
import subprocess

import android
import golden
import ios
import sdk


def inspect(root):
    configuration = sdk.read_configuration(root / ".ci.yaml")
    sdk_jobs = sdk.ci_matrix(configuration, (root / "dev/bots/suite_runners/run_framework_tests.dart").read_text())["include"]
    android_jobs = android.source_plan(root)
    native = []
    for system, machine in (("Linux", "x86_64"), ("Darwin", "arm64")):
        for mode in ("debug", "profile", "release"):
            build, _ = sdk.host_configuration(root, mode, system, machine)
            native.append({"system": system, "mode": mode, "builder": build["name"]})
    ios_unavailable = []
    ios_source_tasks = ios.simulator_tasks(root, unavailable=ios_unavailable)
    ios_compatibility_tasks = ios.baseline_tasks(root, unavailable=ios_unavailable)
    golden_jobs = golden.source_matrix(root)["include"]
    return {
        "source_sha": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        "sdk": sdk_jobs,
        "host_engine": native,
        "android_build": android_jobs["build"]["include"],
        "android_emulator": android_jobs["emulator"]["include"],
        "android_native_avd": android.Android(root).native_avd(),
        "ios_native": [ios.find_build(root, "ios", extension)["name"] for extension in (False, True)],
        "ios_source_simulator_tasks": ios_source_tasks,
        "ios_baseline_compatibility_tasks": ios_compatibility_tasks,
        "android_golden": golden_jobs,
        "unavailable_in_ref": {
            "sdk": sdk.plan_diagnostics({"include": sdk_jobs}),
            "android": android_jobs["coverage"]["unavailable"],
            "ios": ios_unavailable,
            "golden": sorted(set(golden.SUITES) - {job["suite"] for job in golden_jobs}),
        },
        "hosted_exclusions": {
            "physical_devices": "device performance, power, OEM GPU/hardware and device-only assertions",
            "mac_host_engine": sorted(sdk.GPU_EXECUTABLES),
            "mac_host_dart_backend": "metal; other software backends and Dart cases remain enabled",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flutter-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = inspect(args.flutter_root.resolve())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: len(value) for key, value in report.items() if isinstance(value, list)}, indent=2))


if __name__ == "__main__":
    main()
