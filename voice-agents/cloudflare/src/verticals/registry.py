from verticals.base import Vertical

VERTICALS: dict[str, Vertical] = {}


def register(vertical: Vertical) -> Vertical:
    VERTICALS[vertical.slug] = vertical
    return vertical


def get_vertical(slug: str) -> Vertical | None:
    return VERTICALS.get(slug)
