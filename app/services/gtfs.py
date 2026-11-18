from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings

# Tope de puntos por polilínea en el payload SSE (Leaflet se ve bien con ~80–120).
_MAX_SHAPE_POINTS = 100


def _perpendicular_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    if start == end:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    x, y = point
    x1, y1 = start
    x2, y2 = end
    num = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1)
    den = math.hypot(y2 - y1, x2 - x1) or 1.0
    return num / den


def simplify_latlng(
    points: list[list[float]],
    *,
    max_points: int = _MAX_SHAPE_POINTS,
    tolerance: float = 0.00012,
) -> list[list[float]]:
    """Douglas-Peucker sobre [lat, lon]. Reduce peso del SSE sin destrozar el trazado."""
    if len(points) <= max_points:
        return points
    pts = [(float(p[0]), float(p[1])) for p in points]

    def _dp(seq: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
        if len(seq) < 3:
            return seq
        start, end = seq[0], seq[-1]
        idx = max(range(1, len(seq) - 1), key=lambda i: _perpendicular_distance(seq[i], start, end))
        if _perpendicular_distance(seq[idx], start, end) > eps:
            left = _dp(seq[: idx + 1], eps)
            right = _dp(seq[idx:], eps)
            return left[:-1] + right
        return [start, end]

    simplified = _dp(pts, tolerance)
    # Si aún hay demasiados, submuestrear uniformemente.
    if len(simplified) > max_points:
        step = (len(simplified) - 1) / (max_points - 1)
        simplified = [simplified[int(i * step)] for i in range(max_points - 1)] + [simplified[-1]]
    return [[lat, lon] for lat, lon in simplified]


def _coords_from_geojson(geom: Any) -> list[list[float]]:
    """GeoJSON [lon, lat] → Leaflet [lat, lon]."""
    if not geom:
        return []
    if isinstance(geom, str):
        try:
            geom = json.loads(geom)
        except json.JSONDecodeError:
            return []
    if not isinstance(geom, dict):
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        return [[float(lat), float(lon)] for lon, lat in coords]
    if gtype == "MultiLineString":
        out: list[list[float]] = []
        for line in coords:
            out.extend([[float(lat), float(lon)] for lon, lat in line])
        return out
    return []


class GtfsService:
    """Acceso GTFS: Postgres (prod) o feed local (pruebas)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pool: ConnectionPool | None = None
        self._local: _LocalFeed | None = None

    def connect(self) -> None:
        if self.settings.database_url:
            self._pool = ConnectionPool(
                conninfo=self.settings.database_url,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row, "connect_timeout": 10},
                open=True,
            )
            return
        local_dir = (self.settings.gtfs_local_dir or "").strip()
        if local_dir:
            self._local = _LocalFeed(Path(local_dir))

    def close(self) -> None:
        if self._pool:
            self._pool.close()
            self._pool = None
        self._local = None

    @property
    def available(self) -> bool:
        return self._pool is not None or self._local is not None

    def search_stops(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        if self._local:
            return self._local.search_stops(q, limit)
        if not self._pool:
            return []
        pattern = f"%{q.strip()}%"
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT stop_id, stop_name, stop_lat, stop_lon
                FROM stops
                WHERE stop_name ILIKE %s OR stop_id ILIKE %s
                ORDER BY stop_name
                LIMIT %s
                """,
                (pattern, pattern, limit),
            ).fetchall()
        return list(rows)

    def get_stop(self, stop_id: str) -> dict[str, Any] | None:
        if self._local:
            return self._local.get_stop(stop_id)
        if not self._pool:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
                       sd.district_name
                FROM stops s
                LEFT JOIN stop_districts sd ON sd.stop_id = s.stop_id
                WHERE s.stop_id = %s
                """,
                (stop_id,),
            ).fetchone()
        return dict(row) if row else None

    def routes_for_stop(self, stop_id: str) -> list[dict[str, Any]]:
        if self._local:
            return self._local.routes_for_stop(stop_id)
        if not self._pool:
            return []
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    r.route_id,
                    r.route_short_name,
                    r.route_long_name,
                    COALESCE(NULLIF(r.route_color, ''), ra.colour, '0EA5E9') AS route_color,
                    ra.nombre_popular,
                    ra.desde,
                    ra.hasta,
                    ra.interval_min,
                    ra.concesionario,
                    sr.idx AS stop_sequence
                FROM stop_routes sr
                JOIN routes r ON r.route_id = sr.route_id
                LEFT JOIN route_attributes ra ON ra.route_id = r.route_id
                WHERE sr.stop_id = %s
                ORDER BY COALESCE(r.route_short_name, r.route_id)
                """,
                (stop_id,),
            ).fetchall()
            routes = [dict(r) for r in rows]
            for route in routes:
                route["shape"] = self._shape_for_route(conn, route["route_id"])
        return routes

    def _shape_for_route(self, conn: Any, route_id: str) -> list[list[float]]:
        # Preferir geometría ya lista (1 fila / ruta).
        row = conn.execute(
            """
            SELECT geometry
            FROM route_geometries
            WHERE route_id = %s
            LIMIT 1
            """,
            (route_id,),
        ).fetchone()
        if row and row.get("geometry") is not None:
            pts = _coords_from_geojson(row["geometry"])
            if pts:
                return simplify_latlng(pts)

        # Fallback GTFS shapes vía un trip de la ruta.
        pts_rows = conn.execute(
            """
            SELECT s.shape_pt_lat, s.shape_pt_lon
            FROM shapes s
            WHERE s.shape_id = (
                SELECT t.shape_id FROM trips t
                WHERE t.route_id = %s AND t.shape_id IS NOT NULL
                LIMIT 1
            )
            ORDER BY s.shape_pt_sequence
            """,
            (route_id,),
        ).fetchall()
        if not pts_rows:
            return []
        pts = [[float(r["shape_pt_lat"]), float(r["shape_pt_lon"])] for r in pts_rows]
        return simplify_latlng(pts)


class _LocalFeed:
    """Feed GTFS + extras locales para pruebas sin Postgres."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.attrs_path = Path("/workspace/mig/bk/route_attributes.csv")
        self.districts_path = Path("/workspace/mig/bk/stop_districts.csv")
        self._stops = {
            r["stop_id"]: r
            for r in csv.DictReader((root / "stops.txt").open(encoding="utf-8"))
        }
        self._routes = {
            r["route_id"]: r
            for r in csv.DictReader((root / "routes.txt").open(encoding="utf-8"))
        }
        self._trips = list(csv.DictReader((root / "trips.txt").open(encoding="utf-8")))
        self._attrs: dict[str, dict[str, str]] = {}
        if self.attrs_path.is_file():
            self._attrs = {
                r["route_id"]: r
                for r in csv.DictReader(self.attrs_path.open(encoding="utf-8"))
            }
        self._districts: dict[str, str] = {}
        if self.districts_path.is_file():
            for r in csv.DictReader(self.districts_path.open(encoding="utf-8")):
                self._districts[r["stop_id"]] = r.get("district_name") or ""

        # stop_id → set(route_id)
        self._stop_routes: dict[str, set[str]] = {}
        trip_route = {t["trip_id"]: t["route_id"] for t in self._trips}
        with (root / "stop_times.txt").open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rid = trip_route.get(row["trip_id"])
                if rid:
                    self._stop_routes.setdefault(row["stop_id"], set()).add(rid)

        # shape_id → points [lat,lon]
        self._shapes: dict[str, list[list[float]]] = {}
        with (root / "shapes.txt").open(encoding="utf-8") as fh:
            buckets: dict[str, list[tuple[int, float, float]]] = {}
            for row in csv.DictReader(fh):
                buckets.setdefault(row["shape_id"], []).append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    )
                )
            for sid, pts in buckets.items():
                pts.sort(key=lambda x: x[0])
                self._shapes[sid] = [[lat, lon] for _, lat, lon in pts]

        self._route_shape_id = {t["route_id"]: t["shape_id"] for t in self._trips if t.get("shape_id")}

    def search_stops(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        ql = q.strip().lower()
        out = []
        for s in self._stops.values():
            if ql in s["stop_name"].lower() or ql in s["stop_id"].lower():
                out.append(
                    {
                        "stop_id": s["stop_id"],
                        "stop_name": s["stop_name"],
                        "stop_lat": float(s["stop_lat"]),
                        "stop_lon": float(s["stop_lon"]),
                    }
                )
            if len(out) >= limit:
                break
        return out

    def get_stop(self, stop_id: str) -> dict[str, Any] | None:
        s = self._stops.get(stop_id)
        if not s:
            return None
        return {
            "stop_id": s["stop_id"],
            "stop_name": s["stop_name"],
            "stop_lat": float(s["stop_lat"]),
            "stop_lon": float(s["stop_lon"]),
            "district_name": self._districts.get(stop_id) or None,
        }

    def routes_for_stop(self, stop_id: str) -> list[dict[str, Any]]:
        route_ids = sorted(self._stop_routes.get(stop_id, set()))
        out: list[dict[str, Any]] = []
        for rid in route_ids:
            r = self._routes.get(rid) or {"route_id": rid}
            a = self._attrs.get(rid) or {}
            color = (r.get("route_color") or a.get("colour") or "0EA5E9").lstrip("#")
            shape = self._shape_for_route(rid)
            out.append(
                {
                    "route_id": rid,
                    "route_short_name": r.get("route_short_name") or a.get("ref") or rid[-4:],
                    "route_long_name": r.get("route_long_name") or a.get("nombre_popular"),
                    "route_color": color,
                    "nombre_popular": a.get("nombre_popular"),
                    "desde": a.get("desde"),
                    "hasta": a.get("hasta"),
                    "interval_min": int(a["interval_min"]) if a.get("interval_min") else None,
                    "concesionario": a.get("concesionario"),
                    "stop_sequence": None,
                    "shape": shape,
                }
            )
        out.sort(key=lambda x: str(x.get("route_short_name") or x["route_id"]))
        return out

    def _shape_for_route(self, route_id: str) -> list[list[float]]:
        # Preferir geojson extra (equivalente a route_geometries).
        geo = self.root / "extra" / "routes" / f"{route_id}.geojson"
        if geo.is_file():
            try:
                data = json.loads(geo.read_text(encoding="utf-8"))
                for ft in data.get("features") or []:
                    if (ft.get("properties") or {}).get("type") == "route":
                        pts = _coords_from_geojson(ft.get("geometry"))
                        if pts:
                            return simplify_latlng(pts)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        sid = self._route_shape_id.get(route_id)
        if sid and sid in self._shapes:
            return simplify_latlng(self._shapes[sid])
        return []
