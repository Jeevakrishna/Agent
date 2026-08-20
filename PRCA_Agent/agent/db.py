"""Convenience launcher — re-exports agent.db package contents.

When you do `from agent.db import search_rules` inside the installed package,
you get the real module at agent/agent/db.py. This wrapper file exists so
scripts sitting next to this directory can also `import db` without having
the package on sys.path.
"""

import sys
from pathlib import Path

_PRCA_AGENT_ROOT = Path(__file__).resolve().parent
if str(_PRCA_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PRCA_AGENT_ROOT))

from agent.db import (  # noqa: F401  -- re-export
    _to_pgvector_literal,
    get_connection,
    search_rules,
)
