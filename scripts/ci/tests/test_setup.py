import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ACTION = Path(__file__).resolve().parents[3] / ".github/actions/setup-flutter-tests/action.yml"


def action_step(name, required=True):
    for block in ACTION.read_text().split("\n    - "):
        if block.splitlines()[0] == "name: " + name:
            return textwrap.dedent(block.split("      run: |\n", 1)[1])
    if required:
        raise ValueError("Action step not found: " + name)
    return ""


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


class SharedSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.path_file = self.root / "github-path"
        self.path_file.touch()
        self.environment = {
            **os.environ,
            "RUNNER_TEMP": str(self.root),
            "GITHUB_PATH": str(self.path_file),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        # Reproduce depot_tools' basename exclusion, but fail safely on self-selection.
        # https://chromium.googlesource.com/chromium/tools/depot_tools/+/refs/heads/main/ninja.py
        shim = self.root / "ninja-shim"
        executable(shim, "#!" + sys.executable + "\n" + textwrap.dedent('''\
            import os
            import sys
            for directory in os.environ['PATH'].split(os.pathsep):
                if os.path.basename(directory.rstrip(os.sep)) == 'depot_tools':
                    continue
                candidate = os.path.join(directory, 'ninja')
                if os.path.isfile(candidate):
                    if os.path.samefile(candidate, __file__):
                        sys.exit('depot_tools Ninja selected itself recursively')
                    os.execv(candidate, [candidate, *sys.argv[1:]])
            sys.exit('No real Ninja available')
        '''))
        executable(self.bin / "git", "#!/bin/sh\n"
                   'test "$1" = clone || exit 87\n'
                   'for destination do :; done\n'
                   'mkdir -p "$destination"\n'
                   'cp "$RUNNER_TEMP/ninja-shim" "$destination/ninja"\n')
        executable(self.bin / "brew", "#!/bin/sh\n" + textwrap.dedent('''\
            case "$1" in
              list) test -x "$RUNNER_TEMP/brew/bin/ninja" ;;
              install)
                mkdir -p "$RUNNER_TEMP/brew/bin"
                printf '#!/bin/sh\\necho real-ninja-1.13\\n' > "$RUNNER_TEMP/brew/bin/ninja"
                chmod +x "$RUNNER_TEMP/brew/bin/ninja" ;;
              --prefix) echo "$RUNNER_TEMP/brew" ;;
              *) exit 88 ;;
            esac
        '''))

    def shell(self, command):
        return subprocess.run(["bash", "-e", "-c", command], env=self.environment,
                              cwd=self.root, text=True, capture_output=True, timeout=10)

    def step(self, name):
        result = self.shell(action_step(name))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # GitHub prepends paths written by each completed step to following steps.
        entries = self.path_file.read_text().splitlines()
        if entries:
            self.environment["PATH"] = os.pathsep.join(reversed(entries)) + os.pathsep + self.environment["PATH"]
            self.path_file.write_text("")

    def test_macos_installs_and_selects_real_ninja_after_depot_tools(self):
        self.step("Install public Depot Tools")
        self.step("Ensure macOS Ninja is available")
        result = self.shell("ninja --version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "real-ninja-1.13")

    def test_macos_keeps_an_installed_real_ninja_ahead_of_depot_tools(self):
        executable(self.root / "brew/bin/ninja", "#!/bin/sh\necho installed-ninja-1.13\n")
        self.step("Install public Depot Tools")
        self.step("Ensure macOS Ninja is available")
        result = self.shell("ninja --version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "installed-ninja-1.13")

    def test_linux_depot_tools_can_fall_back_to_the_installed_ninja(self):
        executable(self.bin / "ninja", "#!/bin/sh\necho system-ninja-1.13\n")
        self.step("Install public Depot Tools")
        result = self.shell("ninja --version")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "system-ninja-1.13")

    def test_temporary_git_repositories_can_commit_with_the_ci_identity(self):
        git = shutil.which("git")
        for name in list(self.environment):
            if name.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_")):
                self.environment.pop(name)
        global_config = self.root / "global.gitconfig"
        environment_file = self.root / "github-env"
        environment_file.touch()
        self.environment.update({
            "GITHUB_ENV": str(environment_file),
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        identity_step = action_step("Set the CI Git identity for temporary test repositories", required=False)
        result = self.shell(identity_step)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.environment.update(line.split("=", 1) for line in environment_file.read_text().splitlines())
        repository = self.root / "temporary-repository"
        for command in (
            [git, "init", "--quiet", str(repository)],
            [git, "-C", str(repository), "commit", "--quiet", "--allow-empty", "-m", "init commit"],
        ):
            result = self.shell(shlex.join(command))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.shell(shlex.join([git, "-C", str(repository), "log", "-1", "--format=%an|%ae|%cn|%ce"]))
        self.assertEqual(result.stdout.strip(),
                         "Flutter CI|flutter-ci@users.noreply.github.com|Flutter CI|flutter-ci@users.noreply.github.com")
        self.assertFalse(global_config.exists())


if __name__ == "__main__":
    unittest.main()
