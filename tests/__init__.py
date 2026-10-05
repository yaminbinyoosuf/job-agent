"""Test package.

Structured logging is configured by the agent at runtime; during tests the
pipeline's expected warnings (an injected failing model, for example) would
otherwise be printed by logging's last-resort handler and clutter the output.
"""

import logging

logging.getLogger("taskagent").addHandler(logging.NullHandler())
logging.getLogger("taskagent").propagate = False
logging.getLogger("taskagent").setLevel(logging.CRITICAL)
