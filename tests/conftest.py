"""Shared fixtures: a small saved panel (combined_01/fig3a) copied into a temp dir."""
from __future__ import annotations

import json
import pathlib
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAVED_PANEL = ROOT / "artifacts" / "reviews" / "implementation-baseline-2026-09-28" / "combined_01" / "fig3a"


@pytest.fixture
def saved_panel(tmp_path):
    """Copy the saved fig3a panel and point spec.image at the copied panel.png."""
    if not SAVED_PANEL.is_dir():
        pytest.skip("saved panel fixture is not present")
    panel_dir = tmp_path / "combined_01" / "fig3a"
    shutil.copytree(SAVED_PANEL, panel_dir)
    spec_path = panel_dir / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["image"] = str(panel_dir / "panel.png")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return panel_dir
