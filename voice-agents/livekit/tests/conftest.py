"""Make `coval_tracing` importable when running pytest from this directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
