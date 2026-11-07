"""Imported first by every test module: `discover -s tests` imports the modules as top-level
modules (tests/__init__.py is not run then), so the fork root is put on sys.path here too."""
import os
import sys

FORK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FORK_ROOT not in sys.path:
    sys.path.insert(0, FORK_ROOT)
