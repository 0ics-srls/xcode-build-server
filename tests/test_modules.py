"""LSAI fork: one build target per Swift module of the compile file, and buildTarget/prepare re-emits the
modules a target imports when their sources are newer than the module the developer's build left.

Without it, after an edit of a module's API (a rename) the files of the modules that import it are
re-indexed against the old module in DerivedData and their references are lost."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _forkroot  # noqa: F401  (fork root on sys.path)
import lsai_modules
import server
from config import ServerConfig

SWIFTC = "/xc/usr/bin/swiftc"


def command(module, file_list, emit_path, extra="", arch="arm64"):
    return (f"{SWIFTC} -module-name {module} -Onone @{file_list} -sdk /sdk -target {arch}-apple-ios17.0-simulator -g -Xcc -g "
            f"-module-cache-path /dd/ModuleCache.noindex -index-store-path /dd/Index.noindex/DataStore "
            f"-I /dd/Products -F /dd/Products -c -j10 -enable-batch-mode -incremental "
            f"-output-file-map /dd/{module}-OutputFileMap.json -save-temps -explicit-module-build "
            f"-clang-scanner-module-cache-path /dd/ModuleCache.noindex -emit-dependencies -emit-module "
            f"-emit-module-path {emit_path} -serialize-diagnostics -Xcc -D_LIBCPP_HARDENING_MODE\\=DEBUG "
            f"-emit-objc-header -emit-objc-header-path /dd/{module}-Swift.h -emit-const-values "
            f"-const-gather-protocols-list /dd/{module}_const.json -working-directory /w{extra}")


class Workspace(unittest.TestCase):
    """A temp workspace with Swift modules, their file lists, a compile file and the developer's built modules."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "lsai-modules")
        self.entries = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def source(self, module, name, text):
        path = os.path.join(self.tmp, "src", module, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def module(self, name, sources, arch="arm64"):
        file_list = os.path.join(self.tmp, "dd", f"{name}-{arch}.SwiftFileList")
        os.makedirs(os.path.dirname(file_list), exist_ok=True)
        with open(file_list, "w") as f:
            f.write("".join(s + "\n" for s in sources))
        emit_path = os.path.join(self.tmp, "dd", f"{name}-{arch}.swiftmodule")
        with open(emit_path, "w") as f:
            f.write("built")
        self.entries.append({"command": command(name, file_list, emit_path, arch=arch), "directory": "/w",
                             "fileLists": [file_list], "files": [], "module_name": name})
        return emit_path

    def compile_file(self):
        path = os.path.join(self.tmp, "compile_file")
        with open(path, "w") as f:
            json.dump(self.entries, f)
        return path

    def modules(self):
        return lsai_modules.swift_modules(self.compile_file(), {})


def touch(path, mtime):
    os.utime(path, (mtime, mtime))


class SwiftModulesTests(Workspace):
    def test_swift_modules_one_per_module_and_platform_the_last_entry_primary_with_its_file_list_sources(self):
        a = self.source("Core", "A.swift", "")
        b = self.source("Core", "B.swift", "")
        self.module("Core", [a], arch="arm64")
        x86 = self.module("Core", [a, b], arch="x86_64")
        self.module("Core", [a], arch="arm64")  # the same platform again: replaces the first, keeps its place

        modules = self.modules()

        self.assertEqual([(m.name, m.group, m.primary) for m in modules], [
            ("Core", "arm64-apple-ios17.0-simulator", True), ("Core", "x86_64-apple-ios17.0-simulator", False)])
        self.assertEqual(modules[1].sources, [a, b])
        self.assertEqual(modules[1].built_module, x86)
        self.assertEqual(modules[1].directory, "/w")

    def test_swift_modules_name_from_the_command_when_the_entry_has_no_module_name(self):
        a = self.source("App", "A.swift", "")
        self.module("App", [a])
        del self.entries[0]["module_name"]

        self.assertEqual([m.name for m in self.modules()], ["App"])

    def test_swift_modules_skips_entries_without_a_swift_command(self):
        self.entries.append({"command": "/xc/clang -c /p/a.m", "file": "/p/a.m", "directory": "/w"})

        self.assertEqual(self.modules(), [])

    def test_swift_modules_unreadable_compile_file_is_empty(self):
        self.assertEqual(lsai_modules.swift_modules(os.path.join(self.tmp, "missing"), {}), [])


class ModuleDependenciesTests(Workspace):
    def test_module_dependencies_are_the_other_modules_its_sources_import(self):
        core = self.source("Core", "Core.swift", "import Foundation\nimport simd\n")
        app = self.source("App", "App.swift", "import SwiftUI\n@preconcurrency import Core\n")
        tests = self.source("Tests", "T.swift", "@testable import App\nimport struct Core.Mesh\nimport XCTest\n")
        self.module("Core", [core])
        self.module("App", [app])
        self.module("Tests", [tests])

        deps = lsai_modules.module_dependencies(self.modules())

        self.assertEqual(deps, {"Core": [], "App": ["Core"], "Tests": ["App", "Core"]})

    def test_module_dependencies_ignore_imports_in_comments_and_its_own_name(self):
        core = self.source("Core", "Core.swift", "// import App\nimport Core\n")
        app = self.source("App", "App.swift", "")
        self.module("Core", [core])
        self.module("App", [app])

        self.assertEqual(lsai_modules.module_dependencies(self.modules())["Core"], [])


class EmitModuleCommandTests(Workspace):
    def test_emit_module_command_is_the_module_build_without_outputs_writing_only_the_module_to_the_out_dir(self):
        a = self.source("Core", "A.swift", "")
        self.module("Core", [a])
        module = self.modules()[0]

        args = lsai_modules.emit_module_command(module, self.out)

        self.assertEqual(args[0], SWIFTC)
        self.assertEqual(args[1:3], ["-I", self.out])  # first: a module re-emitted here wins over DerivedData
        self.assertEqual(args.count("-g"), 1)  # `-Xcc -g` goes to clang and stays; the driver's `-g` goes
        self.assertEqual(args[args.index("-g") - 1], "-Xcc")
        for gone in ["-c", "-j10", "-incremental", "-enable-batch-mode", "-save-temps", "-explicit-module-build",
                     "-emit-dependencies", "-serialize-diagnostics", "-emit-objc-header", "-emit-const-values",
                     "-index-store-path", "-output-file-map", "-emit-objc-header-path", "-const-gather-protocols-list",
                     "-clang-scanner-module-cache-path", "/dd/Index.noindex/DataStore", module.built_module]:
            self.assertNotIn(gone, args)
        self.assertEqual(args.count("-module-cache-path"), 1)
        tail = args[-9:]
        self.assertEqual(tail, ["-emit-module", "-emit-module-path", os.path.join(self.out, "Core.swiftmodule"),
                                "-module-cache-path", os.path.join(self.out, "ModuleCache"),
                                "-Xfrontend", "-experimental-skip-all-function-bodies",
                                "-Xfrontend", "-experimental-allow-module-with-compiler-errors"])
        self.assertIn("-D_LIBCPP_HARDENING_MODE=DEBUG", args)  # Xcode's escaped `\=` is a plain `=`
        self.assertIn("@" + self.entries[0]["fileLists"][0], args)


class StaleModulesTests(Workspace):
    """Which modules prepare re-emits for a target, in build order: make's rule on the modules' files."""

    def emitted(self, target, modules=None):
        modules = modules or self.modules()
        return [m.name for m in lsai_modules.modules_to_emit(
            [target], modules, lsai_modules.module_dependencies(modules), self.out)]

    def test_a_dependency_older_than_its_built_module_is_not_re_emitted(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(core, 100)
        touch(self.module("Core", [core]), 200)
        self.module("App", [app])

        self.assertEqual(self.emitted("App"), [])

    def test_a_dependency_edited_after_its_built_module_is_re_emitted_and_the_target_itself_is_not(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(self.module("Core", [core]), 200)
        touch(self.module("App", [app]), 200)
        touch(core, 300)
        touch(app, 300)

        self.assertEqual(self.emitted("App"), ["Core"])

    def test_transitive_dependencies_in_build_order_and_a_dependent_of_a_re_emitted_module_follows(self):
        base = self.source("Base", "Base.swift", "")
        core = self.source("Core", "Core.swift", "import Base\n")
        app = self.source("App", "App.swift", "import Core\n")
        touch(self.module("Base", [base]), 200)
        touch(self.module("Core", [core]), 200)
        self.module("App", [app])
        touch(core, 100)
        touch(base, 300)

        self.assertEqual(self.emitted("App"), ["Base", "Core"])

    def test_a_module_already_re_emitted_and_newer_than_its_sources_is_not_emitted_again(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(self.module("Core", [core]), 200)
        self.module("App", [app])
        touch(core, 300)
        os.makedirs(self.out)
        with open(os.path.join(self.out, "Core.swiftmodule"), "w") as f:
            f.write("emitted")
        touch(os.path.join(self.out, "Core.swiftmodule"), 400)

        self.assertEqual(self.emitted("App"), [])

    def test_a_re_emitted_module_older_than_a_new_developer_build_is_removed_so_the_build_wins(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(core, 100)
        touch(self.module("Core", [core]), 500)
        self.module("App", [app])
        os.makedirs(self.out)
        for name in ["Core.swiftmodule", "Core.swiftdoc", "Core.swiftsourceinfo", "Core.abi.json"]:
            with open(os.path.join(self.out, name), "w") as f:
                f.write("emitted")
            touch(os.path.join(self.out, name), 400)

        self.assertEqual(self.emitted("App"), [])
        self.assertEqual(os.listdir(self.out), [])

    def test_a_module_in_an_import_cycle_is_visited_once(self):
        a = self.source("A", "A.swift", "import B\n")
        b = self.source("B", "B.swift", "import A\n")
        touch(self.module("A", [a]), 100)
        touch(self.module("B", [b]), 100)
        touch(a, 300)
        touch(b, 300)

        self.assertEqual(sorted(self.emitted("A")), ["A", "B"])


class PrepareTests(Workspace):
    def test_prepare_runs_each_emit_in_the_module_directory_and_a_failure_is_logged_and_the_next_runs(self):
        base = self.source("Base", "Base.swift", "")
        core = self.source("Core", "Core.swift", "import Base\n")
        app = self.source("App", "App.swift", "import Core\nimport Base\n")
        touch(self.module("Base", [base]), 200)
        touch(self.module("Core", [core]), 200)
        self.module("App", [app])
        touch(base, 300)
        touch(core, 300)
        modules = self.modules()
        calls = []

        def run(args, cwd, capture_output, text):
            calls.append((args[args.index("-module-name") + 1], cwd))
            return mock.Mock(returncode=1 if not calls[1:] else 0, stderr="error: boom")

        with self.assertLogs(lsai_modules.logger, level="WARNING") as logs:
            emitted = lsai_modules.prepare(["App"], modules, self.out, run=run)

        self.assertEqual(calls, [("Base", "/w"), ("Core", "/w")])
        self.assertEqual(emitted, ["Core"])
        self.assertTrue(any("Base" in line and "boom" in line for line in logs.output), logs.output)
        self.assertTrue(os.path.isdir(lsai_modules.group_dir(self.out, "arm64-apple-ios17.0-simulator")))


class ServerBase(Workspace):
    """A State over the workspace's compile file, as the server's shared state."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.state = server.State.__new__(server.State)
        self.state.root_path = self.root
        self.state.cache_path = os.path.join(self.tmp, "cache")
        self.state.config = ServerConfig(os.path.join(self.root, "buildServer.json"))
        self.state.store = {}
        self.previous_shared_state = server.shared_state
        server.shared_state = self.state

    def tearDown(self):
        server.shared_state = self.previous_shared_state
        super().tearDown()

    def two_modules(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        self.module("Core", [core])
        self.module("App", [app])
        self.state.compile_file = self.compile_file()
        return core, app


class ServerModuleTargetsTests(ServerBase):
    """The server side: targets, their sources, prepare and the flags of a Swift file."""

    def test_build_targets_one_per_module_with_its_imports_as_dependencies_and_the_fallback_target_last(self):
        self.two_modules()

        targets = server.dispatch["workspace_buildTargets"]({"id": 1})["result"]["targets"]

        self.assertEqual([t["id"]["uri"] for t in targets], ["module://Core", "module://App", "xcode://workspace"])
        self.assertEqual(targets[1]["dependencies"], [{"uri": "module://Core"}])
        self.assertEqual(targets[0]["dependencies"], [])
        # sourcekit-lsp takes a file's target with the smallest uri: a module, never the fallback
        self.assertLess("module://App", lsai_modules.FALLBACK_TARGET["uri"])

    def test_build_targets_without_compile_file_is_the_fallback_target_only(self):
        self.state.compile_file = None

        targets = server.dispatch["workspace_buildTargets"]({"id": 1})["result"]["targets"]

        self.assertEqual([t["id"]["uri"] for t in targets], ["xcode://workspace"])

    def test_build_target_sources_module_target_lists_its_files_and_the_fallback_the_directories(self):
        core, app = self.two_modules()

        with mock.patch.object(server.State, "swift_output_path", lambda _self, f: None):
            response = server.dispatch["buildTarget_sources"]({"id": 2, "params": {"targets": [
                {"uri": "module://App"}, lsai_modules.FALLBACK_TARGET]}})

        app_item, fallback_item = response["result"]["items"]
        self.assertEqual(app_item["target"], {"uri": "module://App"})
        self.assertEqual(app_item["sources"], [{"uri": Path(app).as_uri(), "kind": 1, "generated": False}])
        self.assertEqual([s["kind"] for s in fallback_item["sources"]], [2])
        self.assertEqual(fallback_item["sources"][0]["uri"], Path(self.root).as_uri())

    def test_build_target_prepare_re_emits_the_stale_imported_modules_into_the_cache(self):
        self.two_modules()

        with mock.patch.object(lsai_modules, "prepare", return_value=[]) as prepare:
            response = server.dispatch["buildTarget_prepare"](
                {"id": 3, "params": {"targets": [{"uri": "module://App"}, lsai_modules.FALLBACK_TARGET]}})

        self.assertEqual(response, {"jsonrpc": "2.0", "id": 3, "result": {}})
        names, modules, out = prepare.call_args[0]
        self.assertEqual(names, ["App"])
        self.assertEqual([m.name for m in modules], ["Core", "App"])
        self.assertEqual(out, os.path.join(self.state.cache_path, "lsai-modules"))

    def test_swift_file_options_drop_the_explicit_module_build(self):
        options = self.state.optionsForFlags(["-module-name", "App", "-target", "arm64-apple-ios17.0-simulator",
                                              "-explicit-module-build", "/p/A.swift"], "/w")

        self.assertNotIn("-explicit-module-build", options["options"])

    def test_swift_file_options_search_the_re_emitted_modules_first_and_clang_options_are_unchanged(self):
        self.state.compile_file = None
        swift = self.state.optionsForFlags(["-module-name", "App", "-target", "arm64-apple-ios17.0-simulator",
                                            "-I", "/dd/Products", "/p/A.swift"], "/w")
        clang = self.state.optionsForFlags(["-x", "objective-c", "-I/dd/include", "/p/a.m"], "/w")

        modules_dir = os.path.join(self.state.cache_path, "lsai-modules", "arm64-apple-ios17.0-simulator")
        self.assertEqual(swift["options"], ["-I", modules_dir, "-module-name", "App", "-target",
                                            "arm64-apple-ios17.0-simulator", "-I", "/dd/Products", "/p/A.swift"])
        self.assertEqual(clang["options"], ["-x", "objective-c", "-I/dd/include", "/p/a.m"])



class PrepareAcrossArchitecturesTests(Workspace):
    def test_prepare_re_emits_the_dependency_of_the_platform_the_target_files_are_compiled_for(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(self.module("Core", [core], arch="x86_64"), 200)
        touch(self.module("Core", [core], arch="arm64"), 200)
        self.module("App", [app], arch="arm64")
        self.module("App", [app], arch="x86_64")  # the App files get x86_64 flags; Core's last entry is arm64
        touch(core, 300)
        calls = []

        def run(args, cwd, capture_output, text):
            calls.append((args[args.index("-module-name") + 1], args[args.index("-target") + 1], args[2]))
            return mock.Mock(returncode=0, stderr="")

        lsai_modules.prepare(["App"], self.modules(), self.out, run=run)

        self.assertEqual(calls, [("Core", "x86_64-apple-ios17.0-simulator",
                                  lsai_modules.group_dir(self.out, "x86_64-apple-ios17.0-simulator"))])


class RetargetTests(Workspace):
    def test_a_dependency_the_log_kept_for_another_architecture_only_is_re_emitted_retargeted(self):
        core = self.source("Core", "Core.swift", "")
        app = self.source("App", "App.swift", "import Core\n")
        touch(self.module("Core", [core], arch="arm64"), 200)
        self.module("App", [app], arch="x86_64")
        touch(core, 300)
        calls = []

        def run(args, cwd, capture_output, text):
            calls.append((args[args.index("-module-name") + 1], args[args.index("-target") + 1]))
            return mock.Mock(returncode=0, stderr="")

        lsai_modules.prepare(["App"], self.modules(), self.out, run=run)

        self.assertEqual(calls, [("Core", "x86_64-apple-ios17.0-simulator")])

    def test_a_module_of_another_platform_is_never_retargeted(self):
        self.entries = []
        core = self.source("Core", "Core.swift", "")
        self.module("Core", [core], arch="arm64")
        modules = self.modules()

        members = lsai_modules.group_members(modules, "arm64-apple-macosx14.0")

        self.assertEqual(members, [])


class PackageTestModulesTests(Workspace):
    """A local package's test target: Xcode builds the package's library for the app, `swift test` its tests."""

    def package(self):
        package = os.path.join(self.tmp, "root", "Core")
        self.library = os.path.join(package, "Sources", "Core", "Core.swift")
        self.tests = [os.path.join(package, "Tests", "CoreTests", "CoreTests.swift"),
                      os.path.join(package, "Tests", "CoreTests", "Sub", "More.swift")]
        for path in [self.library] + self.tests:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("@testable import Core\n" if "Tests" in path else "")
        os.makedirs(os.path.join(package, "Tests", "CoreTests", "Fixtures"))
        with open(os.path.join(package, "Tests", "CoreTests", "Fixtures", "cube.obj"), "w") as f:
            f.write("")
        with open(os.path.join(package, "Package.swift"), "w") as f:
            f.write("// swift-tools-version: 6.0\n")
        return package

    def test_package_test_module_is_the_tests_folder_with_the_library_flags_and_no_library_outputs(self):
        self.package()
        self.module("Core", [self.library], arch="arm64")
        self.module("Core", [self.library], arch="x86_64")

        tests = lsai_modules.package_test_modules(self.modules())

        self.assertEqual([(m.name, m.group, m.primary, m.package_test) for m in tests], [
            ("CoreTests", "arm64-apple-ios17.0-simulator", False, True),
            ("CoreTests", "x86_64-apple-ios17.0-simulator", True, True)])
        primary = tests[1]
        self.assertEqual(primary.sources, self.tests)
        args = primary.command
        self.assertEqual(lsai_modules._value(args, "-module-name"), "CoreTests")
        self.assertEqual(args[-2:], self.tests)
        for gone in ["-emit-module-path", "-output-file-map", "-index-store-path", "-emit-objc-header-path", "-emit-module"]:
            self.assertNotIn(gone, args)
        self.assertFalse(any(a.startswith("@") for a in args))
        self.assertIn("-sdk", args)
        self.assertEqual(lsai_modules._value(args, "-target"), "x86_64-apple-ios17.0-simulator")

    def test_a_module_of_the_xcode_project_has_no_package_tests(self):
        app = self.source("App", "App.swift", "")
        self.module("App", [app])

        self.assertEqual(lsai_modules.package_test_modules(self.modules()), [])


class ServerPackageTestsTests(ServerBase):
    def setUp(self):
        super().setUp()
        tests = PackageTestModulesTests.package
        self.root_package = tests(self)
        self.app = self.source("App", "App.swift", "import Core\n")
        self.module("Core", [self.library])
        self.module("App", [self.app])
        self.state.compile_file = self.compile_file()

    def test_build_targets_have_the_package_test_target_depending_on_its_library(self):
        targets = server.dispatch["workspace_buildTargets"]({"id": 1})["result"]["targets"]

        self.assertEqual([t["id"]["uri"] for t in targets],
                         ["module://Core", "module://App", "module://CoreTests", "xcode://workspace"])
        self.assertEqual(targets[2]["dependencies"], [{"uri": "module://Core"}])

    def test_a_package_test_file_has_the_test_flags_searching_the_re_emitted_modules_first(self):
        options = self.state.optionsForFile(Path(self.tests[0]).as_uri())

        flags = options["options"]
        self.assertEqual(flags[:2], ["-I", lsai_modules.group_dir(self.state.modules_dir, "arm64-apple-ios17.0-simulator")])
        self.assertEqual(lsai_modules._value(flags, "-module-name"), "CoreTests")
        self.assertEqual(flags[-2:], self.tests)

    def test_a_package_test_file_has_an_index_unit_output_path_of_its_own(self):
        first = self.state.swift_output_path(self.tests[0])
        second = self.state.swift_output_path(self.tests[1])

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(lsai_modules.group_dir(self.state.modules_dir, "arm64-apple-ios17.0-simulator")))

    def test_prepare_of_the_package_tests_re_emits_their_library(self):
        touch(self.library, 10 ** 10)
        calls = []

        def run(args, cwd, capture_output, text):
            calls.append(args[args.index("-module-name") + 1])
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(lsai_modules.subprocess, "run", run):
            server.dispatch["buildTarget_prepare"]({"id": 4, "params": {"targets": [{"uri": "module://CoreTests"}]}})

        self.assertEqual(calls, ["Core"])


if __name__ == "__main__":
    unittest.main()
