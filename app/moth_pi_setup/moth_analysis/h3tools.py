from __future__ import annotations

from typing import Iterable

try:
    import h3  # type: ignore
except Exception:  # pragma: no cover
    h3 = None


def latlon_to_cell(lat: float, lon: float, res: int) -> str | None:
    if h3 is None:
        return None
    # h3-py v4 uses latlng_to_cell; v3 used geo_to_h3.
    if hasattr(h3, "latlng_to_cell"):
        return h3.latlng_to_cell(lat, lon, res)
    if hasattr(h3, "geo_to_h3"):
        return h3.geo_to_h3(lat, lon, res)
    return None


def cell_to_boundary_lnglat(cell: str) -> list[list[float]]:
    if h3 is None:
        return []
    if hasattr(h3, "cell_to_boundary"):
        boundary = h3.cell_to_boundary(cell)
    else:
        boundary = h3.h3_to_geo_boundary(cell)
    # H3 returns lat/lon pairs. GeoJSON expects lon/lat.
    coords = [[float(lon), float(lat)] for lat, lon in boundary]
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords
