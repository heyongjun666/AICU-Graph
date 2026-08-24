"""Pytest configuration for cc_0 workspace — runtime is self-contained within cc_0."""

import os
import sys

# Ensure cc_0 root is on sys.path so imports like "from runtime.models" work
_cc0_root = os.path.abspath(os.path.dirname(__file__))
if _cc0_root not in sys.path:
    sys.path.insert(0, _cc0_root)
