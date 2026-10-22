"""`python -m knowledgestore`: the console script, under a named interpreter.

A store that drives graphify as `python -m graphify` from its own environment
needs the same for this library, so the version it runs is the one its lock
installed rather than whichever `knowledgestore` is first on PATH.
"""

import sys

from .cli import main

sys.exit(main())
