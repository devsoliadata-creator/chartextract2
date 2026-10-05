"""Normalize authored line evidence without inventing paths between samples.

Trace segments carry their own identity and point order.  Older flat line-sample
rows do not reliably carry either, so they remain unconnected neutral samples.
"""

import math


TRACE_SOURCES = {"curve", "grid"}
TRACE_EVIDENCE_TYPES = {"line_sample"}
SUPPORTING_ROLES = {"supporting", "supporting_only", "python_support"}


def is_trace_point(point):
    """Return whether a row is line evidence rather than an experimental marker."""
    source = str(point.get("source") or "").strip().lower()
    evidence_type = str(point.get("evidence_type") or "").strip().lower()
    return source in TRACE_SOURCES or evidence_type in TRACE_EVIDENCE_TYPES


def trace_role(path):
    """Return a concise display role without changing artifact authority."""
    role = str(path.get("role") or "").strip().lower()
    return "supporting" if role in SUPPORTING_ROLES else "reviewed"


def _point_role(point):
    """Infer display role from explicit authority before generic source labels."""
    explicit = str(point.get("trace_role") or point.get("role") or "").strip().lower()
    if explicit:
        return "supporting" if explicit in SUPPORTING_ROLES else "reviewed"
    csv_row = point.get("csv_row") or {}
    if (str(point.get("evidence_type") or "").lower() == "line_sample"
            or str(csv_row.get("evidence_type") or "").lower() == "line_sample"):
        return "reviewed"
    source = str(point.get("source") or "").lower()
    return "supporting" if source in TRACE_SOURCES else "reviewed"


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ordered(points):
    """Use authored order; explicit order fields resolve shuffled serialization."""
    points = list(points or [])
    if points and all(_finite(point.get("order")) is not None for point in points):
        return [point for _, point in sorted(
            enumerate(points), key=lambda item: (_finite(item[1].get("order")), item[0])
        )]
    if points and all(_finite(point.get("trace_order")) is not None for point in points):
        return [point for _, point in sorted(
            enumerate(points), key=lambda item: (_finite(item[1].get("trace_order")), item[0])
        )]
    return points


def _path(trace, series_name=None, role=None, authority=None):
    points = trace.get("ordered_points")
    if points is None:
        points = trace.get("points", [])
    segment_id = trace.get("segment_id")
    if segment_id is None:
        segment_id = trace.get("trace_segment_id")
    return {
        "trace_id": trace.get("trace_id"),
        "segment_id": segment_id,
        "series_name": trace.get("series_name") or series_name,
        "color_hex": trace.get("color_hex"),
        "role": trace.get("role") or role,
        "authority": trace.get("authority") or authority,
        "branch": trace.get("branch"),
        "points": _ordered(points),
        "legacy_unsegmented": segment_id is None,
    }


def trace_paths(traces, series_name=None):
    """Return trace paths, retaining boundaries and never sorting by x.

    Supported inputs are a ``supporting_traces.json`` document, a list of
    segment records, or legacy point records.  A record with no segment ID is
    split into isolated points so historical samples cannot become a guessed
    polyline.
    """
    role = authority = None
    if isinstance(traces, dict):
        role = traces.get("role")
        authority = traces.get("authority")
        traces = traces.get("traces", [])
    paths = []
    for trace in traces or []:
        if not isinstance(trace, dict):
            continue
        path = _path(trace, series_name, role, authority)
        if path["segment_id"] is not None:
            paths.append(path)
            continue
        # Unidentified historical traces can be shown as neutral samples only.
        for point in path["points"]:
            paths.append({**path, "points": [point], "legacy_unsegmented": True})
    return paths


def point_trace_paths(points, series_name=None):
    """Group line-sample rows by explicit trace and segment identity.

    Rows without segment identity become singleton paths.  For identified
    segments, the row sequence is preserved unless the author supplied an
    explicit ``trace_order`` value.
    """
    groups = {}
    isolated = []
    for index, point in enumerate(points or []):
        if not is_trace_point(point):
            continue
        segment_id = point.get("segment_id") or point.get("trace_segment_id")
        if segment_id is None:
            isolated.append((index, point))
            continue
        branch = point.get("branch")
        key = (point.get("trace_id"), segment_id, branch)
        groups.setdefault(key, []).append((index, point))

    paths = []
    for (trace_id, segment_id, branch), indexed in groups.items():
        indexed.sort(key=lambda item: item[0])
        ordered_points = [point for _, point in indexed]
        if ordered_points and all(_finite(point.get("trace_order")) is not None for point in ordered_points):
            ordered_points = _ordered(ordered_points)
        paths.append({
            "trace_id": trace_id,
            "segment_id": segment_id,
            "series_name": series_name,
            "color_hex": None,
            "role": _point_role(ordered_points[0]),
            "authority": None,
            "branch": branch,
            "points": ordered_points,
            "legacy_unsegmented": False,
        })
    for _, point in isolated:
        paths.append({
            "trace_id": point.get("trace_id"),
            "segment_id": None,
            "series_name": series_name,
            "color_hex": None,
            "role": _point_role(point),
            "authority": None,
            "branch": point.get("branch"),
            "points": [point],
            "legacy_unsegmented": True,
        })
    return paths


def point_xy(point, axis_from_pixel=None):
    """Get finite axis coordinates, converting source pixels when needed."""
    x, y = _finite(point.get("x")), _finite(point.get("y"))
    px = point.get("px")
    if (x is None or y is None) and isinstance(px, (list, tuple)) and len(px) == 2:
        if axis_from_pixel is None:
            return None
        if x is None:
            x = _finite(axis_from_pixel("x", px[0]))
        if y is None:
            y = _finite(axis_from_pixel("y", px[1]))
    return (x, y) if x is not None and y is not None else None


def point_px(point):
    """Get finite source-pixel coordinates from an authored trace point."""
    px = point.get("px")
    if not isinstance(px, (list, tuple)) or len(px) != 2:
        return None
    x, y = _finite(px[0]), _finite(px[1])
    return (x, y) if x is not None and y is not None else None
