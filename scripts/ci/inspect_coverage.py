#!/usr/bin/env python3
"""Inspect Android/iOS coverage without building or executing Flutter."""
import argparse
import json
from pathlib import Path
import subprocess

import android
import golden
import ios
import sdk
from official_ci import Policy


def inspect(root):
    policy = Policy.load(root)
    configuration = sdk.read_configuration(root / ".ci.yaml")
    tools_unavailable = []
    android_tools = sdk.ci_matrix(configuration, root, policy=policy, unavailable=tools_unavailable)["include"]
    android_jobs = android.source_plan(root)
    ios_unavailable = []
    ios_source_tasks = ios.task_entries(root, ios.SIMULATOR_TASKS, simulator=True,
                                       unavailable=ios_unavailable, policy=policy)
    ios_compatibility_tasks = ios.task_entries(root, ios.BASELINE_TASKS,
                                              unavailable=ios_unavailable, policy=policy)
    golden_jobs = golden.source_matrix(root)["include"]
    return {
        "source_sha": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        "android_tools": android_tools,
        "android_build": android_jobs["build"]["include"],
        "android_emulator": android_jobs["emulator"]["include"],
        "official_ci_policy": policy.document(),
        "android_embedding": android_jobs["embedding"]["include"],
        "android_native_avd": (android.Android(root).native_avd()
                               if any(item["suite"] == "native" for item in android_jobs["embedding"]["include"]) else None),
        "ios_native": ios.native_matrix(root, policy)["include"],
        "ios_simulator_tasks": ios_source_tasks,
        "ios_baseline_compatibility_tasks": ios_compatibility_tasks,
        "android_golden": golden_jobs,
        "unavailable_in_ref": {
            "android_tools": sdk.plan_diagnostics({"include": android_tools}, tools_unavailable),
            "android": android_jobs["coverage"]["unavailable"],
            "ios": ios_unavailable,
            "golden": sorted(set(golden.SUITES) - {job["suite"] for job in golden_jobs}),
        },
        "hosted_exclusions": {
            "physical_devices": "device performance, power, OEM GPU/hardware and device-only assertions",
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
