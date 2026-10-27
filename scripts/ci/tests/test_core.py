import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))


def module(name):
    spec = importlib.util.spec_from_file_location(name, CI / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


resolve_ref = module("resolve_ref")
prepare = module("prepare_checkout")
golden = module("golden")


class RefValidationTest(unittest.TestCase):
    def test_rejects_repositories_that_escape_github_owner_repository(self):
        for repository in ("https://evil.test/a/b", "owner/repo/extra", "../repo", "owner name/repo"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                resolve_ref.validate_repository(repository)

    def test_rejects_empty_option_or_multiline_refs_before_running_git(self):
        for ref in ("", "--upload-pack=command", "main\nsha=spoofed", "two words"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                resolve_ref.validate_ref(ref)

    def test_accepts_branch_tag_and_commit(self):
        for ref in ("feature/ios-scenes", "3.38.0", "a" * 40, "refs/tags/3.38.0"):
            self.assertEqual(resolve_ref.validate_ref(ref), ref)


class BootstrapManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        (self.root / "engine").mkdir()
        (self.root / "engine/source.cc").write_text("engine one")
        (self.root / "DEPS").write_text("dependencies")
        internal = self.root / "bin/internal"
        internal.mkdir(parents=True)
        (internal / "update_engine_version.sh").write_text('''#!/usr/bin/env bash
set -e
root="$(dirname "$(dirname "$(dirname "$0")")")"
mkdir -p "$root/bin/cache"
if [ -n "$FLUTTER_PREBUILT_ENGINE_VERSION" ]; then
  version="$FLUTTER_PREBUILT_ENGINE_VERSION"
else
  version="$(bash "$root/bin/internal/content_aware_hash.sh")"
fi
printf '%s\\n' "$version" > "$root/bin/cache/engine.stamp"
''')
        (internal / "content_aware_hash.sh").write_text('''#!/usr/bin/env bash
set -eo pipefail
root="$(dirname "$(dirname "$(dirname "$0")")")"
git -C "$root" ls-tree HEAD -- DEPS engine bin/internal/release-candidate-branch.version | git -C "$root" hash-object --stdin
''')
        self.commit("baseline")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_bootstrap_hash_uses_official_baseline_not_fork_engine_changes(self):
        baseline = self.git("rev-parse", "HEAD")
        expected = prepare.baseline_engine(self.root, baseline)
        (self.root / "engine/source.cc").write_text("unpublished fork change")
        fork = self.commit("fork")
        self.assertEqual(prepare.baseline_engine(self.root, baseline), expected)
        self.assertNotEqual(prepare.baseline_engine(self.root, fork), expected)

    def test_release_engine_version_takes_priority_over_content_hash(self):
        path = self.root / "bin/internal/engine.version"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("b" * 40 + "\n")
        revision = self.commit("release")
        self.assertEqual(prepare.baseline_engine(self.root, revision), "b" * 40)

    def test_release_fork_keeps_its_published_engine_instead_of_hashing_merge_base(self):
        baseline = self.git("rev-parse", "HEAD")
        published_engine = "035316565ad77281a75305515e4682e6c4c6f7ca"
        pin = self.root / "bin/internal/engine.version"
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(published_engine + "\n")
        (self.root / "engine/source.cc").write_text("unpublished release-fork fix")
        self.commit("Flutter 3.35.7 fork with the official release Engine pin")

        self.assertEqual(prepare.baseline_engine(self.root, baseline), published_engine)

    def test_legacy_updater_selects_commit_even_when_content_hash_helper_exists(self):
        updater = self.root / "bin/internal/update_engine_version.sh"
        updater.write_text('''#!/usr/bin/env bash
set -e
root="$(dirname "$(dirname "$(dirname "$0")")")"
mkdir -p "$root/bin/cache"
git -C "$root" merge-base HEAD upstream/master > "$root/bin/cache/engine.stamp"
''')
        baseline = self.commit("Legacy updater uses the upstream commit as artifact version")
        self.assertEqual(prepare.baseline_engine(self.root, baseline), baseline)

    def test_version_policy_is_read_from_baseline_without_mutating_the_checkout(self):
        helper = self.root / "bin/internal/content_aware_hash.sh"
        helper.write_text(helper.read_text().replace("ls-tree HEAD --", 'ls-tree --format="%(objectname) %(path)" HEAD --'))
        baseline = self.commit("Historical content hash format")
        expected = subprocess.check_output(["bash", str(helper)], text=True).strip()
        helper.write_text("exit 99\n")
        self.commit("Fork must not supply the official baseline version policy")
        cache = self.root / "bin/cache/engine.stamp"
        cache.parent.mkdir()
        cache.write_text("existing target stamp\n")
        untracked_pin = self.root / "bin/internal/engine.version"
        untracked_pin.write_text("untracked-pin-must-not-be-used\n")
        head = self.git("rev-parse", "HEAD")
        status = self.git("status", "--porcelain")
        with patch.dict(os.environ, {"FLUTTER_PREBUILT_ENGINE_VERSION": "f" * 40,
                                     "FLUTTER_REALM": "unpublished-realm"}):
            self.assertEqual(prepare.baseline_engine(self.root, baseline), expected)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(cache.read_text(), "existing target stamp\n")


class GoldenComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "flutter"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=CI",
                        "-c", "user.email=ci@example.invalid", "commit", "--allow-empty",
                        "--quiet", "-m", "fixture"], check=True)
        self.outputs = Path(self.temp.name) / "results"
        self.environ = patch.dict(os.environ, {"FLUTTER_BASELINE_SHA": "b" * 40, "UPDATE_GOLDENS": "true"})
        self.environ.start()
        self.runner = golden.Golden(self.root, self.outputs, "android_engine_vulkan_tests")
        self.driver = self.runner.project / "test_driver"
        self.driver.mkdir(parents=True)
        comparator = self.root / "dev/tools/android_driver_extensions/lib/src/goldens.dart"
        comparator.parent.mkdir(parents=True)
        comparator.write_text("  Future<bool> compare(Uint8List imageBytes, Uri golden) async {\n"
                              "    final io.File goldenFile = _getTestFilePath(golden);\n"
                              "    return true;\n  }\n")
        self.commands = []
        self.runner.checkout = lambda revision: self.commands.append(("checkout", revision))
        self.runner.android_command = lambda *args: self.commands.append(("android", args))
        self.runner.baseline_images = lambda: (self.driver / "reference.png").write_bytes(b"trusted baseline")

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def test_target_comparison_cannot_inherit_update_goldens(self):
        def run(command, **kwargs):
            self.assertNotIn("UPDATE_GOLDENS", kwargs["env"])
            self.assertEqual((self.driver / "reference.png").read_bytes(), b"trusted baseline")
            actuals = Path(kwargs["env"]["FLUTTER_GOLDEN_ACTUALS_DIR"])
            actuals.mkdir()
            (actuals / "reference.actual.png").write_bytes(b"trusted baseline")
            return 0
        self.runner.run = run
        self.assertEqual(self.runner.execute(), 0)
        report = json.loads((self.outputs / "manifest.json").read_text())
        self.assertEqual(report["baseline_sha"], "b" * 40)
        self.assertTrue(report["baseline_images"])

    def test_missing_reference_images_fails_instead_of_passing_empty_suite(self):
        self.runner.baseline_images = lambda: None
        with self.assertRaisesRegex(ValueError, "no reference screenshots"):
            self.runner.execute()

    def test_mutated_reference_image_is_rejected(self):
        def run(command, **kwargs):
            (self.driver / "reference.png").write_bytes(b"target silently updated golden")
            actuals = Path(kwargs["env"]["FLUTTER_GOLDEN_ACTUALS_DIR"])
            actuals.mkdir()
            (actuals / "reference.actual.png").write_bytes(b"target silently updated golden")
            return 0
        self.runner.run = run
        with self.assertRaisesRegex(ValueError, "modified"):
            self.runner.execute()

    def test_success_without_actual_comparisons_is_rejected(self):
        self.runner.run = lambda *args, **kwargs: 0
        with self.assertRaisesRegex(ValueError, "without comparing"):
            self.runner.execute()


class GoldenPlanTest(unittest.TestCase):
    def write_runner(self, root, suites):
        runner = root / "dev/bots/test.dart"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("\n".join("'" + suite + "': runTests," for suite in suites))

    def test_target_tool_dependencies_do_not_erase_inherited_avd(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = {
                "platform_properties": {"linux_android_emu": {"properties": {
                    "dependencies": json.dumps([{"dependency": "android_virtual_device",
                                                "version": "android_36_google_apis_x64.textpb"},
                                               {"dependency": "open_jdk", "version": "version:21"}])}}},
                "targets": [{"name": "Linux_android_emu " + suite, "properties": {
                    "shard": suite, "dependencies": json.dumps([{"dependency": "open_jdk", "version": "version:25"}])}}
                    for suite in golden.SUITES],
            }
            (root / ".ci.yaml").write_text(yaml.safe_dump(ci))
            self.write_runner(root, golden.SUITES)
            matrix = golden.source_matrix(root)["include"]
            self.assertEqual(len(matrix), 7)
            self.assertTrue(all(entry["api"] == "36" for entry in matrix))
            self.assertTrue(all(entry["jdk"] == "25" for entry in matrix))

    def test_release_plan_runs_existing_goldens_without_requiring_future_suites(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = golden.SUITES[:2]
            ci = {
                "platform_properties": {"linux_android_emu": {"properties": {
                    "dependencies": json.dumps([{"dependency": "android_virtual_device",
                                                "version": "android_35_google_apis_x64.textpb"},
                                               {"dependency": "open_jdk", "version": "version:21"}])}}},
                "targets": [{"name": "Linux_android_emu " + suite, "properties": {"shard": suite}}
                            for suite in existing],
            }
            (root / ".ci.yaml").write_text(yaml.safe_dump(ci))
            self.write_runner(root, existing)
            self.assertEqual([entry["suite"] for entry in golden.source_matrix(root)["include"]], list(existing))
            self.write_runner(root, [*existing, golden.SUITES[2]])
            with self.assertRaisesRegex(ValueError, "official emulator target"):
                golden.source_matrix(root)

    def test_instrumentation_uses_flat_source_engine_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / "engine/src/out/android_debug_x64"
            engine.mkdir(parents=True)
            for artifact in ("flutter_embedding_debug", "x86_64_debug"):
                (engine / (artifact + ".pom")).write_text('<project xmlns="urn:maven"><version>1.0.0-source</version></project>')
                (engine / (artifact + ".jar")).write_bytes(b"source artifact")
                (engine / (artifact + ".maven-metadata.xml")).write_text("metadata")
            properties = golden.maven_repository(root, root / "maven")
            self.assertEqual(properties["ORG_GRADLE_PROJECT_local-engine-out"], str(engine))
            links = list((root / "maven").rglob("*.jar"))
            self.assertEqual(len(links), 2)
            self.assertTrue(all(path.read_bytes() == b"source artifact" for path in links))


if __name__ == "__main__":
    unittest.main()
