"""LSAI fork: every compiled Swift file is a file-kind source, and the build server supports prepare."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _forkroot  # noqa: F401  (fork root on sys.path)
import server
from config import ServerConfig

DUMMY_TARGET = {"uri": "dummy://dummy"}


class StateTestBase(unittest.TestCase):
    """A State without file watching or a real build: root, config and compile file under a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.state = server.State.__new__(server.State)
        self.state.root_path = self.root
        self.state.config = ServerConfig(os.path.join(self.root, "buildServer.json"))
        self.state.compile_file = None
        self.previous_shared_state = server.shared_state

    def tearDown(self):
        server.shared_state = self.previous_shared_state
        shutil.rmtree(self.tmp)

    def write_file_list(self, name, paths):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write("".join(p + "\n" for p in paths))
        return path

    def write_compile_file(self, entries):
        path = os.path.join(self.tmp, "compile_file")
        with open(path, "w") as f:
            json.dump(entries, f)
        self.state.compile_file = path


class CompiledSwiftFilesTests(StateTestBase):
    def test_compiled_swift_files_lists_files_file_and_file_lists_once_in_first_seen_order(self):
        file_list = self.write_file_list("A.SwiftFileList", ["/p/B.swift", "/p/A.swift"])
        self.write_compile_file([
            {"files": ["/p/A.swift"], "fileLists": [file_list]},
            {"file": "/p/C.swift"},
        ])

        result = self.state.compiled_swift_files()

        # entry 1: files [A] then its file list [B, A] → A, B; entry 2: file C → C
        self.assertEqual(result, ["/p/A.swift", "/p/B.swift", "/p/C.swift"])

    def test_compiled_swift_files_ignores_non_swift_paths(self):
        self.write_compile_file([
            {"files": ["/p/A.m", "/p/B.c", "/p/C.h", "/p/D.swift"]},
            {"file": "/p/E.m"},
        ])

        result = self.state.compiled_swift_files()

        self.assertEqual(result, ["/p/D.swift"])

    def test_compiled_swift_files_without_compile_file_returns_empty(self):
        self.state.compile_file = None

        result = self.state.compiled_swift_files()

        self.assertEqual(result, [])

    def test_compiled_swift_files_invalid_json_returns_empty_and_logs_warning(self):
        path = os.path.join(self.tmp, "compile_file")
        with open(path, "w") as f:
            f.write("{ not json")
        self.state.compile_file = path

        with self.assertLogs(server.logger, level="WARNING") as logs:
            result = self.state.compiled_swift_files()

        self.assertEqual(result, [])
        self.assertTrue(any("compile file unreadable" in line for line in logs.output), logs.output)


class BuildTargetSourcesTests(StateTestBase):
    def test_build_target_sources_dummy_target_has_root_directory_and_one_file_item_per_swift_file(self):
        self.write_compile_file([{"files": ["/p/A.swift", "/p/B.swift", "/p/C.m"]}])
        server.shared_state = self.state

        response = server.dispatch["buildTarget_sources"](
            {"id": 7, "params": {"targets": [DUMMY_TARGET]}})

        sources = response["result"]["items"][0]["sources"]
        self.assertIn({"uri": Path(self.root).as_uri(), "kind": 2, "generated": False}, sources)
        file_items = [s for s in sources if s["kind"] == 1]
        self.assertEqual(file_items, [
            {"uri": Path("/p/A.swift").as_uri(), "kind": 1, "generated": False},
            {"uri": Path("/p/B.swift").as_uri(), "kind": 1, "generated": False},
        ])


class SwiftOutputPathTests(StateTestBase):
    """sourcekit-lsp re-indexes an edited file into the index unit named by its output path: one per file."""

    def write_output_file_map(self, entries):
        path = os.path.join(self.tmp, "App-OutputFileMap.json")
        with open(path, "w") as f:
            json.dump(entries, f)
        return path

    def test_swift_output_path_reads_index_unit_output_path_from_the_flags_output_file_map(self):
        map_path = self.write_output_file_map({
            "/p/A.swift": {"index-unit-output-path": "/dd/A.o", "object": "/dd/A-obj.o"},
        })
        self.state.store = {}
        with mock.patch.object(server, "GetFlags", return_value=["-module-name", "App", "-output-file-map", map_path]):
            result = self.state.swift_output_path("/p/A.swift")

        self.assertEqual(result, "/dd/A.o")

    def test_swift_output_path_falls_back_to_object(self):
        map_path = self.write_output_file_map({"/p/B.swift": {"object": "/dd/B.o"}})
        self.state.store = {}
        with mock.patch.object(server, "GetFlags", return_value=["-output-file-map", map_path]):
            result = self.state.swift_output_path("/p/B.swift")

        self.assertEqual(result, "/dd/B.o")

    def test_swift_output_path_without_output_file_map_is_none(self):
        self.state.store = {}
        with mock.patch.object(server, "GetFlags", return_value=["-module-name", "App"]):
            result = self.state.swift_output_path("/p/A.swift")

        self.assertIsNone(result)

    def test_build_target_sources_file_items_carry_their_sourcekit_output_path(self):
        self.write_compile_file([{"files": ["/p/A.swift", "/p/B.swift"]}])
        self.state.store = {}
        server.shared_state = self.state
        outputs = {"/p/A.swift": "/dd/A.o", "/p/B.swift": None}

        with mock.patch.object(server.State, "swift_output_path", lambda _self, f: outputs[f]):
            response = server.dispatch["buildTarget_sources"]({"id": 8, "params": {"targets": [DUMMY_TARGET]}})

        items = {s["uri"]: s for s in response["result"]["items"][0]["sources"] if s["kind"] == 1}
        a = items[Path("/p/A.swift").as_uri()]
        self.assertEqual(a["dataKind"], "sourceKit")
        self.assertEqual(a["data"], {"language": "swift", "kind": "source", "outputPath": "/dd/A.o"})
        self.assertNotIn("data", items[Path("/p/B.swift").as_uri()])


class BuildInitializeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.root)
        self.previous_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp  # the server's cache dir (~/Library/Caches/…) lands in the temp dir
        self.previous_shared_state = server.shared_state
        server.shared_state = None

    def tearDown(self):
        server.shared_state = self.previous_shared_state
        if self.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous_home
        shutil.rmtree(self.tmp)

    def test_build_initialize_data_has_prepare_provider_and_keeps_sourcekit_options_provider(self):
        message = {"id": 1, "params": {"rootUri": Path(self.root).as_uri(), "bspVersion": "2.2.0"}}

        response = server.dispatch["build_initialize"](message)

        data = response["result"]["data"]
        self.assertIs(data["prepareProvider"], True)
        self.assertIs(data["outputPathsProvider"], True)
        self.assertIs(data["sourceKitOptionsProvider"], True)


class BuildTargetPrepareTests(unittest.TestCase):
    def test_build_target_prepare_answers_empty_result_with_request_id(self):
        message = {"id": 42, "params": {"targets": [DUMMY_TARGET]}}

        response = server.dispatch["buildTarget_prepare"](message)

        self.assertEqual(response, {"jsonrpc": "2.0", "id": 42, "result": {}})


class LsaiSentinelConfigTests(unittest.TestCase):
    def test_server_config_with_lsai_key_keeps_kind_and_build_root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "buildServer.json")
        with open(path, "w") as f:
            json.dump({"kind": "xcode", "build_root": "/dd/App-abc", "workspace": "/w/App.xcodeproj/project.xcworkspace",
                       "lsai": "LSAI-MANAGED v1"}, f)

        config = ServerConfig(path)

        self.assertEqual(config.kind, "xcode")
        self.assertEqual(config.build_root, "/dd/App-abc")
        self.assertEqual(config.data["lsai"], "LSAI-MANAGED v1")


if __name__ == "__main__":
    unittest.main()
