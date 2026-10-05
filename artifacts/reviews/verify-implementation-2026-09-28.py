"""Local presentation/preparation replay; never runs models or edits saved runs."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import faulthandler

faulthandler.dump_traceback_later(45, repeat=True)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.tools import adjudicate_tiles, dense_regions, recreate, review_page

BASE = ROOT / "artifacts/reviews/implementation-baseline-2026-09-28"
OUT = ROOT / "artifacts/reviews/implementation-preview-2026-09-28"
OUT.mkdir(parents=True, exist_ok=True)
manifest = json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))
checks = []
for entry in manifest["files"]:
    source = ROOT / entry["path"]
    checks.append({"path": entry["path"], "unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]})
assert all(item["unchanged"] for item in checks), checks
report = {"note": "Presentation/preparation replay only; no new reviewer extraction or recall claim.",
          "preserved_artifacts": len(checks), "panels": []}
for document, panel_name in [("combined_01", "fig2a"), ("combined_01", "fig3a"), ("f2b", "figp1b")]:
    panel = BASE / document / panel_name
    out = OUT / document / panel_name
    out.mkdir(parents=True, exist_ok=True)
    spec = json.loads((panel / "spec.json").read_text(encoding="utf-8"))
    spec["image"] = str(panel / "panel.png")
    final = json.loads((panel / "agent04/points.json").read_text(encoding="utf-8"))
    print(f"Render {document}/{panel_name}", flush=True)
    recreate.recreate(final, spec, str(out / "recreated.png"), curve_traces=[], evidence_view=True)
    recreate.side_by_side(str(panel / "panel.png"), str(out / "recreated.png"), str(out / "compare.png"))
    review_page.build(str(panel / "agent04/points.json"), str(panel / "panel.png"), str(out / "review.html"),
                      status="partial_review_required", status_reason="Saved historical rows; display preview only.")
    print(f"Prepare trace evidence {document}/{panel_name}", flush=True)
    candidate = json.loads((panel / "python/points.json").read_text(encoding="utf-8"))
    traces, audit = dense_regions.build_supporting_traces(candidate, spec, spec["image"])
    audit["legacy_proposals"] = dense_regions.move_supporting_point_proposals(candidate, traces, spec)
    (out / "supporting_traces.json").write_text(json.dumps(traces, indent=1), encoding="utf-8")
    entry = {"document": document, "panel": panel_name, "trace_preparation": audit}
    if panel_name == "fig3a":
        print("Prepare unknown-slot crops", flush=True)
        _, tiles = adjudicate_tiles.build_review_tiles(candidate, spec, out / "tiles", spec.get("complex_regions", []))
        entry["slot_crops"] = [{key: item.get(key) for key in ("item_id", "source_pixel", "source_bbox_px", "native_tile", "crop_transform")}
                               for item in tiles["review_items"] if item["item_type"] == "slot"]
    report["panels"].append(entry)
    print(f"Prepared {document}/{panel_name}", flush=True)
(OUT / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"preserved_artifacts": len(checks), "output": str(OUT)}), flush=True)
faulthandler.cancel_dump_traceback_later()
