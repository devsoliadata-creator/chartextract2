"""Validate and load an Agent 04 authoritative replacement points CSV.

Unlike the operation based editor, this boundary treats the authored CSV as the
complete final point set.  It validates every row, rebuilds the normal
extraction shape used by plots and hard checks, and keeps the original row cells
on each point for downstream export.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import math
import pathlib
import re
from collections import Counter

import cv2
from src.tools import stage_artifacts
from src.tools.extract import pixel_to_axis


_CONFIDENCE = {"high": 0.90, "medium": 0.75, "low": 0.40}
_EVIDENCE_TYPES = {
    "visible_marker", "partially_visible_marker", "estimated_marker",
    "reviewer_asserted_marker",
}
_MARKER_EVIDENCE_TYPES = {
    "visible_marker", "partially_visible_marker", "estimated_marker",
    "reviewer_asserted_marker",
}
_NON_MARKER_EVIDENCE_TYPES = {
    "line_sample", "template_fit", "template_fill", "reported_numeric", "manual",
}
# Identity columns: the model must reproduce them exactly; a mismatch is a real error.
_IDENTITY_COLUMNS = ("series_id", "series_name", "species", "export_eligible")
# Descriptive columns: fixed per series and already known from the candidate metadata.
# A model that copies the measured color (#5462fb) instead of the declared one (#5875F3)
# has not changed any measurement, so these cells are filled from the metadata rather
# than validated; the audit counts how many cells were normalized.
_DESCRIPTIVE_COLUMNS = (
    "x_unit", "y_unit", "line_style", "color_hex",
    "species_evidence", "quantity_type", "loading_basis",
)
_BOOLEAN_COLUMNS = ("is_inferred", "export_eligible")


class FinalCSVError(ValueError):
    """The final CSV is malformed or contradicts its source contract."""


class CandidateOmissionError(FinalCSVError):
    """Agent 04 omitted candidate IDs without explicitly deleting them.

    ``row_errors`` lists rows that were present in the CSV but rejected by the
    row validator; their IDs are counted as omissions so that the single
    correction request can name the exact defect instead of only the ID.
    """

    def __init__(self, point_ids, row_errors=None):
        self.point_ids = tuple(sorted(str(point_id) for point_id in point_ids))
        self.row_errors = list(row_errors or [])
        message = ("final CSV omits Agent 03 point IDs without an explicit delete: "
                   + ", ".join(self.point_ids))
        if self.row_errors:
            message += "; rejected rows: " + "; ".join(
                f"{item.get('point_id') or 'row ' + str(item.get('row'))}: {item.get('error')}"
                for item in self.row_errors[:12]
            )
        super().__init__(message)


def _fail(message: str) -> None:
    raise FinalCSVError(message)


def _finite(value, label: str, *, positive=False, nonnegative=False) -> float:
    if isinstance(value, bool):
        _fail(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(f"{label} must be a finite number")
    if not math.isfinite(number):
        _fail(f"{label} must be a finite number")
    if positive and number <= 0:
        _fail(f"{label} must be positive")
    if nonnegative and number < 0:
        _fail(f"{label} must be nonnegative")
    return number


def _numeric_text(value, label: str, *, optional=False, positive=False, nonnegative=False):
    if value == "" and optional:
        return None
    return _finite(value, label, positive=positive, nonnegative=nonnegative)


def _notes(row):
    result = {}
    for part in str(row.get("notes", "")).split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


def _rounding_tolerance(text: str) -> float:
    """Half a unit in the last non-padding decimal place, including exponent.

    Agent-authored CSVs may serialize a four-decimal calibrated value with
    trailing zeros to six places. Those zeros do not make the native pixel
    coordinate or fitted calibration more precise. The existing 5.1e-5 cap
    still rejects differences beyond four-decimal rounding.
    """
    value = str(text).strip().lower()
    match = re.fullmatch(r"[+-]?(?:\d+(?:\.(\d*))?|\.(\d+))(?:e([+-]?\d+))?", value)
    if not match:
        return 1e-8
    fraction = (match.group(1) if match.group(1) is not None else match.group(2) or "").rstrip("0")
    exponent = int(match.group(3) or 0)
    return min(5.1e-5, max(1e-8, 0.5000001 * 10.0 ** (exponent - len(fraction))))


def _is_shared_zero_origin(row, x, y, px, py, spec, calibration, frame, notes):
    """Allow Agent 04's explicit (0, 0) estimate at the calibrated origin.

    The native pixel is estimated with uncertainty, while the reported data
    coordinate is the exact shared zero required by the Agent 04 contract.
    This exception is limited to tagged estimated-marker rows on linear axes.
    """
    if (row.get("evidence_type") != "estimated_marker"
            or row.get("is_inferred") != "true"
            or notes.get("origin_estimate") != "true"
            or x != 0.0 or y != 0.0
            or notes.get("source") != "agent04"
            or notes.get("evidence_kind") != "estimated"
            or "shared" not in notes.get("source_evidence", "").lower()
            or "origin" not in notes.get("source_evidence", "").lower()
            or not notes.get("evidence_ref")):
        return False

    try:
        uncertainty_px = float(notes.get("uncertainty_px", ""))
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(uncertainty_px) or uncertainty_px <= 0:
        return False

    models = calibration.get("axis_models") or {}
    for axis, pixel, frame_min, frame_max in (
        ("x", px, frame[0], frame[2]),
        ("y", py, frame[1], frame[3]),
    ):
        axis_spec = spec.get(axis) or {}
        model = models.get(axis) or {}
        if axis_spec.get("scale") != "linear" or model.get("scale") != "linear":
            return False
        ticks = axis_spec.get("ticks") or []
        try:
            values = [float(value) for value in ticks]
            lower_value = axis_spec.get("minimum")
            upper_value = axis_spec.get("maximum")
            lower = float(lower_value) if lower_value is not None else min(values)
            upper = float(upper_value) if upper_value is not None else max(values)
            slope = float(model["slope"])
            intercept = float(model["intercept"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if (not math.isfinite(lower) or not math.isfinite(upper)
                or not math.isfinite(slope) or not math.isfinite(intercept)
                or lower > 0 or upper < 0 or slope == 0):
            return False
        origin_pixel = -intercept / slope
        if (not math.isfinite(origin_pixel)
                or not frame_min <= origin_pixel <= frame_max
                or abs(pixel - origin_pixel) > uncertainty_px):
            return False
    return True


def _validate_calibration(calibration, spec):
    if not isinstance(calibration, dict):
        _fail("report.calibration must contain the full calibration object")
    frame = calibration.get("frame_px")
    if not isinstance(frame, (list, tuple)) or len(frame) != 4:
        _fail("report.calibration.frame_px must contain [left, top, right, bottom]")
    frame = [_finite(value, "calibration frame coordinate") for value in frame]
    if frame[2] <= frame[0] or frame[3] <= frame[1]:
        _fail("calibration frame_px must have positive width and height")
    models = calibration.get("axis_models")
    if not isinstance(models, dict):
        _fail("report.calibration.axis_models is required")
    for axis in ("x", "y"):
        model = models.get(axis)
        if not isinstance(model, dict):
            _fail(f"report.calibration.axis_models.{axis} is required")
        slope = _finite(model.get("slope"), f"{axis} calibration slope")
        if abs(slope) <= 1e-15:
            _fail(f"{axis} calibration slope must be nonzero")
        _finite(model.get("intercept"), f"{axis} calibration intercept")
        if model.get("scale", "linear") not in {"linear", "log"}:
            _fail(f"{axis} calibration scale must be linear or log")
        expected_scale = str((spec.get(axis) or {}).get("scale") or "linear")
        if model.get("scale", "linear") != expected_scale:
            _fail(f"{axis} calibration scale conflicts with the chart specification")
    return frame


def _image_dimensions(spec):
    path = pathlib.Path(str(spec.get("image") or ""))
    if not path.is_file():
        _fail("spec.image must identify the source panel image")
    image = cv2.imread(str(path))
    if image is None:
        _fail("spec.image could not be read")
    return image.shape[1], image.shape[0]


def _validate_unresolved(unresolved):
    if not isinstance(unresolved, dict):
        _fail("report.unresolved_slots must be an object")
    if not {"total", "by_series", "slots"}.issubset(unresolved):
        _fail("report.unresolved_slots must preserve total, by_series, and slots")
    total = unresolved["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        _fail("report.unresolved_slots.total must be a nonnegative integer")
    if not isinstance(unresolved["by_series"], dict) or not isinstance(unresolved["slots"], list):
        _fail("report.unresolved_slots.by_series and slots have invalid types")
    if total != len(unresolved["slots"]):
        _fail("report.unresolved_slots.total must match its slots list")
    observed = Counter(str(slot.get("series_label") or "") for slot in unresolved["slots"]
                       if isinstance(slot, dict))
    declared = unresolved["by_series"]
    if any(not isinstance(key, str) or isinstance(value, bool)
           or not isinstance(value, int) or value < 0
           for key, value in declared.items()):
        _fail("report.unresolved_slots.by_series must contain nonnegative integer counts")
    # An agent may explicitly list a reviewed series with zero unresolved slots.
    # Keep that useful accounting while requiring every positive count to match
    # an actual slot; zeros must never mask a missing or extra slot.
    if any(declared.get(key) != count for key, count in observed.items()) or any(
        count != observed.get(key, 0) for key, count in declared.items()
    ):
        _fail("report.unresolved_slots.by_series must match its slots list")


def _series_maps(candidate, spec, figure_id, panel_id, answer=None):
    raw = candidate.get("series")
    if not isinstance(raw, list):
        _fail("candidate.series must be a list")
    seen_point_ids, seen_source_ids = set(), set()
    for series in raw:
        for point in series.get("points", []):
            for key, seen, label in (("point_id", seen_point_ids, "point_id"),
                                     ("source_instance_id", seen_source_ids, "source_instance_id")):
                value = point.get(key)
                if value not in (None, ""):
                    value = str(value)
                    if value in seen:
                        _fail(f"candidate contains duplicate {label}: {value}")
                    seen.add(value)

    normalized, _, metadata = stage_artifacts.build_candidate(
        candidate, spec, figure_id, panel_id,
    )
    expected_by_series = {}
    series_by_id = {}
    series_by_label = {}
    candidate_by_id = {}
    for series in normalized.get("series", []):
        label = str(series.get("label", ""))
        sid = str(series.get("series_id", ""))
        if not sid or sid in series_by_id or label in series_by_label:
            _fail("candidate has missing or duplicate eligible series identities")
        series_by_id[sid] = series
        series_by_label[label] = series
        for point in series.get("points", []):
            pid = str(point.get("point_id", ""))
            source_id = str(point.get("source_instance_id", ""))
            if not pid or not source_id or pid in candidate_by_id:
                _fail("candidate has missing or duplicate point identities")
            candidate_by_id[pid] = (series, point)
    metadata_by_id = {str(item.get("series_id")): item for item in metadata.get("series", [])}
    reviewed_styles = {
        str(item.get("label", "")): item
        for item in ((answer or {}).get("source_review") or {}).get("series", [])
        if isinstance(item, dict) and item.get("label")
    }
    for sid, series in series_by_id.items():
        item = metadata_by_id[sid]
        source_style = reviewed_styles.get(str(series.get("label", "")), {})
        expected_by_series[sid] = {
            "series_id": sid,
            "series_name": str(series.get("label", "")),
            "x_unit": str((spec.get("x") or {}).get("unit", "")),
            "y_unit": str((spec.get("y") or {}).get("unit", "")),
            # Agent 04's source-checked final style can correct the candidate
            # glyph measurement. Its marker is authoritative for this CSV.
            "marker_shape": source_style.get("marker") or item.get("marker_shape", "circle"),
            "candidate_marker_shape": item.get("marker_shape", "circle"),
            "marker_style_source": "agent04_source_review" if source_style.get("marker") else "candidate",
            "line_style": item.get("line_style", "solid"),
            "color_hex": item.get("color_hex", ""),
            "species": item.get("species", ""),
            "export_eligible": str(bool(item.get("export_eligible"))).lower(),
            "species_evidence": item.get("species_evidence", ""),
            "quantity_type": item.get("quantity_type", ""),
            "loading_basis": item.get("loading_basis", ""),
        }
    return normalized, series_by_id, series_by_label, candidate_by_id, expected_by_series


def _parse_csv(path):
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames
            if header != stage_artifacts.STAGE_COLUMNS:
                _fail("CSV header must exactly match the canonical points column order")
            rows = []
            for index, row in enumerate(reader, 2):
                if None in row or any(value is None for value in row.values()):
                    _fail(f"CSV row {index} has a malformed number of cells")
                row = dict(row)
                # Python's csv writer serializes bool values as ``True``/``False``.
                # The canonical CSV contract uses lowercase JSON-style tokens, so
                # accept only those exact Python spellings and normalize them at
                # the file boundary. Other spellings remain invalid and are
                # rejected by _check_row.
                for column in _BOOLEAN_COLUMNS:
                    if row[column] in {"True", "False"}:
                        row[column] = row[column].lower()
                rows.append(row)
            return rows
    except UnicodeDecodeError as error:
        raise FinalCSVError("CSV must be UTF-8 encoded") from error
    except OSError as error:
        raise FinalCSVError(f"could not read final CSV: {error}") from error


def _check_row(row, index, spec, figure_id, panel_id, calibration, frame,
               image_size, series_by_id, series_by_label, candidate_by_id,
               expected_by_series, old_by_source, normalized=None):
    label = f"CSV row {index}"
    if row["figure_id"] != str(figure_id) or row["panel_id"] != str(panel_id):
        _fail(f"{label} has the wrong figure_id or panel_id")
    if not row["point_id"].strip() or not row["source_instance_id"].strip():
        _fail(f"{label} requires nonempty point_id and source_instance_id")
    if row["is_inferred"] not in {"true", "false"} or row["export_eligible"] != "true":
        _fail(f"{label} has invalid boolean fields or is not export eligible")
    if row["confidence"] not in _CONFIDENCE:
        _fail(f"{label} confidence must be high, medium, or low")
    notes = _notes(row)
    if row["evidence_type"] in _NON_MARKER_EVIDENCE_TYPES:
        _fail(f"{label} violates the marker-only final CSV contract")
    if row["evidence_type"] not in _EVIDENCE_TYPES:
        _fail(f"{label} has an unsupported evidence_type")
    if row["evidence_type"] not in _MARKER_EVIDENCE_TYPES:
        _fail(f"{label} must describe a marker; Agent 04 final CSVs cannot contain line samples or templates")
    existing = candidate_by_id.get(row["point_id"])
    cited_fields = ("evidence_kind", "evidence_ref", "source_evidence", "uncertainty_px")
    has_citation = any(notes.get(key) for key in cited_fields)
    if row["evidence_type"] == "reviewer_asserted_marker":
        if existing is not None or has_citation:
            _fail(f"{label} reviewer_asserted_marker is only for uncited Agent 04 additions")
        if row["confidence"] != "low" or row["is_inferred"] != "false":
            _fail(f"{label} reviewer_asserted_marker additions require confidence=low and is_inferred=false")
        if "reviewer_asserted_marker=true" not in str(row.get("notes", "")):
            note = str(row.get("notes", "")).strip()
            row["notes"] = "; ".join(part for part in (note, "reviewer_asserted_marker=true") if part)
    elif existing is None and not has_citation:
        if row["confidence"] != "low" or row["is_inferred"] != "false":
            _fail(f"{label} uncited additions require confidence=low and is_inferred=false")
        row["evidence_type"] = "reviewer_asserted_marker"
        note = str(row.get("notes", "")).strip()
        row["notes"] = "; ".join(part for part in (note, "reviewer_asserted_marker=true") if part)
        notes = _notes(row)
    if row["is_inferred"] == "true" and row["evidence_type"] != "estimated_marker":
        _fail(f"{label} inferred marker rows must use estimated_marker")
    if row["evidence_type"] == "estimated_marker":
        if row["is_inferred"] != "true" or row["confidence"] != "low":
            _fail(f"{label} estimated_marker rows require is_inferred=true and confidence=low")
        if not row["uncertainty_x"].strip() or not row["uncertainty_y"].strip():
            _fail(f"{label} estimated_marker rows require nonblank uncertainty_x and uncertainty_y")
    if row["species"].strip().upper() != "CO2" or not row["species_evidence"].strip():
        _fail(f"{label} must identify an eligible CO2 species with evidence")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", row["color_hex"]):
        _fail(f"{label} color_hex must be a six-digit hex color")

    sid = row["series_id"]
    series = series_by_id.get(sid)
    if series is None or row["series_name"] != str(series.get("label", "")):
        _fail(f"{label} refers to a series that is not in the candidate")
    label_series = series_by_label.get(row["series_name"])
    if label_series is not series:
        _fail(f"{label} series identity is inconsistent")
    expected = expected_by_series.get(sid)
    if expected is None:
        _fail(f"{label} series is not an eligible CO2 series")
    for column in _IDENTITY_COLUMNS:
        if row[column] != expected[column]:
            _fail(f"{label} {column} conflicts with the candidate series identity")
    for column in _DESCRIPTIVE_COLUMNS:
        if row[column] != expected[column]:
            if normalized is not None:
                normalized[column] += 1
            row[column] = expected[column]
    # Agent 04's final source review is authoritative for marker rows.
    expected_marker = expected["marker_shape"]
    if row["marker_shape"] != expected_marker:
        if (expected["marker_style_source"] == "agent04_source_review"
                and row["marker_shape"] == expected["candidate_marker_shape"]):
            # Reconcile only a known stale candidate glyph. Arbitrary marker
            # changes still fail; the source-backed Agent 04 read wins here.
            row["marker_shape"] = expected_marker
            if normalized is not None:
                normalized["marker_shape"] += 1
        else:
            _fail(f"{label} marker_shape conflicts with Agent 04's source marker")

    x = _numeric_text(row["x"], f"{label} x")
    y = _numeric_text(row["y"], f"{label} y")
    px = _numeric_text(row["source_pixel_x"], f"{label} source_pixel_x")
    py = _numeric_text(row["source_pixel_y"], f"{label} source_pixel_y")
    frame_warning = not (frame[0] <= px <= frame[2] and frame[1] <= py <= frame[3])
    if not (0 <= px < image_size[0] and 0 <= py < image_size[1]):
        _fail(f"{label} native pixel lies outside the panel image")
    masked_bboxes = []
    for mask in spec.get("mask_bboxes") or []:
        if isinstance(mask, (list, tuple)) and len(mask) == 4:
            left, top, right, bottom = map(float, mask)
            if left <= px <= right and top <= py <= bottom:
                masked_bboxes.append([left, top, right, bottom])
    if masked_bboxes:
        notes["mask_warning"] = "true"
        if "mask_warning=true" not in str(row.get("notes", "")):
            row["notes"] = "; ".join(part for part in (
                str(row.get("notes", "")).strip(), "mask_warning=true",
            ) if part)

    try:
        expected_x = float(pixel_to_axis(calibration, "x", px))
        expected_y = float(pixel_to_axis(calibration, "y", py))
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise FinalCSVError(f"{label} cannot be checked against the calibration") from error
    if not math.isfinite(expected_x) or not math.isfinite(expected_y):
        _fail(f"{label} calibration produces a non-finite value")
    shared_zero_origin = _is_shared_zero_origin(
        row, x, y, px, py, spec, calibration, frame, notes,
    )
    calibration_warnings = []
    if abs(x - expected_x) > _rounding_tolerance(row["x"]) and not shared_zero_origin:
        calibration_warnings.append("x disagrees with its native pixel and calibration")
    if abs(y - expected_y) > _rounding_tolerance(row["y"]) and not shared_zero_origin:
        calibration_warnings.append("y disagrees with its native pixel and calibration")

    for col in ("uncertainty_x", "uncertainty_y"):
        _numeric_text(row[col], f"{label} {col}", optional=True, nonnegative=True)
    for value_col, unit_col in (("normalized_x", "normalized_x_unit"),
                                ("normalized_y", "normalized_y_unit")):
        value = _numeric_text(row[value_col], f"{label} {value_col}", optional=True)
        if (value is None) != (row[unit_col] == ""):
            _fail(f"{label} {unit_col} must be present exactly when {value_col} is present")
    has_normalized = bool(row["normalized_x"] or row["normalized_y"])
    if has_normalized != bool(row["conversion_id"]):
        _fail(f"{label} conversion_id must accompany normalized values")

    segment_id = trace_order = None
    if existing:
        original_series, original_point = existing
        if row["source_instance_id"] != str(original_point.get("source_instance_id")):
            _fail(f"{label} changes source_instance_id for an existing point_id")
        moved = (float(original_point["px"][0]) != px or float(original_point["px"][1]) != py)
        reassigned = (str(original_series.get("label")) != row["series_name"]
                      or stage_artifacts.point_evidence_type(original_point) != row["evidence_type"])
    else:
        moved, reassigned = False, False
        if row["point_id"] in candidate_by_id or row["source_instance_id"] in old_by_source:
            _fail(f"{label} reuses an existing point or source instance identity")
    if existing is None and row["evidence_type"] == "reviewer_asserted_marker":
        # This explicitly labeled, low-confidence source assertion is the
        # supported uncited-addition path; its finite coordinates and series
        # identity were validated above.
        pass
    elif moved or existing is None:
        kind = notes.get("evidence_kind", "")
        support = notes.get("source_evidence", "")
        reference = notes.get("evidence_ref", "")
        uncertainty = _numeric_text(notes.get("uncertainty_px", ""),
                                    f"{label} notes uncertainty_px", optional=True, positive=True)
        allowed_kinds = {"native_visible", "partial_marker", "estimated"}
        if kind not in allowed_kinds or not support or not reference or uncertainty is None:
            _fail(f"{label} new or moved points require native evidence, reference, kind, and positive uncertainty_px notes")
        if kind == "estimated" and (row["evidence_type"] != "estimated_marker" or row["confidence"] != "low"
                                     or not row["uncertainty_x"].strip() or not row["uncertainty_y"].strip()):
            _fail(f"{label} estimated points require evidence_type=estimated_marker, confidence=low, "
                  "and nonblank uncertainty_x/uncertainty_y")
    elif reassigned:
        allowed_kinds = {"native_visible", "partial_marker", "estimated"}
        if (notes.get("evidence_kind") not in allowed_kinds
                or not notes.get("source_evidence") or not notes.get("evidence_ref")):
            _fail(f"{label} reassignments require native evidence, reference, and evidence kind notes")

    evidence_kind = notes.get("evidence_kind") or {
        "visible_marker": "native_visible",
        "partially_visible_marker": "partial_marker",
        "estimated_marker": "estimated",
    }.get(row["evidence_type"])
    source = notes.get("source") or "agent04_csv"
    return series, {"x": x, "y": y, "px": [px, py], "point_id": row["point_id"],
                    "source_instance_id": row["source_instance_id"],
                    "confidence": _CONFIDENCE[row["confidence"]],
                    "source": source,
                    "is_inferred": row["is_inferred"] == "true",
                    "evidence_type": row["evidence_type"],
                    "evidence_kind": evidence_kind,
                    "evidence_ref": notes.get("evidence_ref"),
                    "source_evidence": notes.get("source_evidence"),
                     "branch": notes.get("branch"),
                     "assigned_by": notes.get("assigned_by"),
                     "segment_id": segment_id,
                     "trace_order": None if trace_order is None else int(trace_order),
                     "uncertainty_px": (_numeric_text(notes.get("uncertainty_px", ""),
                                                      f"{label} notes uncertainty_px", optional=True)),
                    "overlap_flag": notes.get("overlap_flag") == "true",
                    "frame_warning": frame_warning,
                    "calibration_warnings": calibration_warnings,
                    "mask_warning": bool(masked_bboxes),
                    "mask_bboxes": masked_bboxes,
                    "csv_row": row}, moved, reassigned


def _explicit_overlap_support(point):
    """True only for a separately identified marker with cited native support."""
    if not point.get("overlap_flag"):
        return False
    row = point.get("csv_row") or {}
    if row.get("evidence_type") == "line_sample":
        return False
    notes = _notes(row)
    if notes.get("evidence_kind") not in {"native_visible", "partial_marker", "estimated"}:
        return False
    if not notes.get("source_evidence") or not notes.get("evidence_ref"):
        return False
    try:
        uncertainty = float(notes.get("uncertainty_px", ""))
    except (TypeError, ValueError):
        return False
    return math.isfinite(uncertainty) and uncertainty > 0


def _same_series_pixel_warnings(pixels_by_key):
    """Report duplicate authored positions without overriding Agent 04's inventory."""
    warnings = []
    for (label, px, py), entries in pixels_by_key.items():
        if len(entries) < 2:
            continue
        warnings.append({
            "series_label": label,
            "source_pixel": [px, py],
            "point_ids": [entry["point_id"] for entry in entries],
            "reason": "multiple Agent 04 rows share one native pixel; retained as reviewer-authored inventory",
        })
    return warnings


def _rebuild_unresolved(unresolved):
    """Rebuild a consistent unresolved_slots object from whatever slots are usable."""
    slots = []
    if isinstance(unresolved, dict) and isinstance(unresolved.get("slots"), list):
        slots = [slot for slot in unresolved["slots"]
                 if isinstance(slot, dict) and str(slot.get("series_label") or "").strip()
                 and str(slot.get("reason") or "").strip()]
    by_series = Counter(str(slot["series_label"]) for slot in slots)
    return {"total": len(slots), "by_series": dict(sorted(by_series.items())), "slots": slots}


def load_final_csv(csv_path, report, candidate, spec, figure_id, panel_id, *,
                   reconcile_bookkeeping=False, tolerate_row_errors=False,
                   candidate_rows=None, carry_over_omissions=False):
    """Load an Agent 04 full-replacement CSV into the normal extraction form.

    Returns ``(extraction, rows, audit)``.  ``rows`` and each point's
    ``csv_row`` retain all authored cells, including notes and optional fields.

    The opt-in recovery mode tolerates only redundant report counts and a
    different declared change inventory. Every data/evidence check still runs.
    It preserves the authored report in the audit, records unexplained changes
    without inventing reasons, and requires human review. The caller's report
    is never mutated while examining alternative cited files.

    ``tolerate_row_errors`` keeps the table when individual rows or report
    fields are malformed: a bad row is recorded in ``audit["rejected_rows"]``
    and skipped instead of failing the whole CSV, and report-level fields that
    cannot be used are normalized and listed in ``audit["report_normalizations"]``.
    A rejected row whose ID is an Agent 03 candidate counts as an omission.
    With ``carry_over_omissions`` and ``candidate_rows`` (the Agent 03 CSV rows),
    those omitted candidates are re-validated from the Agent 03 table and kept,
    marked ``carried_from_agent03=true``; otherwise ``CandidateOmissionError``
    is raised so the host can request one correction.
    """
    authored_report = copy.deepcopy(report)
    report = copy.deepcopy(report)
    lenient = reconcile_bookkeeping or tolerate_row_errors
    normalizations = []

    def normalize(condition, message, fix):
        """Apply ``fix`` and record it in tolerant mode; otherwise fail."""
        if not condition:
            return
        if not tolerate_row_errors:
            _fail(message)
        fix()
        normalizations.append(message)

    if not isinstance(report, dict):
        _fail("report must be a JSON object")
    normalize(str(report.get("verdict", "")).lower() not in {"accept", "review", "reject"},
              "report must include a valid verdict", lambda: report.update(verdict="review"))
    report["verdict"] = str(report["verdict"]).lower()
    normalize(not isinstance(report.get("series"), list),
              "report.series must be the existing QA series report", lambda: report.update(series=[]))
    normalize(isinstance(report.get("row_count"), bool) or not isinstance(report.get("row_count"), int),
              "report.row_count must be an integer", lambda: report.update(row_count=0))
    normalize(not isinstance(report.get("changes"), list),
              "report.changes must list the reason for every row-level change", lambda: report.update(changes=[]))
    expected_digest = report.get("points_csv_sha256")
    if expected_digest is not None:
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            _fail("report.points_csv_sha256 must be a lowercase SHA-256 digest")
        try:
            actual_digest = hashlib.sha256(pathlib.Path(csv_path).read_bytes()).hexdigest()
        except OSError as error:
            raise FinalCSVError(f"could not read final CSV: {error}") from error
        if actual_digest != expected_digest:
            _fail("CSV bytes do not match report.points_csv_sha256")
    calibration = copy.deepcopy(report.get("calibration"))
    frame = _validate_calibration(calibration, spec)
    image_size = _image_dimensions(spec)
    if not (0 <= frame[0] < frame[2] <= image_size[0]
            and 0 <= frame[1] < frame[3] <= image_size[1]):
        _fail("calibration frame_px lies outside the panel image")
    unresolved = copy.deepcopy(report.get("unresolved_slots"))
    try:
        _validate_unresolved(unresolved)
    except FinalCSVError as error:
        if not tolerate_row_errors:
            raise
        unresolved = _rebuild_unresolved(unresolved)
        normalizations.append(f"{error}; unresolved_slots rebuilt from its usable slots")

    (extraction, series_by_id, series_by_label, candidate_by_id,
     expected_by_series) = _series_maps(
        copy.deepcopy(candidate), spec, figure_id, panel_id, report,
    )
    old_by_source = {
        str(point.get("source_instance_id")): (series, point)
        for series, point in candidate_by_id.values()
    }
    rows = _parse_csv(csv_path)
    seen_points, seen_sources, pixels_by_key = set(), set(), {}
    seen_trace_orders = set()
    points_by_series = {label: [] for label in series_by_label}
    report_series = {}
    for item in report["series"]:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not item["label"].strip():
            normalize(True, "each report.series entry requires a label", lambda: None)
            continue
        label = item["label"]
        if label in report_series:
            normalize(True, f"report.series contains duplicate label: {label}", lambda: None)
            continue
        if label not in series_by_label:
            normalize(True, f"report.series contains a noneligible or unknown label: {label}", lambda: None)
            continue
        report_series[label] = item
    for label in series_by_label:
        if label not in report_series:
            normalize(True, "report.series must include every eligible candidate series exactly once",
                      lambda: None)
            report_series[label] = {"label": label, "coverage": "uncertain", "final_count": 0,
                                    "comment": "Series entry supplied by the host; Agent 04 did not report it."}
    report["series"] = list(report_series.values())
    audit = {
        "authoritative_csv": True,
        "added": [], "deleted": [], "moved": [], "reassigned": [],
        "candidate_count": len(candidate_by_id), "final_count": len(rows),
        "rejected_rows": [], "carried_over": [],
    }
    normalized = Counter()
    accepted_rows = []

    def accept_row(row, index, *, carried=False):
        """Validate one row; returns True when it joined the inventory."""
        point_id, source_id = row["point_id"], row["source_instance_id"]
        try:
            if point_id in seen_points:
                _fail(f"duplicate point_id: {point_id}")
            if source_id in seen_sources:
                _fail(f"duplicate source_instance_id: {source_id}")
            series, point, moved, reassigned = _check_row(
                row, index, spec, figure_id, panel_id, calibration, frame,
                image_size, series_by_id, series_by_label, candidate_by_id,
                expected_by_series, old_by_source, normalized,
            )
        except FinalCSVError as error:
            if not tolerate_row_errors:
                raise
            audit["rejected_rows"].append({
                "row": index, "point_id": point_id, "error": str(error),
                "source": "agent03_carry_over" if carried else "agent04_csv",
            })
            return False
        seen_points.add(point_id)
        seen_sources.add(source_id)
        if carried:
            note = str(row.get("notes", "")).strip()
            row["notes"] = "; ".join(part for part in (note, "carried_from_agent03=true") if part)
            point["carried_from_agent03"] = True
            audit["carried_over"].append(point_id)
        pixel_key = (str(series.get("label")), point["px"][0], point["px"][1])
        pixels_by_key.setdefault(pixel_key, []).append({
            "row": row, "point": point, "point_id": point_id,
            "candidate_by_id": candidate_by_id, "moved": moved, "reassigned": reassigned,
        })
        if row["evidence_type"] == "line_sample" and point.get("segment_id") is not None:
            trace_key = (str(series["label"]), point.get("branch"), point["segment_id"], point.get("trace_order"))
            if trace_key in seen_trace_orders:
                _fail(f"duplicate trace_order in series {series['label']} segment {point['segment_id']}")
            seen_trace_orders.add(trace_key)
        points_by_series[str(series["label"])].append(point)
        if point.get("frame_warning"):
            audit.setdefault("frame_warnings", []).append({
                "point_id": point_id,
                "source_pixel": list(point.get("px") or []),
                "reason": "authored point lies outside the calibrated plot frame; retained",
            })
        if point.get("calibration_warnings"):
            audit.setdefault("calibration_warnings", []).append({
                "point_id": point_id,
                "warnings": list(point["calibration_warnings"]),
                "authored_value": [point.get("x"), point.get("y")],
                "source_pixel": list(point.get("px") or []),
            })
        if point.get("mask_warning"):
            audit.setdefault("masked_point_warnings", []).append({
                "point_id": point_id,
                "series_label": str(series.get("label")),
                "source_pixel": list(point.get("px") or []),
                "mask_bboxes": point.get("mask_bboxes") or [],
                "reason": "authored point is inside a configured source mask; retained for review",
            })
        prior = candidate_by_id.get(point_id)
        if prior is None:
            audit["added"].append(point_id)
            if point["evidence_type"] == "reviewer_asserted_marker":
                audit.setdefault("reviewer_asserted_additions", []).append(point_id)
        else:
            old_series, old_point = prior
            if moved:
                audit["moved"].append({"point_id": point_id, "from_px": list(old_point.get("px") or []),
                                       "to_px": list(point["px"])})
            if reassigned:
                change = {"point_id": point_id, "from": old_series.get("label"),
                          "to": series.get("label")}
                old_evidence_type = stage_artifacts.point_evidence_type(old_point)
                if old_evidence_type != row["evidence_type"]:
                    change.update(from_evidence_type=old_evidence_type,
                                  to_evidence_type=row["evidence_type"])
                audit["reassigned"].append(change)
        accepted_rows.append(row)
        return True

    for index, row in enumerate(rows, 2):
        accept_row(row, index)

    explicit_delete_ids = {
        str(change.get("point_id"))
        for change in report["changes"]
        if isinstance(change, dict) and change.get("action") == "delete"
        and isinstance(change.get("point_id"), str)
    }
    undeclared_omissions = set(candidate_by_id) - seen_points - explicit_delete_ids
    if undeclared_omissions:
        if not (carry_over_omissions and tolerate_row_errors):
            raise CandidateOmissionError(undeclared_omissions, audit["rejected_rows"])
        candidate_rows_by_id = {
            str(row.get("point_id")): dict(row) for row in (candidate_rows or [])
            if row.get("point_id")
        }
        for point_id in sorted(undeclared_omissions):
            source_row = candidate_rows_by_id.get(point_id)
            if source_row is None:
                audit["rejected_rows"].append({
                    "row": None, "point_id": point_id, "source": "agent03_carry_over",
                    "error": "Agent 03 CSV row is unavailable for carry-over",
                })
                continue
            accept_row(source_row, None, carried=True)
    rows = accepted_rows
    audit["final_count"] = len(rows)
    audit["collision_warnings"] = _same_series_pixel_warnings(pixels_by_key)
    audit["deleted"] = sorted(set(candidate_by_id) - seen_points)
    if report["row_count"] != len(rows) and not lenient:
        _fail("report.row_count must match the authoritative CSV row count")
    # descriptive cells replaced by the candidate metadata value, per column
    audit["normalized_columns"] = dict(sorted(normalized.items()))
    audit["series_counts"] = {
        label: {"candidate": len(series.get("points", [])), "final": len(points_by_series[label])}
        for label, series in series_by_label.items()
    }
    for label, item in report_series.items():
        final_count = item.get("final_count")
        if isinstance(final_count, bool) or not isinstance(final_count, int):
            normalize(True, f"report.series {label} requires an integer final_count",
                      lambda item=item, label=label: item.update(final_count=len(points_by_series[label])))
        if item["final_count"] != len(points_by_series[label]) and not lenient:
            _fail(f"report.series {label} final_count does not match the authoritative CSV")

    inferred_changes = set()
    inferred_changes.update(("add", point_id) for point_id in audit["added"])
    inferred_changes.update(("delete", point_id) for point_id in audit["deleted"])
    inferred_changes.update(("move", item["point_id"]) for item in audit["moved"])
    inferred_changes.update(("reassign", item["point_id"]) for item in audit["reassigned"])
    reported_changes = {}
    malformed_changes = []
    for change in report["changes"]:
        try:
            if not isinstance(change, dict):
                _fail("each report.changes entry must be an object")
            action, point_id = change.get("action"), change.get("point_id")
            if action not in {"add", "move", "delete", "reassign"} or not isinstance(point_id, str) or not point_id.strip():
                _fail("report.changes entries require action and point_id")
            if not isinstance(change.get("reason"), str) or not change["reason"].strip():
                _fail(f"report change {action} {point_id} requires a reason")
            if not isinstance(change.get("source_evidence"), str) or not change["source_evidence"].strip():
                _fail(f"report change {action} {point_id} requires source_evidence")
            key = (action, point_id)
            if key in reported_changes:
                _fail(f"duplicate report change {action} for point {point_id}")
        except FinalCSVError as error:
            if not tolerate_row_errors:
                raise
            malformed_changes.append({"change": change, "error": str(error)})
            continue
        reported_changes[key] = change
    if malformed_changes:
        audit["malformed_changes"] = malformed_changes
        normalizations.append(f"{len(malformed_changes)} malformed report.changes entries were ignored")
    if set(reported_changes) != inferred_changes and not lenient:
        _fail("report.changes must match the additions, deletions, moves, and reassignments in the CSV")
    missing_changes = inferred_changes - set(reported_changes)
    extra_changes = set(reported_changes) - inferred_changes
    count_corrections = {
        label: {"reported": item["final_count"], "actual": len(points_by_series[label])}
        for label, item in report_series.items()
        if item["final_count"] != len(points_by_series[label])
    }
    bookkeeping_mismatch = (
        report["row_count"] != len(rows) or bool(count_corrections)
        or bool(missing_changes) or bool(extra_changes)
    )
    if bookkeeping_mismatch:
        audit["report_reconciliation"] = {
            "original_report": authored_report,
            "row_count": {"reported": report["row_count"], "actual": len(rows)},
            "series_counts": count_corrections,
            "unreported_changes": [
                {"action": action, "point_id": point_id}
                for action, point_id in sorted(missing_changes)
            ],
            "reported_changes_not_in_csv": [
                copy.deepcopy(reported_changes[key]) for key in sorted(extra_changes)
            ],
            "reconciliation_modified_measurements": False,
        }
        report["row_count"] = len(rows)
        for label, item in report_series.items():
            item["final_count"] = len(points_by_series[label])
        if report["verdict"] == "accept":
            report["verdict"] = "review"
        report.setdefault("uncertainties", []).append(
            "The final CSV passed data validation, but Agent 04's report bookkeeping differed. "
            f"Counts were reconciled; {len(missing_changes)} actual changes have no matching report entry "
            f"and {len(extra_changes)} reported changes are absent from the CSV diff. "
            "The original report and exact diff are preserved in the audit; human review is required."
        )
    if audit["rejected_rows"] or audit["carried_over"] or normalizations:
        audit["report_normalizations"] = normalizations
        if report["verdict"] == "accept":
            report["verdict"] = "review"
        parts = []
        if audit["rejected_rows"]:
            parts.append(f"{len(audit['rejected_rows'])} CSV rows failed validation and were not published")
        if audit["carried_over"]:
            parts.append(f"{len(audit['carried_over'])} Agent 03 rows were carried over unchanged "
                         "because Agent 04's version of them was invalid or missing")
        if normalizations:
            parts.append(f"{len(normalizations)} report fields were normalized by the host")
        report.setdefault("uncertainties", []).append(
            "; ".join(parts) + ". Details are in the Python edit audit; human review is required."
        )
    # The model's reasons remain its own claims. Actual operations come from
    # the validated replacement CSV; an absent explanation stays absent.
    audit["reported_changes"] = copy.deepcopy(report["changes"])
    audit["requested"] = len(report["changes"])
    audit["applied"] = len(inferred_changes)
    audit["operations"] = [
        {**copy.deepcopy(item), "status": "applied" if key in inferred_changes else "not_in_csv"}
        for key, item in reported_changes.items()
    ] + [
        {"action": action, "point_id": point_id, "status": "applied", "report_status": "missing"}
        for action, point_id in sorted(missing_changes)
    ]
    audit["change_reasons"] = {
        f"{action}:{point_id}": {
            "reason": item["reason"], "source_evidence": item["source_evidence"],
        }
        for (action, point_id), item in reported_changes.items()
    }

    for series in extraction.get("series", []):
        label = str(series.get("label"))
        series["points"] = points_by_series[label]
        series["n_points"] = len(series["points"])
    extraction["calibration"] = calibration
    extraction["unresolved_slots"] = unresolved
    extraction["unresolved_total"] = unresolved["total"]
    extraction["authored_rows"] = rows
    extraction["agent04_report"] = copy.deepcopy(report)
    audit["contains_inferred"] = any(row["is_inferred"] == "true" for row in rows)
    audit["requires_review"] = (audit["contains_inferred"] or bookkeeping_mismatch
                                or bool(audit["rejected_rows"]) or bool(audit["carried_over"])
                                or bool(normalizations))
    return extraction, rows, audit
