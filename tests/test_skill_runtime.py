from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def api():
    source = ROOT / "scripts/skill_runtime.py"
    assert source.is_file(), "Current-host capability probe is not implemented"
    spec = importlib.util.spec_from_file_location("skill_runtime_test_target", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_optional_tools_do_not_block_docx_packets(monkeypatch):
    monkeypatch.setenv("PATH", "")
    def forbidden(*args, **kwargs):
        raise AssertionError("Probe must not spawn anything with empty executable capability")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = api().probe_capabilities(ROOT, requested=["docx"])
    assert report["status"] == "ready"
    assert report["capabilities"]["docx"]["status"] == "available"
    assert report["capabilities"]["tex"]["status"] == "unavailable"
    assert report["capabilities"]["wps"]["status"] == "host_check_required"
    assert report["capabilities"]["independent_review"]["status"] == "host_check_required"
    assert report["provider_selected"] is None


def test_requested_missing_renderer_is_actionable_not_ready(monkeypatch):
    monkeypatch.setenv("PATH", "")
    report = api().probe_capabilities(ROOT, requested=["equation_geometry"])
    assert report["status"] == "blocked"
    assert "soffice" in report["capabilities"]["equation_geometry"]["missing"]
    assert report["capabilities"]["word_release"]["status"] != "available"


def test_probe_rejects_unknown_capability_instead_of_ignoring_it():
    with pytest.raises(ValueError, match="unknown capability"):
        api().probe_capabilities(ROOT, requested=["magic_signed_word"])
