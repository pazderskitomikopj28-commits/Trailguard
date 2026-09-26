"""Route asset resolution: from a ``route_id`` to a GPX file on disk.

TrailGuard never asks the user where a route lives. A ``route_id`` is a logical
name (``scn_route_sample``) or a repo-root-relative asset reference
(``replay/routes/scn_route_sample.gpx``), and this module turns it into a
:class:`~trailguard.routes.geometry.RouteGeometry` by trying, in order:

1. the reference itself, if it points at an existing file (absolute, or relative
   to the repository root, or relative to one of the configured search paths);
2. ``<search path>/<route_id>.gpx`` for every configured search path, which is how
   the frozen replay asset ``scn_route_sample`` resolves.

Nothing is invented when nothing matches: the error lists the candidates that were
tried, because "unknown route" is missing data (spec 10.3) and a silent
fallback -- an empty polyline, a straight line between two arbitrary points --
would hand the safety core a geometry no one chose.
"""

from __future__ import annotations

from pathlib import Path

from .geometry import RouteAssetError, RouteGeometry, load_route_geometry

__all__ = ["REPO_ROOT", "ROUTE_SEARCH_PATHS", "known_route_ids", "resolve_route_geometry"]

#: Repository root, derived from this file (``trailguard/routes/registry.py``).
#: Derived rather than configured so the package works from any working directory.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Where a bare ``route_id`` is looked up. Replay assets live here (spec 17, 24).
ROUTE_SEARCH_PATHS: tuple[str, ...] = ("replay/routes",)


def _candidates(route_id: str, search_paths: tuple[str, ...]) -> list[Path]:
    """Every path a ``route_id`` could refer to, in the order they are tried."""
    raw = Path(route_id)
    candidates: list[Path] = [raw]
    if not raw.is_absolute():
        candidates.append(REPO_ROOT / raw)
    if raw.suffix != ".gpx":
        for search_path in search_paths:
            candidates.append(REPO_ROOT / search_path / f"{route_id}.gpx")
    return candidates


def resolve_route_geometry(
    route_id: str,
    *,
    search_paths: tuple[str, ...] = ROUTE_SEARCH_PATHS,
) -> RouteGeometry:
    """Resolve ``route_id`` to a parsed geometry.

    Args:
        route_id: Logical route name or repo-root-relative GPX reference.
        search_paths: Directories searched for ``<route_id>.gpx``.

    Returns:
        The parsed :class:`~trailguard.routes.geometry.RouteGeometry`, recorded
        under ``route_id``.

    Raises:
        RouteAssetError: no candidate file exists, listing what was tried.
    """
    if not route_id or not route_id.strip():
        raise RouteAssetError("route_id must be a non-empty route name or GPX reference")

    tried: list[Path] = []
    for candidate in _candidates(route_id.strip(), tuple(search_paths)):
        if candidate in tried:
            continue
        tried.append(candidate)
        if candidate.is_file():
            return load_route_geometry(candidate, route_id=route_id.strip())

    listing = "\n  ".join(str(path) for path in tried)
    known = ", ".join(known_route_ids()) or "none installed"
    raise RouteAssetError(
        f"unknown route {route_id!r}: no GPX asset found at any of\n  {listing}\n"
        f"known route ids: {known}"
    )


def known_route_ids(search_paths: tuple[str, ...] = ROUTE_SEARCH_PATHS) -> list[str]:
    """Route ids discoverable in the configured search paths (sorted)."""
    found: set[str] = set()
    for search_path in search_paths:
        directory = REPO_ROOT / search_path
        if not directory.is_dir():
            continue
        found.update(path.stem for path in sorted(directory.glob("*.gpx")))
    return sorted(found)
