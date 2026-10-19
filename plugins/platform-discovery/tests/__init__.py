"""Support MovieClaw repository CI discovery as well as standalone tests."""
import sys
from pathlib import Path

# The existing fixtures are imported by their module names in both invocation
# modes. Keep this test-only path local to the suite, never the plugin runtime.
_directory = str(Path(__file__).resolve().parent)
if _directory not in sys.path:
    sys.path.insert(0, _directory)
