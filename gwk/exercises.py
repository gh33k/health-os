"""Garmin exercise taxonomy: resolve friendly names to (category, exerciseName) keys.

The taxonomy is Garmin's own list (connect.garmin.com/web-data/exercises/Exercises.json),
vendored as exercises.json. The watch only shows animations and counts reps when the
exact keys are used.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources


class ExerciseError(Exception):
    pass


@lru_cache(maxsize=1)
def taxonomy() -> dict[str, list[str]]:
    """Return {category: [exercise keys]}."""
    raw = json.loads(
        resources.files("gwk").joinpath("exercises.json").read_text()
    )
    return {cat: list(v["exercises"]) for cat, v in raw["categories"].items()}


def _normalize(name: str) -> str:
    return name.strip().upper().replace("-", "_").replace(" ", "_")


def resolve(name: str) -> tuple[str, str]:
    """Resolve a name like 'pull-up' or 'ROW/INVERTED_ROW' to (category, exerciseName)."""
    tax = taxonomy()
    if "/" in name:
        cat, ex = (_normalize(p) for p in name.split("/", 1))
        if cat not in tax:
            raise ExerciseError(f"Unknown exercise category: {cat!r}")
        if ex not in tax[cat]:
            raise ExerciseError(f"Unknown exercise {ex!r} in category {cat!r}")
        return cat, ex

    ex = _normalize(name)
    matches = [(cat, ex) for cat, exs in tax.items() if ex in exs]
    if not matches:
        suggestions = search(name)[:5]
        hint = f" Did you mean: {', '.join(s[1] for s in suggestions)}?" if suggestions else ""
        raise ExerciseError(f"Unknown exercise: {name!r}.{hint}")
    if len(matches) > 1:
        opts = ", ".join(f"{c}/{e}" for c, e in matches)
        raise ExerciseError(
            f"Exercise {name!r} exists in several categories; use CATEGORY/NAME: {opts}"
        )
    return matches[0]


def search(term: str) -> list[tuple[str, str]]:
    """Substring search across all exercise keys; returns (category, exerciseName) pairs."""
    needle = _normalize(term)
    return [
        (cat, ex)
        for cat, exs in sorted(taxonomy().items())
        for ex in sorted(exs)
        if needle in ex or needle in cat
    ]
