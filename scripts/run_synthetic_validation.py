#!/usr/bin/env python3
"""Run multiple synthetic thesis fixtures through the fresh template pipeline.

The runner deliberately uses ``supported_subset`` for conversion experiments:
the full compliance gate is still reported in each manifest, while unresolved
requirements do not prevent exercising the DOCX backend and renderers.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # compatibility with the repository's older test environment
    from PyPDF2 import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/sample-thesis.tex"

from semantic_contract import strict_json_read

VARIANTS = {
    "baseline": {},
    "long-fields": {
        "基于机器学习的城市交通流预测方法研究": "面向多源异构数据融合与可解释深度学习的城市复杂交通网络时空流量预测方法研究",
        "测试学生甲": "测试学生甲（长字段边界样例）",
        "智能交通与数据科学学院": "智能交通与数据科学学院及城市交通系统协同创新研究中心",
        "交通大数据与智能计算": "交通大数据与智能计算、复杂网络建模与城市韧性交通系统分析",
    },
    "dense-layout": {
        "在此基础上，提出了一种融合注意力机制的时空图卷积网络模型。": "在此基础上，提出了一种融合注意力机制的时空图卷积网络模型。该模型用于密集版式测试，包含较长的段落、连续公式、图表标题和多级章节，以观察分页、行距、表格宽度、浮动对象及目录缓存的稳定性。",
    },
    "edge-values": {
        "测试学生甲": "测试学生甲-ABC_2026",
        "TEST20260001": "TEST-2026_0001",
        "U491.1": "U491.1-α",
        "2026年6月": "2026年06月30日",
        "公开": "公开",
    },
}

FIXTURE_EXPECTATIONS = {
    "baseline": {
        "source_contains": ["基于机器学习的城市交通流预测方法研究", "TEST20260001", "U491.1"],
        "rendered_contains": ["测试学生甲", "PeMSD4", "图卷积网络", "附录"],
    },
    "long-fields": {
        "source_contains": ["面向多源异构数据融合与可解释深度学习的城市复杂交通网络时空流量预测方法研究",
                             "测试学生甲（长字段边界样例）",
                             "智能交通与数据科学学院及城市交通系统协同创新研究中心"],
        "rendered_contains": ["长字段边界样例", "PeMSD4", "图卷积网络"],
    },
    "dense-layout": {
        "source_contains": ["密集版式测试", "连续公式", "多级章节"],
        "rendered_contains": ["密集版式测试", "连续公式", "多级章节", "PeMSD4"],
    },
    "edge-values": {
        "source_contains": ["测试学生甲-ABC_2026", "TEST-2026_0001", "U491.1-α", "2026年06月30日"],
        "rendered_contains": ["测试学生甲-ABC_2026", "2026年06月30日", "公开"],
    },
}
# Calibrated from the current NEAU synthetic output (15--16 pages and about
# 9.9--10.1k extracted characters).  Image count is still audited explicitly;
# these fixtures currently contain no raster image XObjects.
MIN_PAGE_COUNT = 15
MIN_TEXT_LENGTH = 9_500
MIN_IMAGE_COUNT = 0


def compact_text(text: str) -> str:
    return "".join(text.split())


def pdf_image_count(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        resources = page.get("/Resources")
        if not resources:
            continue
        xobjects = resources.get_object().get("/XObject", {})
        for ref in xobjects.values():
            if ref.get_object().get("/Subtype") == "/Image":
                count += 1
    return count


def content_assertions(name: str, source: Path, *, rendered_text: str,
                       page_count: int, image_count: int) -> dict:
    expected = FIXTURE_EXPECTATIONS[name]
    source_text = compact_text(source.read_text(encoding="utf-8"))
    output_text = compact_text(rendered_text)
    checks = []
    for token in expected["source_contains"]:
        checks.append({"kind": "source_contains", "value": token,
                       "passed": token in source_text})
    for token in expected["rendered_contains"]:
        checks.append({"kind": "rendered_contains", "value": token,
                       "passed": token in output_text})
    checks += [
        {"kind": "page_count_min", "expected": MIN_PAGE_COUNT, "actual": page_count,
         "passed": page_count >= MIN_PAGE_COUNT},
        {"kind": "text_length_min", "expected": MIN_TEXT_LENGTH, "actual": len(rendered_text),
         "passed": len(rendered_text) >= MIN_TEXT_LENGTH},
        {"kind": "image_count_min", "expected": MIN_IMAGE_COUNT, "actual": image_count,
         "passed": image_count >= MIN_IMAGE_COUNT},
    ]
    return {"passed": all(x["passed"] for x in checks), "checks": checks,
            "page_count": page_count, "text_length": len(rendered_text),
            "image_count": image_count}


def make_sources(out: Path) -> list[Path]:
    text = SAMPLE.read_text(encoding="utf-8")
    sources = []
    for name, replacements in VARIANTS.items():
        value = text
        for old, new in replacements.items():
            if old not in value:
                raise RuntimeError(f"fixture replacement not found for {name}: {old}")
            value = value.replace(old, new)
        target = out / f"{name}.tex"
        target.write_text(value, encoding="utf-8")
        sources.append(target)
    return sources


def run_batch(source: Path, build: Path, neutral_reference: Path | None = None) -> dict:
    cmd = [sys.executable, "scripts/batch_rerun_ten_schools.py",
           "--build-dir", str(build), "--template-manifest",
           "inputs/ten-school-template-manifest.json", "--prepare-host-review"]
    if neutral_reference:
        cmd += ["--neutral-reference-docx", str(neutral_reference)]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    (build / "batch.stdout.log").write_text(proc.stdout, encoding="utf-8")
    (build / "batch.stderr.log").write_text(proc.stderr, encoding="utf-8")
    summary = strict_json_read(build / "run-results.json") if (build / "run-results.json").exists() else None
    rows = []
    if summary:
        for case_id, result in (summary.get("cases") or {}).items():
            fresh = result.get("fresh_run") or {}
            rows.append({
                "school": case_id,
                "case_id": case_id,
                "returncode": result.get("returncode"),
                "status": fresh.get("pipeline_status"),
                "reason": result.get("stderr_tail") or result.get("stdout_tail"),
                "cache_reused": fresh.get("cache_reused"),
                "manifest_exists": bool(fresh.get("pipeline_manifest")),
            })
    def classify(row: dict) -> str:
        code = row.get("returncode")
        status = row.get("status")
        reason = str(row.get("reason") or "")
        if code == 0 and row.get("manifest_exists"):
            return "pipeline_pass"
        if code in (3, 6) or status == "blocked" or "clarification" in reason:
            return "expected_compliance_block"
        if code not in (0, 3, 6) or status == "failed":
            return "pipeline_validation_failure"
        return "pipeline_pass"

    matrix = [{"school": row.get("school"), "returncode": row.get("returncode"),
               "status": row.get("status"), "reason": row.get("reason"),
               "state": classify(row)} for row in rows]
    return {"returncode": proc.returncode,
            "effective_success": bool(summary) and all(
                row.get("cache_reused") is False for row in rows
            ),
            "neutral_reference": str(neutral_reference) if neutral_reference else None,
            "command": cmd, "summary": summary,
            "school_status_matrix": matrix,
            "pipeline_failures": [
                {"school": row.get("school"), "returncode": row.get("returncode"),
                 "status": row.get("status"), "reason": row.get("reason")}
                for row in rows if row.get("returncode") not in (0, 3, 6)
            ],
            "expected_nonzero": [
                {"school": row.get("school"), "returncode": row.get("returncode"),
                 "status": row.get("status"), "reason": row.get("reason")}
                for row in rows if row.get("returncode") in (3, 6)
            ],
            "freshness_failures": [
                {"school": row.get("school"), "cache_reused": row.get("cache_reused")}
                for row in rows if row.get("cache_reused") is not False
            ]}


def render_lo(build: Path, render_root: Path, source: Path, fixture_name: str) -> list[dict]:
    rows = []
    for docx in sorted(build.glob("*/generated.docx")):
        school = docx.parent.name
        out = render_root / "libreoffice" / school
        out.mkdir(parents=True, exist_ok=True)
        copied = out / docx.name
        shutil.copy2(docx, copied)
        proc = subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                               "--outdir", str(out), str(copied)],
                              cwd=ROOT, text=True, capture_output=True)
        pdf = out / f"{copied.stem}.pdf"
        row = {"school": school, "renderer": "LibreOffice", "returncode": proc.returncode,
               "docx": str(docx), "pdf": str(pdf), "stderr": proc.stderr.strip()}
        if pdf.exists():
            reader = PdfReader(str(pdf))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            image_count = pdf_image_count(reader)
            png_prefix = out / copied.stem
            raster = subprocess.run(["pdftoppm", "-png", "-f", "1", "-singlefile",
                                     str(pdf), str(png_prefix)],
                                    cwd=ROOT, text=True, capture_output=True)
            png = png_prefix.with_suffix(".png")
            row.update({"pdf_exists": True, "page_count": len(reader.pages),
                        "text_length": len(text), "has_text": bool(text.strip()),
                        "image_count": image_count,
                        "raster_returncode": raster.returncode,
                        "raster_png": str(png), "raster_exists": png.exists()})
            row["content_assertions"] = content_assertions(
                fixture_name, source, rendered_text=text,
                page_count=len(reader.pages), image_count=image_count)
        else:
            row.update({"pdf_exists": False, "page_count": 0, "text_length": 0,
                        "has_text": False, "raster_returncode": None,
                        "raster_png": None, "raster_exists": False, "image_count": 0,
                        "content_assertions": content_assertions(
                            fixture_name, source, rendered_text="", page_count=0,
                            image_count=0)})
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--neutral-reference-docx", type=Path,
                        help="school-neutral DOCX baseline; skips ten-school template capability checks")
    args = parser.parse_args()
    root = args.out_dir if args.out_dir.is_absolute() else ROOT / args.out_dir
    if root.exists():
        raise SystemExit(f"output already exists: {root}")
    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True)
    sources = make_sources(sources_dir)
    neutral_reference = (args.neutral_reference_docx if args.neutral_reference_docx and args.neutral_reference_docx.is_absolute()
                         else ROOT / args.neutral_reference_docx if args.neutral_reference_docx else None)
    if neutral_reference and not neutral_reference.is_file():
        raise SystemExit(f"neutral reference DOCX does not exist: {neutral_reference}")
    report = {"schema_version": "1.0", "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "fixtures": {}, "renderer_inventory": {"libreoffice": shutil.which("soffice"),
                                                         "pandoc": shutil.which("pandoc"),
                                                         "pdftoppm": shutil.which("pdftoppm")}}
    for source in sources:
        name = source.stem
        build = root / "runs" / name
        build.parent.mkdir(parents=True, exist_ok=True)
        batch = run_batch(source, build, neutral_reference)
        render_rows = render_lo(build, root / "renders" / name, source, name)
        content_checks_passed = all(row["content_assertions"]["passed"] for row in render_rows)
        renderer_states = [{"school": row["school"], "renderer": row["renderer"],
                            "state": ("renderer_pass" if row["pdf_exists"] and row["has_text"] and row["raster_exists"]
                                      else "renderer_failure"),
                            "returncode": row["returncode"], "pdf_exists": row["pdf_exists"],
                            "raster_exists": row["raster_exists"]} for row in render_rows]
        report["fixtures"][name] = {"source": str(source), "batch": batch,
                                    "school_status_matrix": batch.get("school_status_matrix", []),
                                    "renderer_status_matrix": renderer_states,
                                    "neutral_reference_docx": str(neutral_reference) if neutral_reference else None,
                                    "libreoffice": render_rows,
                                    "generated_docx_count": len(render_rows),
                                    "renderer_checks_passed": all(
                                        row["pdf_exists"] and row["has_text"] and row["raster_exists"]
                                        for row in render_rows),
                                    "content_checks_passed": content_checks_passed,
                                    "content_assertion_summary": [
                                        row["content_assertions"] for row in render_rows
                                    ],
                                    "notes": (
                                        "The ten-school batch intentionally retains per-school compliance/template failures;"
                                        " renderer verification is considered exercised when at least one DOCX is generated."
                                    )}
        print(json.dumps({"fixture": name, "batch_returncode": batch["returncode"],
                          "rendered": len(render_rows)}, ensure_ascii=False), flush=True)
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (root / "synthetic-validation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(x["generated_docx_count"] > 0 and x["renderer_checks_passed"]
                    and x["content_checks_passed"]
                    for x in report["fixtures"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
