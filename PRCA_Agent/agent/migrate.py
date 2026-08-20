"""Launcher script for `python agent/migrate.py` (from repo root).

Delegates to the installed package module agent.migrate so there is a
single source of truth for the migration logic.
"""

import sys
from pathlib import Path

_PRCA_AGENT_ROOT = Path(__file__).resolve().parent
if str(_PRCA_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PRCA_AGENT_ROOT))

from agent.migrate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
