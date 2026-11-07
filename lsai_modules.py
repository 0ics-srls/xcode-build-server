"""LSAI fork: the Swift modules of the compile file, and their preparation for indexing.

Each module the developer's Xcode build compiled is a build target, depending on the other modules its
sources import. sourcekit-lsp re-prepares the targets that depend on an edited file before it re-indexes
their files; preparing a target re-emits the modules it imports whose sources are newer than the module
the build left (make's rule), into a directory of this server's cache that every Swift file searches
first. The developer's DerivedData is never written, and the build itself is never run: only the
module interface, without function bodies, the way SwiftPM prepares a package for indexing.

Without it, after an edit of a module's API (a rename) the files that import the module are re-indexed
against the old module and their references are lost.

A local package the Xcode build compiles has test targets the app's scheme never builds (`swift test`
builds them, with its own build system). Each is a module too, indexed with the flags of its package's
library as the Xcode build compiled it: same SDK, platform and test frameworks.
"""
import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("lsai_modules")

MODULE_TARGET_SCHEME = "module://"
# sourcekit-lsp uses a file's target with the smallest uri: a module target, never this one
FALLBACK_TARGET = {"uri": "xcode://workspace"}
MODULES_DIR_NAME = "lsai-modules"

# outputs of the developer's build, and the build modes an emit of the module alone does not use
_DROP_WITH_VALUE = {"-o", "-output-file-map", "-index-store-path", "-module-cache-path", "-clang-scanner-module-cache-path",
                    "-sdk-module-cache-path", "-clang-build-session-file", "-const-gather-protocols-list",
                    "-dependency-scan-serialize-diagnostics-path", "-serialize-diagnostics-path", "-num-threads"}
_DROP = {"-c", "-g", "-incremental", "-enable-batch-mode", "-save-temps", "-explicit-module-build", "-serialize-diagnostics",
         "-validate-clang-modules-once", "-experimental-emit-module-separately", "-parseable-output",
         "-use-frontend-parseable-output"}
# the next argument goes to another tool: kept as it is
_PASS_THROUGH = {"-Xcc", "-Xfrontend", "-Xlinker", "-Xclang-linker", "-Xllvm"}
# a library's outputs, which its package's test module does not have
_LIBRARY_OUTPUTS = {"-emit-module-path", "-output-file-map", "-emit-objc-header-path", "-const-gather-protocols-list",
                    "-index-store-path", "-dependency-scan-serialize-diagnostics-path", "-filelist"}
_EMITTED_SUFFIXES = (".swiftmodule", ".swiftdoc", ".swiftsourceinfo", ".abi.json")
_IMPORT = re.compile(r"^[ \t]*(?:@[\w()]+[ \t]+)*import[ \t]+(?:(?:typealias|struct|class|enum|protocol|let|var|func)[ \t]+)?(\w+)",
                     re.MULTILINE)


@dataclass
class SwiftModule:
    name: str
    command: List[str]  # the swiftc invocation, executable first
    directory: str
    sources: List[str]
    built_module: str  # the module the developer's build emitted (-emit-module-path), "" when there is none
    group: str = ""  # its platform (-target): the modules it imports are those of the same group
    primary: bool = True  # the entry its files get their flags from (a module is compiled once per architecture)
    package_test: bool = False  # a local package's test target, indexed with its library's flags


def group_dir(out_root, group):
    """Where the modules of a group are re-emitted, and searched first by its files."""
    return os.path.join(out_root, re.sub(r"[^\w.-]", "_", group) or "default")


def _value(args, flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def _file_list(path, store):
    cache = store.setdefault("lsai_file_lists", {})
    if path not in cache:
        try:
            with open(path) as f:
                cache[path] = [line.strip() for line in f if line.strip()]
        except OSError as ex:
            logger.warning("file list unreadable %s: %s", path, ex)
            cache[path] = []
    return cache[path]


def swift_modules(compile_file, store) -> List[SwiftModule]:
    """The Swift modules of the compile file, one per module and platform, in first-seen order; a later entry of
    the same module and platform replaces the earlier one. The primary one of a module is its last entry, the one
    its files get their flags from."""
    try:
        with open(compile_file) as f:
            entries = json.load(f)
    except (OSError, ValueError) as ex:
        logger.warning("compile file unreadable %s: %s", compile_file, ex)
        return []
    modules: Dict[tuple, SwiftModule] = {}
    last: Dict[str, tuple] = {}
    for entry in entries:
        args = shlex.split((entry.get("command") or "").replace("\\=", "="))
        if not args or not os.path.basename(args[0]).startswith("swift") or "-module-name" not in args:
            continue
        name = entry.get("module_name") or _value(args, "-module-name")
        sources = list(entry.get("files") or [])
        for list_path in entry.get("fileLists") or []:
            sources.extend(_file_list(list_path, store))
        sources = list(dict.fromkeys(s for s in sources if s.endswith(".swift")))
        key = (name, _value(args, "-target"))
        modules[key] = SwiftModule(name, args, entry.get("directory") or "", sources, _value(args, "-emit-module-path"), key[1])
        last[name] = key
    for key, module in modules.items():
        module.primary = last[module.name] == key
    return list(modules.values())


def _package_root(module: SwiftModule):
    """The package a module of the Xcode build comes from: its sources in <package>/Sources/<name>/ beside a
    Package.swift; None for a module of the Xcode project."""
    for source in module.sources:
        parts = source.split(os.sep)
        for i in range(len(parts) - 2, 0, -1):
            if parts[i] == "Sources" and parts[i + 1] == module.name:
                root = os.sep.join(parts[:i])
                if os.path.isfile(os.path.join(root, "Package.swift")):
                    return root
                break
    return None


def _swift_files(directory):
    found = []
    for path, subdirs, files in os.walk(directory):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        found.extend(os.path.join(path, f) for f in sorted(files) if f.endswith(".swift"))
    return found


def _test_command(library: SwiftModule, name, sources):
    """The library's compile command made the compile of its package's test target: its name, its sources, none
    of the library's outputs."""
    args = [library.command[0]]
    rest = iter(library.command[1:])
    for arg in rest:
        if arg in _PASS_THROUGH:
            args += [arg, next(rest, "")]
        elif arg == "-module-name":
            next(rest, None)
            args += ["-module-name", name]
        elif arg in _LIBRARY_OUTPUTS:
            next(rest, None)
        elif arg in ("-emit-module", "-emit-objc-header") or arg.startswith("@") or arg.endswith(".swift"):
            continue  # the library's own module and sources
        else:
            args.append(arg)
    return args + sources


def package_test_modules(modules: List[SwiftModule]) -> List[SwiftModule]:
    """The test targets of the local packages the Xcode build compiles: each <package>/Tests/<name>/ with Swift
    files and no module of that name in the build, one per platform of its library, with the library's flags."""
    names = {m.name for m in modules}
    result, seen = [], set()
    for library in modules:
        package = _package_root(library)
        tests_dir = os.path.join(package, "Tests") if package else None
        if not tests_dir or not os.path.isdir(tests_dir):
            continue
        for name in sorted(os.listdir(tests_dir)):
            sources = _swift_files(os.path.join(tests_dir, name))
            if name in names or not sources or (name, library.group) in seen:
                continue
            seen.add((name, library.group))
            result.append(SwiftModule(name, _test_command(library, name, sources), library.directory, sources, "",
                                      library.group, library.primary, package_test=True))
    return result


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def module_dependencies(modules: List[SwiftModule]) -> Dict[str, List[str]]:
    """For each module, the other modules of the list its sources import, in first-import order."""
    names = {m.name for m in modules}
    result = {}
    for m in modules:
        found = []
        for path in m.sources:
            try:
                with open(path, errors="replace") as f:
                    text = _strip_comments(f.read())
            except OSError:
                continue
            for imported in _IMPORT.findall(text):
                if imported in names and imported != m.name and imported not in found:
                    found.append(imported)
        result[m.name] = found
    return result


def emit_module_command(module: SwiftModule, out_dir) -> List[str]:
    """The module's swiftc invocation turned into an emit of the module alone, into out_dir: the modules already
    re-emitted there are searched first, nothing of the developer's build is written."""
    args = [module.command[0], "-I", out_dir]
    rest = iter(module.command[1:])
    for arg in rest:
        if arg in _PASS_THROUGH:
            args += [arg, next(rest, "")]
        elif arg in _DROP_WITH_VALUE or (arg.startswith("-emit-") and arg.endswith("-path")):
            next(rest, None)
        elif arg in _DROP or arg.startswith("-emit-") or re.fullmatch(r"-j\d+", arg):
            continue
        else:
            args.append(arg)
    return args + ["-emit-module", "-emit-module-path", os.path.join(out_dir, module.name + ".swiftmodule"),
                   "-module-cache-path", os.path.join(out_dir, "ModuleCache"),
                   "-Xfrontend", "-experimental-skip-all-function-bodies",
                   "-Xfrontend", "-experimental-allow-module-with-compiler-errors"]


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


def _remove_emitted(out_dir, name):
    for suffix in _EMITTED_SUFFIXES:
        try:
            os.remove(os.path.join(out_dir, name + suffix))
        except OSError:
            pass


def modules_to_emit(target_names, modules: List[SwiftModule], deps: Dict[str, List[str]], out_dir) -> List[SwiftModule]:
    """The modules the targets import, transitively, that are older than their sources or than a module they import,
    in build order. A re-emitted module older than a newer developer build is removed: the build is the newer one.
    The modules are those of one group, a name once."""
    by_name = {m.name: m for m in modules}
    order, visiting = [], set()

    def visit(name):
        if name in visiting or name not in by_name:
            return
        visiting.add(name)
        for dep in deps.get(name, []):
            visit(dep)
        order.append(name)

    for target in target_names:
        for dep in deps.get(target, []):
            visit(dep)

    served, result = {}, []
    for name in order:
        module = by_name[name]
        built = _mtime(module.built_module) if module.built_module else 0
        emitted = _mtime(os.path.join(out_dir, name + ".swiftmodule"))
        if emitted and emitted <= built:
            _remove_emitted(out_dir, name)
            emitted = 0
        served[name] = max(built, emitted)
        newest_input = max([_mtime(s) for s in module.sources] + [served.get(d, 0) for d in deps.get(name, [])] + [0])
        if newest_input > served[name]:
            result.append(module)
            served[name] = float("inf")  # re-emitted now: what imports it follows
    return result


def _same_platform(triple, other):
    """Two triples of one platform, another architecture: x86_64-apple-ios17.0-simulator, arm64-apple-ios17.0-simulator."""
    return triple.partition("-")[2] == other.partition("-")[2] and bool(triple.partition("-")[2])


def _retargeted(module: SwiftModule, triple) -> SwiftModule:
    args = list(module.command)
    args[args.index("-target") + 1] = triple
    return SwiftModule(module.name, args, module.directory, module.sources, module.built_module, triple, False,
                       module.package_test)


def group_members(modules: List[SwiftModule], group) -> List[SwiftModule]:
    """The modules of a group; a module the group has not, compiled for another architecture of the same platform, is
    taken retargeted (the build log of a module may keep one architecture only)."""
    members = [m for m in modules if m.group == group]
    names = {m.name for m in members}
    for module in modules:
        if module.name not in names and _same_platform(module.group, group):
            members.append(_retargeted(module, group))
            names.add(module.name)
    return members


def prepare(target_names, modules: List[SwiftModule], out_root, run: Optional[Callable] = None) -> List[str]:
    """Re-emits the stale modules the targets import, each target with the modules of its own platform, into that
    platform's directory; the names emitted. A failed emit is logged: the files that import that module are indexed
    against the module they had."""
    run = run or subprocess.run
    primary = {m.name: m.group for m in modules if m.primary}
    emitted = []
    for group in dict.fromkeys(primary[n] for n in target_names if n in primary):
        members = group_members(modules, group)
        names = [n for n in target_names if primary.get(n) == group]
        out_dir = group_dir(out_root, group)
        os.makedirs(out_dir, exist_ok=True)
        for module in modules_to_emit(names, members, module_dependencies(members), out_dir):
            result = run(emit_module_command(module, out_dir), cwd=module.directory or None, capture_output=True, text=True)
            if result.returncode == 0:
                emitted.append(module.name)
                logger.info("re-emitted module %s (%s) for indexing", module.name, group)
            else:
                logger.warning("emit of module %s (%s) failed (%s): %s", module.name, group, result.returncode,
                               (result.stderr or "")[-2000:])
    return emitted
