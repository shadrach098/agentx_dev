"""The ``agentx`` command-line coding assistant."""

from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``agentx`` command (imported lazily so ``import agentx_dev.cli`` is cheap)."""
    from agentx_dev.cli.app import main as _main
    return _main(argv)
