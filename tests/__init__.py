"""LSAI fork tests (stdlib unittest). Puts the fork root on sys.path so `import server` works."""
import os
import sys

FORK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FORK_ROOT not in sys.path:
    sys.path.insert(0, FORK_ROOT)
