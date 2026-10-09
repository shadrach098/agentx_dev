"""Error messages for optional packages (the ``pip install agentx-dev[extra]`` ones).

A failed ``import`` can mean two different things, and the fix is different:

- the package is not installed: install the extra, into the Python that is running;
- the package is installed but importing it failed (a file of the user's named like a library,
  such as ``google.py``, hiding the real one; a version conflict; a missing system library):
  reinstalling does nothing, so show the real error.

Saying "requires X, install X" for both sent a user hunting for a package they already had."""

import sys
from typing import Tuple


def _is_missing(error: BaseException, modules: Tuple[str, ...]) -> bool:
    """True when ``error`` says one of ``modules`` itself is not installed."""
    if not isinstance(error, ModuleNotFoundError):
        return False
    return (getattr(error, "name", None) or "").split(".")[0] in modules


def optional_import_error(feature: str, error: BaseException, *, modules: Tuple[str, ...],
                          pip: str, extra: str = "") -> ImportError:
    """The ``ImportError`` to raise (``from error``) when ``feature``'s package fails to import.

    ``modules`` are the top-level module names whose absence means "not installed", ``pip`` is
    the requirement to install, ``extra`` the agentx-dev extra that includes it."""
    if _is_missing(error, modules):
        cmd = f'python -m pip install "agentx-dev[{extra}]"' if extra else f"python -m pip install {pip}"
        also = f" (or: python -m pip install {pip})" if extra else ""
        return ImportError(
            f"{feature} requires the {pip} package, which is not installed. "
            f"Install it with: {cmd}{also}. "
            f"This program is running on {sys.executable}; install into that interpreter."
        )
    return ImportError(
        f"{feature} could not import {modules[0]}: {type(error).__name__}: {error}. "
        f"If {pip} is already installed, installing it again will not help: the error is inside "
        f"its imports. Common causes: a file in your project folder named like a library "
        f"(for example google.py, numpy.py or typing.py) hides the real one; a version conflict "
        f"with another package. Run `python -c \"import {modules[0]}\"` for the full traceback."
    )
