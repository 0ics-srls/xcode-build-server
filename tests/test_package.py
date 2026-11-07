"""LSAI fork release: 0ics/package.sh builds the pinned tarball LSAI's installer downloads."""
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import _forkroot

PACKAGE_SCRIPT = os.path.join(_forkroot.FORK_ROOT, "0ics", "package.sh")
VERSION_FILE = os.path.join(_forkroot.FORK_ROOT, "0ics", "VERSION")
TOP = "xcode-build-server"


def read_version():
    with open(VERSION_FILE) as f:
        return f.read().strip()


class PackageTests(unittest.TestCase):
    """package.sh runs once for the class; each test reads the same output."""

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp()
        subprocess.run(["bash", PACKAGE_SCRIPT, cls.out], check=True)
        cls.version = read_version()
        cls.tarball_name = "xcode-build-server-%s.tar.gz" % cls.version
        cls.tarball = os.path.join(cls.out, cls.tarball_name)
        cls.checksum = cls.tarball + ".sha256"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out)

    def members(self):
        with tarfile.open(self.tarball) as tar:
            return {m.name: m for m in tar.getmembers()}

    def test_package_creates_tarball_and_checksum_named_after_version(self):
        self.assertEqual(sorted(os.listdir(self.out)), [self.tarball_name, self.tarball_name + ".sha256"])

    def test_package_archive_has_top_folder_with_runtime_files_and_no_tests_or_github(self):
        names = self.members().keys()

        for required in ["xcode-build-server", "server.py", "lsai_modules.py", "misc.py", "compile_database.py", "xcactivitylog.py",
                         "xclog_parser.py", "config", "config/config.py", "xcode", "LICENSE", "0ics/VERSION"]:
            self.assertIn(TOP + "/" + required, names)
        self.assertTrue(all(n == TOP or n.startswith(TOP + "/") for n in names), sorted(names))
        self.assertFalse(any(n.startswith(TOP + "/tests") or n.startswith(TOP + "/.github") for n in names))

    def test_package_launcher_keeps_executable_bit(self):
        launcher = self.members()[TOP + "/xcode-build-server"]

        self.assertTrue(launcher.mode & 0o111, oct(launcher.mode))

    def test_package_checksum_line_is_sha256_two_spaces_and_name(self):
        with open(self.tarball, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()

        with open(self.checksum) as f:
            line = f.read()

        self.assertEqual(line, "%s  %s\n" % (digest, self.tarball_name))


if __name__ == "__main__":
    unittest.main()
