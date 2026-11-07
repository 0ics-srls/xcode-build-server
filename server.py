import hashlib
import json
import logging
import os
import sys
from threading import Lock, Thread, main_thread
import time
from typing import Optional
import urllib.parse
from pathlib import Path

from compile_database import (
    GetFlags,
    GetWorkingDirectory,
    InferFlagsForSwift,
    filekey,
    filterFlags,
    newfileForCompileFile,
)
from config import ServerConfig, env
from misc import force_remove, get_mtime, VERSION, version_compare
import lsai_modules

logger = logging.getLogger(__name__)


def send(data):
    data_str = json.dumps(data)
    logger.debug("Res <-- %s", data_str)
    try:
        sys.stdout.write(f"Content-Length: {len(data_str)}\r\n\r\n{data_str}")
        sys.stdout.flush()
    except IOError as e:
        # stdout closed, time to quit
        raise SystemExit(0) from e


def uri2filepath(uri):
    result = urllib.parse.urlparse(uri)
    if result.scheme != "file":
        raise ValueError(uri)
    return urllib.parse.unquote(result.path)


def uri2realpath(uri):
    path = uri2filepath(uri)
    return os.path.realpath(path)


def uptodate(target: str, srcs: list[str]):
    target_mtime = get_mtime(target)
    srcs_mtime = (get_mtime(src) for src in srcs)
    return all(src_mtime < target_mtime for src_mtime in srcs_mtime)


class State(object):
    def __init__(self, root_path: str, cache_path, new_version):
        """
        pass in path should be absolute and normalized
        :param new_version: if LSP is new version and support target prepare
        """
        self.root_path = root_path
        self.cache_path = cache_path
        self.new_version = new_version
        os.makedirs(cache_path, exist_ok=True)

        # buildServer.json used as config dict
        config_path = os.path.join(root_path, "buildServer.json")
        self.config = ServerConfig(config_path)

        # opened files need to be notified when flags changed
        self.observed_uri = set()
        # background thread to observe changes
        self.observed_thread: Optional[Thread] = None

        self.exception_count = 0
        self.last_exception_time = 0.0

        # {path: mtime} cache. use to find changes
        self.observed_info = {self.config.path: get_mtime(self.config.path)}

        self.reinit_compile_info()
        # NOTE:thread-safety: for state shared by main and background watch thread,
        # can only changed in sync_compile_file, which block all thread and no one access it.
        # other time, the shared state is readonly and safe..

    def get_compile_file(self, config: ServerConfig):
        # isolate xcode generate compile file and manual compile_file
        if config.kind == "xcode":
            hash = hashlib.md5(config.build_root.encode("utf-8")).hexdigest()
            name = ["compile_file", config.scheme or "_last", hash]
            if config.skip_validate_bin:
                name[0] = "compile_file1"
            return os.path.join(self.cache_path, "-".join(name))
        # manual compile_file
        return os.path.join(self.root_path, ".compile")

    def reinit_compile_info(self):
        """all the compile information may change in background"""

        # store use to save compile_datainfo. it will be reload when config changes.
        self.store = {}  # main-thread
        self._compile_file = self.get_compile_file(self.config)
        if os.path.exists(self._compile_file):
            self.compile_file = self._compile_file
            logger.info(f"use flags from {self._compile_file}")
        else:
            self.compile_file = None

        # self._compile_file may change. need to init mtime to avoid trigger a change event
        self.observed_info[self._compile_file] = get_mtime(self._compile_file)

    def compiled_swift_files(self):
        """Swift sources of the compile file, each once, in first-seen order (LSAI fork).

        sourcekit-lsp re-indexes only files listed as file-kind sources of a target, so every
        compiled Swift file is listed. An unreadable compile file gives [] and a warning.
        """
        if not self.compile_file:
            return []
        try:
            with open(self.compile_file) as f:
                entries = json.load(f)
        except (OSError, ValueError) as ex:
            logger.warning("compile file unreadable %s: %s", self.compile_file, ex)
            return []
        seen, result = set(), []
        for entry in entries:
            paths = list(entry.get("files") or [])
            if entry.get("file"):
                paths.append(entry["file"])
            for list_path in entry.get("fileLists") or []:
                try:
                    with open(list_path) as lf:
                        paths.extend(line.strip() for line in lf if line.strip())
                except OSError as ex:
                    logger.warning("file list unreadable %s: %s", list_path, ex)
            for p in paths:
                if p.endswith(".swift") and p not in seen:
                    seen.add(p)
                    result.append(p)
        return result

    def swift_output_path(self, file_path):
        """The index unit output path of a Swift file (LSAI fork): its `index-unit-output-path` (else `object`)
        in the OutputFileMap its compile flags name. sourcekit-lsp needs one per file when it re-indexes an edit;
        without it every file of the module writes the same index unit and the last one replaces the others.
        None when the flags or the map do not say."""
        if module := self.package_test_module_of(file_path):  # never compiled: an index unit name of its own
            relative = file_path.lstrip(os.sep).replace(os.sep, "_")
            return os.path.join(lsai_modules.group_dir(self.modules_dir, module.group), "units", module.name, relative + ".o")
        try:
            flags = self.file_flags(file_path)
            if not flags:
                flags = InferFlagsForSwift(file_path, self.compile_file, store=self.store)
            map_path = flags[flags.index("-output-file-map") + 1]
        except Exception as ex:  # no compile info for this file: sourcekit-lsp indexes it without an output path
            logger.debug("no output file map for %s: %s", file_path, ex)
            return None
        maps = self.__dict__.setdefault("_output_file_maps", {})
        if map_path not in maps:
            try:
                with open(map_path) as f:
                    maps[map_path] = json.load(f)
            except (OSError, ValueError) as ex:
                logger.warning("output file map unreadable %s: %s", map_path, ex)
                maps[map_path] = {}
        entry = maps[map_path].get(file_path) or {}
        return entry.get("index-unit-output-path") or entry.get("object")

    @property
    def modules_dir(self):
        """Where prepare re-emits the modules a target imports (LSAI fork): this server's cache, never DerivedData."""
        return os.path.join(self.cache_path, lsai_modules.MODULES_DIR_NAME)

    def swift_modules(self):
        """The Swift modules of the compile file (LSAI fork), each a build target."""
        if not self.compile_file:
            return []
        if "lsai_modules" not in self.store:
            self.store["lsai_modules"] = lsai_modules.swift_modules(self.compile_file, self.store)
        return self.store["lsai_modules"]

    def package_test_modules(self):
        """The test targets of the local packages the Xcode build compiles (LSAI fork), with their library's flags."""
        if "lsai_package_test_modules" not in self.store:
            self.store["lsai_package_test_modules"] = lsai_modules.package_test_modules(self.swift_modules())
        return self.store["lsai_package_test_modules"]

    def all_modules(self):
        return self.swift_modules() + self.package_test_modules()

    def target_modules(self):
        """The build targets (LSAI fork): each module once, as its files are compiled."""
        return [m for m in self.all_modules() if m.primary]

    def module_dependencies(self):
        if "lsai_module_dependencies" not in self.store:
            self.store["lsai_module_dependencies"] = lsai_modules.module_dependencies(self.target_modules())
        return self.store["lsai_module_dependencies"]

    def package_test_module_of(self, file_path):
        key = filekey(file_path)
        for module in self.package_test_modules():
            if module.primary and any(filekey(s) == key for s in module.sources):
                return module
        return None

    def file_flags(self, file_path):
        """The compile flags of a file: the Xcode build's, else those of the package test target that lists it."""
        flags = GetFlags(file_path, self.compile_file, store=self.store)
        if flags:
            return flags
        if module := self.package_test_module_of(file_path):
            return list(filterFlags(module.command[1:], self.store.setdefault("filelist", {})))
        return None

    def source_item(self, file_path):
        """A Swift file as a file-kind source with its index unit output path (LSAI fork)."""
        item = {"uri": Path(file_path).as_uri(), "kind": 1, "generated": False}
        output_path = self.swift_output_path(file_path)
        if output_path:
            item["dataKind"] = "sourceKit"
            item["data"] = {"language": "swift", "kind": "source", "outputPath": output_path}
        return item

    @property
    def indexStorePath(self) -> Optional[str]:
        if self.config.kind == "xcode":
            if not (root := self.config.build_root):
                return None
            return os.path.join(root, "Index.noindex/DataStore")

        return self.config.indexStorePath

    @property
    def compile_lock_path(self):
        from xclog_parser import output_lock_path

        return output_lock_path(self._compile_file)

    def register_uri(self, uri):
        self.observed_uri.add(uri)

        file_path = uri2realpath(uri)
        flags = self.file_flags(file_path)

        if not flags and env.new_file:
            if filekeys := newfileForCompileFile(
                file_path, self.compile_file, store=self.store
            ):
                # add new file success, update options for module files
                for v in self.observed_uri:
                    if filekey(uri2filepath(v)) in filekeys:
                        self.notify_option_changed(v)
                return

        if not flags and file_path.endswith(".swift"):
            flags = InferFlagsForSwift(file_path, self.compile_file, store=self.store)

        working_directory = GetWorkingDirectory(file_path, self.compile_file, store=self.store)

        self._notify_option_changed(uri, self.optionsForFlags(flags, working_directory))

    def unregister_uri(self, uri):
        self.observed_uri.remove(uri)

    def sourceKitOptions(self, uri):
        file_path = uri2realpath(uri)
        flags = self.file_flags(file_path)
        if not flags and env.new_file:
            if newfileForCompileFile(file_path, self.compile_file, store=self.store):
                flags = GetFlags(file_path, self.compile_file, store=self.store)
                self.notify_target_changes()

        if not flags and file_path.endswith(".swift"):
            flags = InferFlagsForSwift(file_path, self.compile_file, store=self.store)

        working_directory = GetWorkingDirectory(file_path, self.compile_file, store=self.store)

        if result := self.optionsForFlags(flags, working_directory):
            return {
                "compilerArguments": result["options"],
                "workingDirectory": result["workingDirectory"],
            }
        return None

    def optionsForFile(self, uri):
        file_path = uri2realpath(uri)
        flags = self.file_flags(file_path)
        if not flags and file_path.endswith(".swift"):
            flags = InferFlagsForSwift(file_path, self.compile_file, store=self.store)
        working_directory = GetWorkingDirectory(file_path, self.compile_file, store=self.store)
        return self.optionsForFlags(flags, working_directory)

    def optionsForFlags(self, flags, working_directory):
        if flags is None:
            return None
        try:
            workdir = flags[flags.index("-working-directory") + 1]
        except (IndexError, ValueError):
            if working_directory:
                workdir = working_directory
            else:
                workdir = os.getcwd()
        if "-module-name" in flags:  # LSAI fork: a Swift file finds the modules prepare re-emitted first
            modules_dir = lsai_modules.group_dir(self.modules_dir, lsai_modules._value(flags, "-target"))
            # an explicit module build needs the precompiled modules of the build's own graph: the index compile of
            # a file crashes on them (SIGTRAP), the implicit module cache serves every platform
            flags = ["-I", modules_dir] + [f for f in flags if f != "-explicit-module-build"]
        return {
            "options": flags,
            "workingDirectory": workdir,
        }

    def notify_option_changed(self, uri):
        # no clear options?
        self._notify_option_changed(uri, self.optionsForFile(uri))

    def _notify_option_changed(self, uri, options):
        # empty options is nouse and lsp will stop working.., at least there should has a infer flags..
        if options is None:
            return
        notification = {
            "jsonrpc": "2.0",
            "method": "build/sourceKitOptionsChanged",
            "params": {
                "uri": uri,
                "updatedOptions": options,
            },
        }
        send(notification)

    def shutdown(self):
        self.observed_thread = None  # release to end in subthread

    ########## observed flag changes in background

    def start_observe_changes(self):
        if self.observed_thread:
            logger.warn("already observing!!")
            return

        def start():
            try:
                while (
                    self.observed_thread
                    and main_thread().is_alive()
                    and self == shared_state
                ):
                    try:
                        self.tick()
                    except Exception as e:
                        now = time.time()
                        if now - self.last_exception_time > 10:
                            self.exception_count = 1
                        else:
                            self.exception_count += 1
                        self.last_exception_time = now

                        if self.exception_count >= 3:
                            raise
                        logger.error(f"observe tick failed ({self.exception_count}/3): {e}")

                    time.sleep(1)
            except Exception as e:
                logger.exception(f"observe thread exit by exception: {e}")

        self.locking_compile_file = False

        self.observed_thread = Thread(target=start)
        self.observed_thread.start()

    def tick(self):
        if self.handle_build_server_config_change():
            return
        if self.handle_compile_file_change():
            return
        if self.config.kind != "xcode":
            return
        if self.check_locking_compile_file():
            return
        for log_path in self.log_paths_for_invalid_compile_file():
            self.trigger_parse(log_path)
            if self.locking_compile_file:  # another parse holds the compile file: it merges the logs after
                break

    def handle_build_server_config_change(self):
        mtime = get_mtime(self.config.path)
        if mtime > self.observed_info.get(self.config.path, 0):
            self.observed_info[self.config.path] = mtime
            # currently config update is useto change isolate compile_file path.
            # so just check if the path changes
            new_config = ServerConfig(self.config.path)
            if self.get_compile_file(new_config) == self._compile_file:
                return False

            # compile_file did change.. so change it
            def assign():
                self.config = new_config

            self.sync_compile_file(before=assign)
            return True

    def handle_compile_file_change(self):
        compile_file_mtime = get_mtime(self._compile_file)
        if compile_file_mtime > self.observed_info.get(self._compile_file, 0):
            self.sync_compile_file()
            return True

    def check_locking_compile_file(self):
        if self.locking_compile_file:
            mtime = get_mtime(self.compile_lock_path)
            if not mtime:
                pass
            elif time.time() - mtime < 180:
                return True  # still wait
            else:
                logger.warn("updating compile lock timeout! reset it")
                force_remove(self.compile_lock_path)

            self.locking_compile_file = False

    def log_paths_for_invalid_compile_file(self) -> list:
        """the build logs newer than the compile file, oldest first; [] when it is up to date.

        LSAI fork: every build the developer ran is merged (the app, its tests, a local package's tests),
        not only the newest one, so a later build of the app alone does not drop the tests from the index."""

        # TODO: xcodebuild not generate log until specify -resultBundlePath #

        build_root = self.config.build_root
        assert isinstance(build_root, str)

        from xcactivitylog import metapath_from_buildroot, logpaths

        def update_check_time(path):
            """return mtime if updated, else None"""
            mtime = get_mtime(path)
            if mtime > self.observed_info.get(path, 0):
                self.observed_info[path] = mtime
                return mtime
            return None

        compile_file_mtime = self.observed_info[self._compile_file]
        xcactivitylog_index = metapath_from_buildroot(build_root)
        if not (xcactivitylog_index_mtime := update_check_time(xcactivitylog_index)):
            return []
        if compile_file_mtime > xcactivitylog_index_mtime:
            return []

        return [p for p in logpaths(xcactivitylog_index, self.config.scheme) if get_mtime(p) > compile_file_mtime]

    def trigger_parse(self, xcpath):
        # FIXME: ensure index_store_path from buildServer.json consistent with parsed .compile file..
        import xclog_parser

        xclog_parser.hooks_echo_to_log = True

        from xclog_parser import parse, OutputLockedError

        try:
            cmd = ["xclog_parser", "-al", xcpath, "-o", self._compile_file]
            if self.config.skip_validate_bin:
                cmd.append("--skip-validate-bin")
            logger.info(f"parse log at {xcpath}")
            parse(cmd)
            self.handle_compile_file_change()
        except OutputLockedError:
            self.locking_compile_file = True

    def sync_compile_file(self, before=None):
        """update to newest compile info to main thread

        NOTE: called by observe thread, and block main thread,
        to ensure when exit, all thread is in the runloop and no middle state.
        """
        with mainlock:
            if before:
                before()
            self.reinit_compile_info()

            self.notify_target_changes()

    def notify_target_changes(self):
        # TODO: increment diff change and notify #
        if self.new_version:
            send({
                "jsonrpc": "2.0",
                "method": "buildTarget/didChange",
                "params": {
                    "changes": None
                },
            })
        else:
            for v in self.observed_uri:
                self.notify_option_changed(v)


# valid after build_initialize. access before should throw
shared_state: State = None  # type: ignore


def server_api():
    """nest def is api, return by locals()"""

    def build_initialize(message):
        rootUri = message["params"]["rootUri"]
        root_path = os.path.abspath(os.path.expanduser(uri2filepath(rootUri)))
        cache_path = os.path.join(
            os.path.expanduser("~/Library/Caches/xcode-build-server"),
            root_path.replace("/", "-"),
        )
        bspVersion = message["params"]["bspVersion"]

        state = State(
            root_path, cache_path, new_version=version_compare("2.2.0", bspVersion) <= 0
        )
        global shared_state
        if shared_state:
            logger.warn("already initialized!!")
        else:
            shared_state = state

        # FIXME: currently indexStorePath can't change dynamicly. have to restart server.
        # though it rarely changes.. need to watch sourcekit-lsp implementation
        indexStorePath = state.indexStorePath
        if not indexStorePath:
            indexStorePath = f"{cache_path}/indexStorePath"

        indexStorePathHash = hashlib.md5(indexStorePath.encode("utf-8")).hexdigest()
        # database should unique to a indexStorePath
        indexDatabasePath = f"{cache_path}/indexDatabasePath-{indexStorePathHash}"

        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "displayName": "xcode build server",
                "version": VERSION,
                "bspVersion": "2.2.0",
                "rootUri": rootUri,
                "capabilities": {
                    "languageIds": ["c", "cpp", "objective-c", "objective-cpp", "swift"]
                },
                "data": {
                    "indexDatabasePath": indexDatabasePath,
                    "indexStorePath": indexStorePath,
                    "sourceKitOptionsProvider": True,
                    # LSAI fork: sourcekit-lsp background-indexes edits only with prepare support
                    "prepareProvider": True,
                    # LSAI fork: each Swift source carries its index unit output path (buildTarget/sources)
                    "outputPathsProvider": True,
                },
                "dataKind": "sourceKit",
            },
        }

    def build_initialized(message):
        shared_state.start_observe_changes()

    def workspace_waitForBuildSystemUpdates(message):
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {},
        }

    def workspace_buildTargets(message):
        # LSAI fork: one target per Swift module, depending on the modules it imports, then the fallback target
        # whose directories hold every other file
        modules = shared_state.target_modules() if shared_state else []
        deps = shared_state.module_dependencies() if modules else {}
        targets = [
            {
                "id": {"uri": lsai_modules.MODULE_TARGET_SCHEME + m.name},
                "displayName": m.name,
                "tags": [],
                "capabilities": {},
                "languageIds": ["swift"],
                "dependencies": [{"uri": lsai_modules.MODULE_TARGET_SCHEME + d} for d in deps.get(m.name, [])],
            }
            for m in modules
        ]
        targets.append(
            {
                "id": dict(lsai_modules.FALLBACK_TARGET),
                "displayName": "BuildServer",
                "tags": ["test"],
                "capabilities": {},
                "languageIds": ["c", "cpp", "objective-c", "objective-cpp", "swift"],
                "dependencies": [],
            }
        )
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"targets": targets}}

    def buildTarget_sources(message):
        params: dict = message["params"]
        modules = {m.name: m for m in shared_state.target_modules()}
        items = []
        for target in params["targets"]:
            uri = target["uri"]
            if uri.startswith(lsai_modules.MODULE_TARGET_SCHEME):
                module = modules.get(uri[len(lsai_modules.MODULE_TARGET_SCHEME):])
                sources = [shared_state.source_item(f) for f in module.sources] if module else []
            elif uri == lsai_modules.FALLBACK_TARGET["uri"]:
                sources = [
                    {
                        "uri": Path(shared_state.root_path).as_uri(),
                        "kind": 2,  # 1: file, 2: directory
                        "generated": False,
                    },
                ]
                if shared_state.config.build_root:
                    sources.append(
                        {
                            "uri": Path(
                                shared_state.config.build_root,
                                "SourcePackages",
                                "checkouts",
                            ).as_uri(),
                            "kind": 2,  # 1: file, 2: directory
                            "generated": False,
                        }
                    )
                # LSAI fork: a compiled Swift file no module lists is still a file-kind source, so edits are re-indexed
                in_modules = {f for m in modules.values() for f in m.sources}
                sources.extend(shared_state.source_item(f) for f in shared_state.compiled_swift_files() if f not in in_modules)
            else:
                continue
            items.append({"target": target, "sources": sources})

        return {"jsonrpc": "2.0", "id": message["id"], "result": {"items": items}}

    def buildTarget_prepare(message):
        # LSAI fork: re-emit the stale modules the targets import into this server's cache; the developer's
        # build is never run and DerivedData never written
        scheme = lsai_modules.MODULE_TARGET_SCHEME
        names = [t["uri"][len(scheme):] for t in message["params"].get("targets", []) if t["uri"].startswith(scheme)]
        if names:
            lsai_modules.prepare(names, shared_state.all_modules(), shared_state.modules_dir)
        return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

    def textDocument_registerForChanges(message):
        # empty response, ensure response before notification
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})

        # is file save trigger a change and update index?
        action = message["params"]["action"]
        uri = message["params"]["uri"]
        if action == "register":
            shared_state.register_uri(uri)
        elif action == "unregister":
            shared_state.unregister_uri(uri)

    def textDocument_sourceKitOptions(message):
        uri = message["params"]["textDocument"]["uri"]
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": shared_state.sourceKitOptions(uri),
        }

    # TODO: outputPaths, no spec? #
    def build_shutdown(message):
        shared_state.shutdown()
        return {"jsonrpc": "2.0", "id": message["id"], "result": None}

    def build_exit(message):
        sys.exit()

    return locals()


dispatch = server_api()
mainlock = Lock()


def serve():
    logger.info("Xcode Build Server Startup")
    while True:
        line = sys.stdin.readline()
        if len(line) == 0:
            break

        assert line.startswith("Content-Length:")
        length = int(line[len("Content-Length:") :])
        sys.stdin.readline()
        raw = sys.stdin.read(length)
        message = json.loads(raw)
        logger.debug("Req --> " + raw)

        def default_response():
            if "id" in message:
                return {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": 123,
                        "message": "unhandled method {}".format(message["method"]),
                    },
                }

        with mainlock:
            response = None
            handler = dispatch.get(message["method"].replace("/", "_"))
            if handler:
                try:
                    response = handler(message)
                except Exception as e:
                    logger.exception(f"handle message error: {e}")
                    response = default_response()
            else:
                # ignore other notifications
                response = default_response()
            if response:
                send(response)
