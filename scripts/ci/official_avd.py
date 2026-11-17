#!/usr/bin/env python3
"""Install and run the Chromium AVD pinned by the selected Flutter CI target."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


def adb_executable():
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if directory := os.environ.get(name):
            candidate = Path(directory) / "platform-tools/adb"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    if adb := shutil.which("adb"):
        return adb
    raise ValueError("Android platform-tools adb is unavailable")


def run(command, log, *, timeout=1800):
    command = list(map(str, command))
    print("$ " + shlex.join(command), flush=True)
    with log.open("w") as output:
        try:
            subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                           check=True, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(log.read_text(errors="replace")[-6000:], flush=True)
            raise


def check_acceleration(root, outputs):
    emulators = list((root / "src/.android_emulator").glob("**/emulator/emulator"))
    if len(emulators) != 1:
        raise ValueError("Expected one emulator binary installed by the official AVD launcher")
    run([emulators[0], "-accel-check"], outputs / "acceleration.log", timeout=30)


def install(config, version):
    from android import avd_configuration
    avd_configuration(config)
    if not re.fullmatch(r"build_id:\d+|git_revision:[0-9a-f]{40}|[A-Za-z0-9_-]{40,64}", version):
        raise ValueError(f"Unsupported official AVD CIPD pin: {version!r}")
    root = Path(os.environ["RUNNER_TEMP"]) / "official-android-avd"
    outputs = Path(os.environ["FLUTTER_TEST_OUTPUTS_DIR"]) / "avd"
    outputs.mkdir(parents=True, exist_ok=True)
    adb = adb_executable()
    with Path(os.environ["GITHUB_PATH"]).open("a") as output:
        output.write(str(Path(adb).parent) + "\n")
    ensure = outputs / "launcher.ensure"
    ensure.write_text(f"chromium/tools/android/avd/linux-amd64 {version}\n")
    run(["cipd", "ensure", "-root", root, "-ensure-file", ensure], outputs / "cipd.log")
    proto = root / "src/tools/android/avd/proto" / config
    if not proto.is_file():
        raise ValueError(f"Pinned official AVD package does not contain {config}")
    shutil.copy2(proto, outputs / config)
    run(["vpython3", root / "src/tools/android/avd/avd.py", "install", "--avd-config", proto],
        outputs / "install.log")
    check_acceleration(root, outputs)
    (outputs / "revision.json").write_text(json.dumps({
        "avd_config": config, "avd_cipd_version": version,
        "policy": "Chromium launcher, emulator, system image and preconfigured AVD from the target CI pin",
    }, indent=2) + "\n")
    with Path(os.environ["GITHUB_ENV"]).open("a") as output:
        output.write(f"FLUTTER_CI_AVD_ROOT={root}\nFLUTTER_CI_AVD_CONFIG={config}\n")


@contextmanager
def session(outputs, *, gpu_mode=None):
    """Start the official AVD and stop only that instance, preserving test failures."""
    root = Path(os.environ["FLUTTER_CI_AVD_ROOT"])
    config = os.environ["FLUTTER_CI_AVD_CONFIG"]
    proto = root / "src/tools/android/avd/proto" / config
    outputs.mkdir(parents=True, exist_ok=True)
    adb = adb_executable()
    check_acceleration(root, outputs)
    log = outputs / "emulator.log"
    # Debug output inherits this file, so the emulator keeps logging after the
    # official launcher exits. Do not capture a pipe held open by the emulator.
    command = ["vpython3", root / "src/tools/android/avd/avd.py", "start", "--avd-config", proto,
               "--adb-path", adb, "--enable-network", "--debug-tags", "init,gles"]
    if gpu_mode:
        command.extend(["--gpu-mode", gpu_mode])
    run(command, log, timeout=600)
    match = re.search(r"\|(emulator-\d+) started \(pid: (\d+)\)", log.read_text(errors="replace"))
    if not match:
        raise ValueError(f"Official AVD launcher did not report its instance; see {log}")
    serial, pid = match.groups()
    (outputs / "instance.json").write_text(json.dumps({
        "serial": serial, "pid": int(pid), "gpu_mode_override": gpu_mode,
    }) + "\n")
    previous = os.environ.get("ANDROID_SERIAL")
    previous_path = os.environ.get("PATH", "")
    os.environ["ANDROID_SERIAL"] = serial
    os.environ["PATH"] = str(Path(adb).parent) + os.pathsep + previous_path
    try:
        print(f"Official Android AVD ready: {serial}", flush=True)
        yield serial
    finally:
        try:
            run([adb, "-s", serial, "emu", "kill"], outputs / "stop.log", timeout=15)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"::warning::Could not stop {serial}: {error}", flush=True)
        if previous is None:
            os.environ.pop("ANDROID_SERIAL", None)
        else:
            os.environ["ANDROID_SERIAL"] = previous
        os.environ["PATH"] = previous_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("install")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--version", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--gpu-mode", choices=("swangle_indirect", "swiftshader_indirect"))
    execute.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == "install":
        install(args.config, args.version)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("An Android test command is required")
    with session(Path(os.environ["FLUTTER_TEST_OUTPUTS_DIR"]) / "avd", gpu_mode=args.gpu_mode):
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(error.returncode or 1)
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
