"""LSAI fork: the compile file is the union of every build the developer ran (app, its tests, a package's tests),
not only the newest one: each build log newer than the compile file is merged into it, oldest first."""
import os
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

import _forkroot  # noqa: F401  (fork root on sys.path)
import server
import xcactivitylog
from config import ServerConfig


class BuildLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.build_root = os.path.join(self.tmp, "dd")
        self.logs_dir = os.path.join(self.build_root, "Logs", "Build")
        os.makedirs(self.logs_dir)
        self.meta = xcactivitylog.metapath_from_buildroot(self.build_root)
        self.logs = {}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def log(self, name, scheme, stopped, mtime):
        path = os.path.join(self.logs_dir, name + ".xcactivitylog")
        with open(path, "wb") as f:
            f.write(b"log")
        os.utime(path, (mtime, mtime))
        self.logs[name] = {"fileName": name + ".xcactivitylog", "schemeIdentifier-schemeName": scheme,
                           "timeStoppedRecording": stopped}
        with open(self.meta, "wb") as f:
            plistlib.dump({"logs": self.logs}, f)
        return path


class LogPathsTests(BuildLogs):
    def test_logpaths_are_every_log_oldest_first_and_newest_logpath_is_the_last(self):
        tests = self.log("B", "MeshCore", 300.0, 300)
        app = self.log("A", "LidarScan", 100.0, 100)
        newest = self.log("C", "LidarScan", 500.0, 500)

        self.assertEqual(xcactivitylog.logpaths(self.meta), [app, tests, newest])
        self.assertEqual(xcactivitylog.newest_logpath(self.meta), newest)

    def test_logpaths_of_a_scheme_only(self):
        self.log("B", "MeshCore", 300.0, 300)
        app = self.log("A", "LidarScan", 100.0, 100)

        self.assertEqual(xcactivitylog.logpaths(self.meta, "LidarScan"), [app])

    def test_logpaths_without_manifest_is_empty(self):
        self.assertEqual(xcactivitylog.logpaths(self.meta), [])
        self.assertIsNone(xcactivitylog.newest_logpath(self.meta))


class LogsToParseTests(BuildLogs):
    def setUp(self):
        super().setUp()
        self.state = server.State.__new__(server.State)
        self.state.root_path = self.tmp
        self.state.config = ServerConfig(os.path.join(self.tmp, "buildServer.json"))
        self.state.config.build_root = self.build_root
        self.state._compile_file = os.path.join(self.tmp, "compile_file")
        self.state.observed_info = {}

    def compile_file_at(self, mtime):
        with open(self.state._compile_file, "w") as f:
            f.write("[]")
        os.utime(self.state._compile_file, (mtime, mtime))
        self.state.observed_info[self.state._compile_file] = mtime

    def test_first_start_parses_every_build_log_oldest_first(self):
        tests = self.log("B", "MeshCore", 300.0, 300)
        app = self.log("A", "LidarScan", 100.0, 100)
        self.state.observed_info[self.state._compile_file] = 0

        self.assertEqual(self.state.log_paths_for_invalid_compile_file(), [app, tests])

    def test_only_the_logs_newer_than_the_compile_file_are_parsed_and_each_once(self):
        self.log("A", "LidarScan", 100.0, 100)
        tests = self.log("B", "MeshCore", 300.0, 300)
        self.compile_file_at(200)

        self.assertEqual(self.state.log_paths_for_invalid_compile_file(), [tests])
        self.assertEqual(self.state.log_paths_for_invalid_compile_file(), [])

    def test_tick_parses_each_log_in_order(self):
        app = self.log("A", "LidarScan", 100.0, 100)
        tests = self.log("B", "MeshCore", 300.0, 300)
        self.state.observed_info[self.state._compile_file] = 0
        self.state.config.kind = "xcode"
        self.state.locking_compile_file = False

        with mock.patch.object(server.State, "handle_build_server_config_change", return_value=False), \
                mock.patch.object(server.State, "handle_compile_file_change", return_value=False), \
                mock.patch.object(server.State, "trigger_parse") as parse:
            self.state.tick()

        self.assertEqual([c.args[0] for c in parse.call_args_list], [app, tests])


if __name__ == "__main__":
    unittest.main()
