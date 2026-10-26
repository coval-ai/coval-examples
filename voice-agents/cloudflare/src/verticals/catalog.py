"""Every deployed demo agent. Importing a module registers it."""

from verticals import (  # noqa: F401
    banking,
    healthcare,
    hospitality,
    ibm_cvs,
    insurance,
    nurse_bronancy,
    restaurant,
    retail,
    transport,
)
from verticals.registry import VERTICALS, get_vertical

__all__ = ["VERTICALS", "get_vertical"]
