from __future__ import annotations

import sys
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
SOURCE_DIR = TESTS_DIR.parent

source_path = str(SOURCE_DIR)
if source_path not in sys.path:
	sys.path.insert(0, source_path)