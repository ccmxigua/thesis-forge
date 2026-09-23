#!/usr/bin/env python3
"""Update a DOCX in Microsoft Word, export PDF, and emit render evidence.

macOS Word is intentionally driven in two stages: Launch Services opens the
document (avoiding Word's blocking AppleScript ``open`` command), then a small
AppleScript updates fields/TOC, saves the final DOCX in place, and exports PDF.
The final report is produced by ``word_render_validate.py`` and is bound to the
post-Word DOCX bytes, not to the pre-render generator output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

from render_attestation import load_key, sign
from artifact_io import commit_files, paths_alias, sibling_temp
from process_runner import run_process
from semantic_contract import strict_json_dumps, strict_json_loads, strict_json_read

ROOT = Path(__file__).resolve().parents[1]
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
W = f"{{{W_NS}}}"


def repair_parallel_toc_targets(
    docx: Path, *, target_map: dict[str, str] | None = None,
) -> int:
    """Repair stale TOC targets only from an explicit trusted target map.

    Some official bilingual templates contain stale HYPERLINK fields whose
    bookmarks no longer exist.  Word then renders ``Error! Bookmark not
    defined`` instead of rebuilding those links.  This function accepts only
    a source-derived one-to-one map from the exact stale HYPERLINK target to
    its intended bookmark.

    Entry order is not evidence that two TOC entries have the same semantic
    target.  Without an explicit map this function is deliberately a no-op;
    PAGEREF fields are handled separately after Word updates the document,
    and the normal render validator remains responsible for failing closed on
    unresolved fields.
    """
    with ZipFile(docx) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = etree.fromstring(members["word/document.xml"])
    bookmarks = {
        node.get(W + "name")
        for node in root.xpath(".//w:bookmarkStart", namespaces=NS)
        if node.get(W + "name")
    }
    # This pre-update repair is intentionally limited to hyperlink fields.
    # PAGEREF fields have cached page-number result text and are repaired by
    # ``repair_post_update_pageref_targets`` after Word updates the document.
    # Treating both field families as one positional sequence would count and
    # rewrite unrelated fields from the same stale map.
    pattern = re.compile(r"\bHYPERLINK\s+\\l\s+([^\s\\]+)", re.I)
    fields: list[tuple[etree._Element, re.Match[str]]] = []
    for node in root.xpath(".//w:instrText", namespaces=NS):
        match = pattern.search("".join(node.itertext()))
        if match:
            fields.append((node, match))

    if not target_map:
        return 0
    replacements: list[tuple[etree._Element, re.Match[str], str]] = []
    for node, match in fields:
        old_target = match.group(1)
        if old_target in bookmarks:
            continue
        new_target = target_map.get(old_target)
        if not new_target or new_target not in bookmarks:
            return 0
        replacements.append((node, match, new_target))

    repaired = 0
    for node, match, target in replacements:
        text = "".join(node.itertext())
        node.text = text[:match.start(1)] + target + text[match.end(1):]
        repaired += 1

    if not repaired:
        return 0
    members["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    replacement = docx.with_name(f".{docx.name}.toc-repair")
    with ZipFile(replacement, "w", ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    replacement.replace(docx)
    return repaired


def _complex_field_result_nodes(instruction: etree._Element) -> list[etree._Element]:
    """Return text nodes between a complex field's separate/end markers."""
    paragraph = instruction
    while paragraph is not None and paragraph.tag != W + "p":
        paragraph = paragraph.getparent()
    if paragraph is None:
        return []
    descendants = list(paragraph.iter())
    try:
        start = descendants.index(instruction)
    except ValueError:
        return []
    in_result = False
    result: list[etree._Element] = []
    for node in descendants[start + 1:]:
        if node.tag == W + "fldChar":
            marker_type = node.get(W + "fldCharType")
            if marker_type == "separate":
                in_result = True
            elif marker_type == "end":
                return result
        elif in_result and node.tag == W + "t":
            result.append(node)
    return []


def repair_post_update_pageref_targets(
    docx: Path, *, target_map: dict[str, dict[str, str]] | None = None,
) -> int:
    """Repair invalid PAGEREFs only from an explicit trusted target map.

    Updating two TOCs can leave a manually translated TOC between Word's two
    regenerated caches.  Word creates fresh bookmarks for the regenerated
    TOCs but the translated cache retains deleted targets.  A missing field is
    repaired only when the caller supplies its exact target and cached page
    text; no sequence length, entry order, or neighbouring field is treated as
    semantic evidence.  The supplied Word-calculated result text is copied so
    the second export need not update fields again.
    """
    with ZipFile(docx) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = etree.fromstring(members["word/document.xml"])
    bookmarks = {
        node.get(W + "name")
        for node in root.xpath(".//w:bookmarkStart", namespaces=NS)
        if node.get(W + "name")
    }
    pattern = re.compile(r"\bPAGEREF\s+([^\s\\]+)", re.I)
    entries: list[tuple[etree._Element, re.Match[str], list[etree._Element]]] = []
    for node in root.xpath(".//w:instrText", namespaces=NS):
        match = pattern.search("".join(node.itertext()))
        if match:
            entries.append((node, match, _complex_field_result_nodes(node)))

    if not target_map:
        return 0
    replacements: list[tuple[etree._Element, re.Match[str], list[etree._Element], str, str]] = []
    for node, match, results in entries:
        old_target = match.group(1)
        if old_target in bookmarks:
            continue
        mapping = target_map.get(old_target)
        if not isinstance(mapping, dict):
            return 0
        target = mapping.get("target")
        cached_text = mapping.get("cached_text")
        if (not isinstance(target, str) or target not in bookmarks
                or not isinstance(cached_text, str) or not results):
            return 0
        replacements.append((node, match, results, target, cached_text))

    repaired = 0
    for node, match, results, target, cached_text in replacements:
        text = "".join(node.itertext())
        node.text = text[:match.start(1)] + target + text[match.end(1):]
        results[0].text = cached_text
        for extra in results[1:]:
            extra.text = ""
        repaired += 1

    if not repaired:
        return 0
    members["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    replacement = docx.with_name(f".{docx.name}.post-update-toc-repair")
    with ZipFile(replacement, "w", ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    replacement.replace(docx)
    return repaired

APPLE_SCRIPT = r'''
on run argv
  set expectedPath to item 1 of argv
  set outPdf to item 2 of argv
  -- Word may legitimately spend several minutes repaginating a thesis and
  -- updating a large TOC.  AppleScript's shorter default AppleEvent timeout
  -- otherwise raises -1712 before the Python-level --word-timeout expires.
  with timeout of 600 seconds
    tell application "Microsoft Word"
    set d to missing value
    set ownsDocument to false
    try
      if (count documents) is 0 then error "No Word document is open."
      set d to active document
      if POSIX full name of d is not expectedPath then error "Unexpected active Word document path: " & (POSIX full name of d)
      set ownsDocument to true
    set storyTypes to {}
    -- Do not update main-story fields one by one.  A generated TOC contains
    -- one PAGEREF field per entry; updating each field independently forces
    -- Word to repaginate the whole thesis repeatedly.  The TOC is updated as
    -- one native unit below, which also refreshes its PAGEREF fields.
    set end of storyTypes to footnotes story
    set end of storyTypes to endnotes story
    set end of storyTypes to comments story
    set end of storyTypes to text frame story
    set end of storyTypes to even pages header story
    set end of storyTypes to primary header story
    set end of storyTypes to even pages footer story
    set end of storyTypes to primary footer story
    set end of storyTypes to first page header story
    set end of storyTypes to first page footer story
    set storyCount to 0
    set fieldCount to 0
    set updatedCount to 0
    set failedCount to 0
    repeat with storyKind in storyTypes
      set storyAvailable to false
      try
        set currentRange to get story range d story type (contents of storyKind)
        set storyAvailable to true
      end try
      if storyAvailable then
        set storyChainCount to 0
        repeat
          set storyChainCount to storyChainCount + 1
          if storyChainCount > 64 then error "Word story-range chain exceeded safety limit."
          set storyCount to storyCount + 1
          set fieldList to every field of currentRange
          set fieldCount to fieldCount + (count fieldList)
          repeat with f in fieldList
            try
              if update field f then
                set updatedCount to updatedCount + 1
              else
                set failedCount to failedCount + 1
              end if
            on error
              set failedCount to failedCount + 1
            end try
          end repeat
          try
            set currentRange to next story range of currentRange
          on error
            set storyAvailable to false
            exit repeat
          end try
          try
            if currentRange is missing value then exit repeat
          on error
            exit repeat
          end try
        end repeat
      end if
    end repeat
    if failedCount is not 0 then error "Failed to update " & (failedCount as text) & " Word field(s)."
    set tocCount to 0
    set tocList to tables of contents of d
    repeat with t in tocList
      set tocCount to tocCount + 1
      try
        update t
      on error errText
        error "Failed to update table of contents: " & errText
      end try
      try
        update page numbers t
      on error errText
        error "Failed to update TOC page numbers: " & errText
      end try
    end repeat
      save d
      -- Word's `save as` dictionary expects a path string here. Wrapping an
      -- already-POSIX string with `POSIX file` can make Word 16.105 dispatch
      -- the command to the document object and fail with AppleEvent -1708.
      save as d file name outPdf file format format PDF
      close d saving no
      return "{" & quote & "story_count" & quote & ":" & (storyCount as text) & "," & quote & "field_count" & quote & ":" & (fieldCount as text) & "," & quote & "updated_count" & quote & ":" & (updatedCount as text) & "," & quote & "failed_count" & quote & ":" & (failedCount as text) & "," & quote & "toc_count" & quote & ":" & (tocCount as text) & "}"
    on error errText number errNumber
      if ownsDocument and d is not missing value then
        try
          close d saving no
        end try
      end if
      error errText number errNumber
    end try
    end tell
  end timeout
end run
'''

CLEANUP_SCRIPT = r'''
on run argv
  set expectedPath to item 1 of argv
  with timeout of 60 seconds
    tell application "Microsoft Word"
      repeat with d in documents
        try
          if POSIX full name of d is expectedPath then close d saving no
        end try
      end repeat
    end tell
  end timeout
end run
'''

EXPORT_ONLY_SCRIPT = r'''
on run argv
  set expectedPath to item 1 of argv
  set outPdf to item 2 of argv
  with timeout of 600 seconds
    tell application "Microsoft Word"
      set d to missing value
      set ownsDocument to false
      try
        if (count documents) is 0 then error "No Word document is open."
        set d to active document
        if POSIX full name of d is not expectedPath then error "Unexpected active Word document path: " & (POSIX full name of d)
        set ownsDocument to true
        save as d file name outPdf file format format PDF
        close d saving no
        return "ok"
      on error errText number errNumber
        if ownsDocument and d is not missing value then
          try
            close d saving no
          end try
        end if
        error errText number errNumber
      end try
    end tell
  end timeout
end run
'''


def run(command: list[str], *, timeout: int, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return run_process(command, cwd=ROOT, timeout=timeout, input_text=input)


def active_document_path() -> str | None:
    result = run([
        "osascript", "-e",
        'tell application "Microsoft Word" to if (count documents) > 0 then return POSIX full name of active document',
    ], timeout=15)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _canonical_path(path: str) -> str:
    """Normalize filesystem aliases used differently by Word and Python.

    On macOS, Word commonly reports ``/tmp/...`` while ``Path.resolve()``
    returns ``/private/tmp/...``. Workspace symlinks can produce the same
    mismatch. They identify the same file and must not make the renderer
    falsely diagnose a blocked open or repair prompt.
    """
    return os.path.normcase(os.path.realpath(path))


def wait_for_document(expected: str, timeout: int) -> str:
    expected_canonical = _canonical_path(expected)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = active_document_path()
        if active and _canonical_path(active) == expected_canonical:
            # Return Word's spelling. AppleScript compares POSIX full name
            # exactly, even when the only difference is /tmp vs /private/tmp.
            return active
        time.sleep(0.5)
    raise RuntimeError(f"Microsoft Word did not activate {expected!r} within {timeout} seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_docx", type=Path)
    parser.add_argument("final_docx", type=Path,
                        help="isolated post-Word DOCX; must differ from input_docx")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--open-timeout", type=int, default=45)
    parser.add_argument("--word-timeout", type=int, default=180)
    parser.add_argument(
        "--staging-dir",
        type=Path,
        help=(
            "use this existing directory inside Word's container tmp as the "
            "staging parent; external directories are rejected"
        ),
    )
    parser.add_argument("--case-id", help="fresh batch case identity bound to this render")
    parser.add_argument("--run-id", help="fresh requirements run identity bound to this render")
    parser.add_argument(
        "--toc-target-map",
        type=Path,
        help="trusted source-derived TOC/PAGEREF target map; omitted means no repair is attempted",
    )
    args = parser.parse_args()

    if platform.system() != "Darwin":
        parser.error("Microsoft Word automation is currently supported on macOS only")
    source = args.input_docx.expanduser().resolve()
    final_docx = args.final_docx.expanduser().resolve()
    pdf = args.pdf.expanduser().resolve()
    report = args.report.expanduser().resolve()
    toc_target_map_path = args.toc_target_map.expanduser().resolve() if args.toc_target_map else None
    if not source.is_file():
        parser.error(f"input DOCX does not exist: {source}")
    target_map: dict[str, Any] | None = None
    if toc_target_map_path is not None:
        if not toc_target_map_path.is_file():
            parser.error(f"TOC/PAGEREF target map does not exist: {toc_target_map_path}")
        try:
            target_map = strict_json_read(toc_target_map_path)
        except (OSError, ValueError) as exc:
            parser.error(f"cannot read TOC/PAGEREF target map: {exc}")
        if not isinstance(target_map, dict):
            parser.error("TOC/PAGEREF target map must be a JSON object")
        expected_source_sha = target_map.get("source_docx_sha256")
        actual_source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_source_sha != actual_source_sha:
            parser.error("TOC/PAGEREF target map is not bound to the input DOCX bytes")
        if not isinstance(target_map.get("parallel"), dict) and not isinstance(target_map.get("pageref"), dict):
            parser.error("TOC/PAGEREF target map must contain parallel and/or pageref mappings")
    if paths_alias((source, final_docx, pdf, report)):
        parser.error("input DOCX, final DOCX, PDF, and report must be four filesystem-distinct paths")
    if source == final_docx:
        parser.error("final DOCX must differ from input DOCX; in-place Word updates are forbidden")
    # Word for macOS may show a blocking “Grant File Access” dialog when an
    # AppleScript export writes outside Word's own sandbox.  /tmp is not
    # sufficient: current Word builds can open a DOCX there but still demand a
    # security-scoped bookmark when `save as ... format PDF` writes beside it.
    # Stage inside Word's container so both open and export are sandbox-native,
    # then atomically commit the validated artifacts to their destinations.
    word_container_tmp = (
        Path.home() / "Library" / "Containers" / "com.microsoft.Word" / "Data" / "tmp"
    ).resolve()
    staging_parent = (
        args.staging_dir.expanduser().resolve()
        if args.staging_dir
        else word_container_tmp
    )
    try:
        staging_parent.relative_to(word_container_tmp)
    except ValueError:
        parser.error(
            "--staging-dir must be Word's container tmp directory or one of "
            f"its descendants: {word_container_tmp}"
        )
    if not staging_parent.is_dir():
        raise SystemExit(
            f"Microsoft Word container staging directory is unavailable: {staging_parent}"
        )
    staging = tempfile.TemporaryDirectory(prefix="thesis-word-render-", dir=staging_parent)
    staging_root = Path(staging.name)
    staged_docx = staging_root / "final-word.docx"
    staged_pdf = staging_root / "final.pdf"
    staged_report = staging_root / "render-report.json"
    shutil.copy2(source, staged_docx)
    toc_target_repairs = repair_parallel_toc_targets(
        staged_docx,
        target_map=(target_map or {}).get("parallel"),
    )

    try:
        # Avoid unnecessarily stealing foreground focus during unattended runs.
        # `-g` opens the document without activating Word's application UI.
        opened = run(["open", "-g", "-a", "Microsoft Word", str(staged_docx)], timeout=30)
        if opened.returncode:
            raise SystemExit(opened.stderr.strip() or "failed to open DOCX in Microsoft Word")
        word_docx_path = wait_for_document(str(staged_docx), args.open_timeout)

        # Pass the AppleScript through stdin while retaining the same owned
        # process-group cleanup as every other renderer subprocess.
        try:
            script = run(
                ["osascript", "-", word_docx_path, str(staged_pdf)],
                input=APPLE_SCRIPT, timeout=args.word_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            run(["osascript", "-", word_docx_path], timeout=15, input=CLEANUP_SCRIPT)
            raise SystemExit(
                "Word export timed out. Check for a macOS/Word 'Grant File Access' dialog, "
                "approve the selected output directory, then retry."
            ) from exc
        if script.returncode == 124:
            run(["osascript", "-", word_docx_path], timeout=15, input=CLEANUP_SCRIPT)
            raise SystemExit(
                "Word export timed out. Check for a macOS/Word 'Grant File Access' dialog, "
                "approve the selected output directory, then retry."
            )
        if script.returncode:
            raise SystemExit(script.stderr.strip() or "Word field update/PDF export failed")
        if not staged_docx.is_file() or not staged_pdf.is_file():
            raise SystemExit("Word returned without creating both staged DOCX and PDF")
        try:
            word_update = strict_json_loads(script.stdout)
        except ValueError as exc:
            raise SystemExit(f"Word returned an invalid update summary: {script.stdout!r}") from exc
        required_summary = {"story_count", "field_count", "updated_count", "failed_count", "toc_count"}
        if (set(word_update) != required_summary or word_update["failed_count"] != 0
                or word_update["updated_count"] != word_update["field_count"]):
            raise SystemExit(f"Word returned an invalid or failed update summary: {word_update!r}")

        post_update_repairs = repair_post_update_pageref_targets(
            staged_docx,
            target_map=(target_map or {}).get("pageref"),
        )
        if post_update_repairs:
            staged_pdf.unlink(missing_ok=True)
            reopened = run(
                ["open", "-g", "-a", "Microsoft Word", str(staged_docx)], timeout=30
            )
            if reopened.returncode:
                raise SystemExit(reopened.stderr.strip() or "failed to reopen repaired DOCX")
            word_docx_path = wait_for_document(str(staged_docx), args.open_timeout)
            try:
                export = run(
                    ["osascript", "-", word_docx_path, str(staged_pdf)],
                    input=EXPORT_ONLY_SCRIPT, timeout=args.word_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                run(["osascript", "-", word_docx_path], timeout=15, input=CLEANUP_SCRIPT)
                raise SystemExit("Word PDF re-export timed out after TOC target repair") from exc
            if export.returncode == 124:
                run(["osascript", "-", word_docx_path], timeout=15, input=CLEANUP_SCRIPT)
                raise SystemExit("Word PDF re-export timed out after TOC target repair")
            if export.returncode:
                raise SystemExit(export.stderr.strip() or "Word PDF re-export failed")
            if not staged_pdf.is_file():
                raise SystemExit("Word returned without recreating the repaired PDF")

        validator = run([
            sys.executable, str(ROOT / "scripts" / "word_render_validate.py"),
            str(staged_docx), str(staged_pdf), "--out", str(staged_report),
        ], timeout=120)
        if validator.returncode != 0:
            diagnostics = report.parent / "diagnostics"
            diagnostics.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_docx, diagnostics / "latest-failed.docx")
            shutil.copy2(staged_pdf, diagnostics / "latest-failed.pdf")
            detail = validator.stderr.strip()
            if staged_report.is_file():
                shutil.copy2(staged_report, diagnostics / "latest-failed-validation.json")
                try:
                    failed_evidence = strict_json_read(staged_report)
                    hits = failed_evidence.get("rendered_pdf", {}).get("error_hits", [])
                    if hits:
                        codes = sorted({hit.get("code", "unknown") for hit in hits})
                        detail = f"render error hits: {len(hits)} ({', '.join(codes)})"
                    elif failed_evidence.get("validation_errors"):
                        detail = "; ".join(map(str, failed_evidence["validation_errors"]))
                except (OSError, ValueError, TypeError):
                    pass
            raise SystemExit(detail or "Word render validation failed")
        evidence = strict_json_read(staged_report)
        if not evidence.get("renderer", {}).get("version"):
            raise SystemExit("Word version discovery failed; refusing to attest export")
        evidence["source_docx"]["path"] = str(final_docx)
        evidence["rendered_pdf"]["path"] = str(pdf)
        evidence["word_export"] = word_update
        evidence["case_id"] = args.case_id
        evidence["run_id"] = args.run_id
        evidence["toc_target_map"] = (
            {"path": str(toc_target_map_path), "sha256": hashlib.sha256(toc_target_map_path.read_bytes()).hexdigest()}
            if toc_target_map_path else None
        )
        evidence["pre_render_repairs"] = {
            "parallel_toc_targets": {
                "count": toc_target_repairs,
                "status": "applied" if toc_target_repairs else "not_attempted_no_trusted_target_map",
            },
            "post_update_pageref_targets": {
                "count": post_update_repairs,
                "status": "applied" if post_update_repairs else "not_attempted_no_trusted_target_map",
            },
            "policy": "no_semantic_target_guessing",
        }
        evidence["attestation"] = {"algorithm": "HMAC-SHA256", "scope": "local_word_export_v1"}
        evidence["attestation"]["signature"] = sign(evidence, load_key(create=True))
        staged_report.write_text(strict_json_dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        commit_files([(staged_docx, final_docx), (staged_pdf, pdf), (staged_report, report)])
    finally:
        try:
            run(["osascript", "-", str(staged_docx)], timeout=15, input=CLEANUP_SCRIPT)
        except (OSError, subprocess.SubprocessError):
            pass
        staging.cleanup()
    summary = {
        "word_update": word_update,
        "final_docx": str(final_docx),
        "pdf": str(pdf),
        "report": str(report),
        "render_validation_returncode": 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if validator.stdout:
        print(validator.stdout, end="" if validator.stdout.endswith("\n") else "\n")
    if validator.stderr:
        print(validator.stderr, file=sys.stderr, end="" if validator.stderr.endswith("\n") else "\n")
    return validator.returncode


if __name__ == "__main__":
    raise SystemExit(main())
