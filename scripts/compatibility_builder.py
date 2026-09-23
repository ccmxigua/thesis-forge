#!/usr/bin/env python3
"""Build a fail-closed WPS/Microsoft Word compatible editing edition.

The source is the profile-built CAU trimmed master (or an explicitly supplied
trimmed master).  MACROBUTTON fields are flattened to their cached display text,
or to an explicitly declared replacement.  External linked fields are likewise
reduced to cached content, floating DrawingML is converted to inline DrawingML,
and WPS-only custom XML is removed.  Required fixed declaration blocks must be
canonical-identical before and after the transformation.

The command emits two outputs: the compatible editing DOCX and a Word
finalization manifest.  The manifest deliberately sets submission_ready=false;
only the trusted Microsoft Word render/export receipt and the independent
submission audit may make that claim.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from ooxml_compatibility import NS, W, WP, audit_docx, sha256
from template_builder import build_template
from template_profile import compile_profile, load_profile
from semantic_contract import strict_json_read

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
EXTERNAL_FIELD = re.compile(r"^\s*(?:INCLUDEPICTURE|INCLUDETEXT|LINK|DDE|DDEAUTO)\b", re.I)
MACROBUTTON = re.compile(r"^\s*MACROBUTTON\s+(\S+)(?:\s+(.*?))?\s*$", re.I | re.S)
BRACKET_TEXT = re.compile(r"[\[\"“]([^\]\"”]+)[\]\"”]")


def _canonical_sha(node: etree._Element) -> str:
    return hashlib.sha256(etree.tostring(node, method="c14n")).hexdigest()


def _fixed_hashes(root: etree._Element, compiled: dict[str, Any]) -> dict[str, str]:
    result = {}
    for fixed_id, item in compiled.get("fixed_text_evidence", {}).items():
        if not item.get("required"):
            continue
        locators = item.get("locators", [])
        if len(locators) != 1:
            raise ValueError(f"required fixed block {fixed_id!r} lacks one compiled locator")
        expected = locators[0]["ooxml_sha256"]
        matches = [node for node in root.xpath("//w:p | //w:tbl", namespaces=NS)
                   if _canonical_sha(node) == expected]
        if len(matches) != 1:
            raise ValueError(f"source no longer contains canonical fixed block {fixed_id!r}")
        result[fixed_id] = expected
    return result


def _field_kind(node: etree._Element) -> str | None:
    # A parent container may contain many unrelated descendant fields.  Only a
    # field marker in this direct child belongs to the sibling range currently
    # being scanned.
    values = node.xpath("./w:fldChar/@w:fldCharType", namespaces=NS)
    return values[0] if values else None


def _display_nodes(children: list[etree._Element], separate: int | None, end: int) -> list[etree._Element]:
    if separate is None:
        return []
    result = []
    for child in children[separate + 1:end]:
        clone = copy.deepcopy(child)
        for field_node in clone.xpath(".//w:fldChar | .//w:instrText", namespaces=NS):
            parent = field_node.getparent()
            if parent is not None:
                parent.remove(field_node)
        if clone.xpath(".//w:t | .//w:drawing | .//w:pict | self::w:hyperlink", namespaces=NS):
            result.append(clone)
    return result


def _replacement_text(instruction: str, display: str, replacements: dict[str, str]) -> str:
    normalized = " ".join(instruction.split())
    match = MACROBUTTON.match(instruction)
    macro = match.group(1) if match else ""
    for key in (instruction.strip(), normalized, macro):
        if key and key in replacements:
            return str(replacements[key])
    if display.strip():
        return display
    if match:
        tail = match.group(2) or ""
        bracket = BRACKET_TEXT.search(tail)
        if bracket:
            return bracket.group(1).strip()
        # Historical CAU templates mix ASCII '[' with full-width '］'.
        # Treat the remainder after an opening bracket as declared prompt text.
        for opener in ("[", "［", '"', "“"):
            if opener in tail:
                prompt = tail.split(opener, 1)[1].rstrip("]］\"” ").strip()
                if prompt:
                    return prompt
        # Some official templates contain a bare MACROBUTTON marker with no
        # cached result and no prompt text.  Keeping the live macro is the
        # least compatible option; flatten it to empty static content and
        # record that replacement in the manifest for downstream review.
        return ""
    # A malformed linked field with no cached result is safer as empty static
    # content than as a live network/file reference. Embedded package images,
    # if any, are separate runs and are not removed by this fallback.
    if EXTERNAL_FIELD.match(instruction):
        return ""
    raise ValueError(f"field has neither display text nor declared replacement: {instruction.strip()!r}")


def _text_run(text: str, template: etree._Element | None) -> etree._Element:
    run = copy.deepcopy(template) if template is not None else etree.Element(f"{{{W}}}r")
    for child in list(run):
        if child.tag != f"{{{W}}}rPr":
            run.remove(child)
    value = etree.SubElement(run, f"{{{W}}}t")
    if text[:1].isspace() or text[-1:].isspace():
        value.set(XML_SPACE, "preserve")
    value.text = text
    return run


def _flatten_complex_fields(root: etree._Element, replacements: dict[str, str]) -> list[dict[str, Any]]:
    changed = []
    for parent in list(root.iter()):
        children = list(parent)
        index = 0
        while index < len(children):
            if _field_kind(children[index]) != "begin":
                index += 1
                continue
            depth = 1; cursor = index + 1; separate = None
            while cursor < len(children):
                kind = _field_kind(children[cursor])
                if kind == "begin":
                    depth += 1
                elif kind == "end":
                    depth -= 1
                    if depth == 0:
                        break
                elif kind == "separate" and depth == 1:
                    separate = cursor
                cursor += 1
            if depth != 0:
                # Some Word fields cross a hyperlink/container boundary.  They
                # are outside this sibling-local transformer; leave them for a
                # later parent scan and, ultimately, the fail-closed audit.
                index += 1
                continue
            instruction = "".join(
                text or "" for child in children[index:cursor + 1]
                for text in child.xpath("./w:instrText/text()", namespaces=NS)
            )
            should_flatten = bool(MACROBUTTON.match(instruction) or EXTERNAL_FIELD.match(instruction))
            if not should_flatten:
                index = cursor + 1
                continue
            display_nodes = _display_nodes(children, separate, cursor)
            display = "".join(
                text or "" for child in display_nodes
                for text in child.xpath(".//w:t/text()", namespaces=NS)
            )
            declared = any(key in replacements for key in
                           (instruction.strip(), " ".join(instruction.split()),
                            MACROBUTTON.match(instruction).group(1) if MACROBUTTON.match(instruction) else ""))
            replacement = _replacement_text(instruction, display, replacements)
            if declared or not display_nodes:
                template_run = next((child for child in children[index:cursor + 1]
                                     if child.tag == f"{{{W}}}r"), None)
                display_nodes = [_text_run(replacement, template_run)]
            insertion = parent.index(children[index])
            for child in children[index:cursor + 1]:
                parent.remove(child)
            for offset, child in enumerate(display_nodes):
                parent.insert(insertion + offset, child)
            changed.append({"instruction": instruction.strip(), "replacement_text": replacement,
                            "replacement_source": "declared" if declared else
                                                  ("cached_display" if display else "instruction_prompt")})
            children = list(parent)
            index = insertion + len(display_nodes)
    return changed


def _flatten_cross_container_fields(root: etree._Element, replacements: dict[str, str]) -> list[dict[str, Any]]:
    """Flatten fields whose markers are split across hyperlinks/other containers."""
    changed = []
    for paragraph in root.xpath("//w:p", namespaces=NS):
        while True:
            runs = paragraph.xpath(".//w:r", namespaces=NS)
            target = None
            for instruction_run in runs:
                instruction = "".join(instruction_run.xpath(".//w:instrText/text()", namespaces=NS))
                if MACROBUTTON.match(instruction) or EXTERNAL_FIELD.match(instruction):
                    target = (instruction_run, instruction)
                    break
            if target is None:
                break
            instruction_run, instruction = target
            instruction_index = runs.index(instruction_run)
            begin = None; depth = 0
            for pos in range(instruction_index, -1, -1):
                kinds = runs[pos].xpath("./w:fldChar/@w:fldCharType", namespaces=NS)
                if "end" in kinds:
                    depth += 1
                if "begin" in kinds:
                    if depth == 0:
                        begin = pos; break
                    depth -= 1
            if begin is None:
                raise ValueError(f"MACROBUTTON/external field has no begin marker: {instruction!r}")
            end = None; separate = None; depth = 0
            for pos in range(begin + 1, len(runs)):
                kinds = runs[pos].xpath("./w:fldChar/@w:fldCharType", namespaces=NS)
                if "begin" in kinds:
                    depth += 1
                if "separate" in kinds and depth == 0:
                    separate = pos
                if "end" in kinds:
                    if depth == 0:
                        end = pos; break
                    depth -= 1
            if end is None:
                if EXTERNAL_FIELD.match(instruction) and separate is not None:
                    # The official CAU master contains several truncated
                    # INCLUDEPICTURE fields ending at the separator. Remove the
                    # live field shell; there is no cached result to preserve.
                    end = separate
                else:
                    raise ValueError(f"MACROBUTTON/external field has no end marker: {instruction!r}")
            display_runs = [copy.deepcopy(run) for run in runs[(separate + 1 if separate is not None else end):end]]
            display = "".join(text or "" for run in display_runs
                              for text in run.xpath(".//w:t/text()", namespaces=NS))
            replacement = _replacement_text(instruction, display, replacements)
            keys = (instruction.strip(), " ".join(instruction.split()),
                    MACROBUTTON.match(instruction).group(1) if MACROBUTTON.match(instruction) else "")
            declared = any(key and key in replacements for key in keys)
            if declared or not display_runs:
                display_runs = [_text_run(replacement, runs[begin])]
            begin_run = runs[begin]; insertion_parent = begin_run.getparent()
            insertion = insertion_parent.index(begin_run)
            for run in runs[begin:end + 1]:
                parent = run.getparent()
                if parent is not None:
                    parent.remove(run)
            for offset, run in enumerate(display_runs):
                insertion_parent.insert(insertion + offset, run)
            changed.append({"instruction": instruction.strip(), "replacement_text": replacement,
                            "replacement_source": "declared" if declared else
                                                  ("cached_display" if display else "instruction_prompt")})
    return changed


def _flatten_simple_fields(root: etree._Element, replacements: dict[str, str]) -> list[dict[str, Any]]:
    changed = []
    for field in list(root.xpath("//w:fldSimple", namespaces=NS)):
        instruction = field.get(f"{{{W}}}instr", "")
        if not (MACROBUTTON.match(instruction) or EXTERNAL_FIELD.match(instruction)):
            continue
        display = "".join(field.xpath(".//w:t/text()", namespaces=NS))
        replacement = _replacement_text(instruction, display, replacements)
        parent = field.getparent()
        if parent is None:
            continue
        at = parent.index(field)
        display_nodes = [copy.deepcopy(child) for child in field]
        declared = instruction.strip() in replacements or " ".join(instruction.split()) in replacements
        if declared or not display_nodes:
            display_nodes = [_text_run(replacement, next(iter(field), None))]
        parent.remove(field)
        for offset, child in enumerate(display_nodes):
            parent.insert(at + offset, child)
        changed.append({"instruction": instruction.strip(), "replacement_text": replacement,
                        "replacement_source": "declared" if declared else "cached_display"})
    return changed


def _anchors_to_inline(root: etree._Element) -> int:
    count = 0
    anchor_only = {"simplePos", "positionH", "positionV", "wrapNone", "wrapSquare", "wrapTight",
                   "wrapThrough", "wrapTopAndBottom"}
    for anchor in root.xpath("//wp:anchor", namespaces=NS):
        anchor.tag = f"{{{WP}}}inline"
        kept = {key: value for key, value in anchor.attrib.items()
                if etree.QName(key).localname in {"distT", "distB", "distL", "distR"}}
        anchor.attrib.clear(); anchor.attrib.update(kept)
        for child in list(anchor):
            if etree.QName(child).localname in anchor_only:
                anchor.remove(child)
        count += 1
    return count


def _remove_wps_custom_xml(members: dict[str, bytes]) -> list[str]:
    removed = []
    wps_parts = set()
    for name, payload in members.items():
        if name.startswith("customXml/") and b"www.wps.cn" in payload.lower():
            wps_parts.add(name)
            if name.startswith("customXml/item") and name.endswith(".xml"):
                stem = name[:-4]
                wps_parts.add(f"customXml/_rels/{Path(stem).name}.xml.rels")
    for name in sorted(wps_parts):
        if name in members:
            removed.append(name); members.pop(name)
    for name in list(members):
        if not name.endswith(".rels"):
            continue
        root = etree.fromstring(members[name])
        changed = False
        base = Path(name).parent
        for rel in list(root):
            target = rel.get("Target", "")
            resolved = str((base / target).as_posix()).replace("_rels/../", "")
            if any(part.endswith(target.lstrip("../")) for part in wps_parts) or "item1.xml" in target and any("item1.xml" in x for x in wps_parts):
                root.remove(rel); changed = True
        if changed:
            members[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if "[Content_Types].xml" in members:
        root = etree.fromstring(members["[Content_Types].xml"])
        changed = False
        for item in list(root):
            part_name = item.get("PartName", "").lstrip("/")
            if part_name in wps_parts:
                root.remove(item); changed = True
        if changed:
            members["[Content_Types].xml"] = etree.tostring(root, xml_declaration=True,
                                                             encoding="UTF-8", standalone=True)
    return removed


def _remove_external_relationships(members: dict[str, bytes]) -> list[dict[str, str]]:
    removed = []
    for name in list(members):
        if not name.endswith(".rels"):
            continue
        root = etree.fromstring(members[name]); changed = False
        for rel in list(root):
            rel_type = rel.get("Type", "").lower().rstrip("/").rsplit("/", 1)[-1]
            if rel.get("TargetMode", "").lower() == "external" and rel_type != "hyperlink":
                removed.append({"part": name, "id": rel.get("Id", ""), "target": rel.get("Target", "")})
                root.remove(rel); changed = True
        if changed:
            members[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return removed


def _word_story_parts(members: dict[str, bytes]) -> list[str]:
    """Return document/header/footer XML parts that may contain live fields."""
    return sorted(name for name in members
                  if name == "word/document.xml"
                  or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name))


def _normalize_declared_fonts(members: dict[str, bytes]) -> list[str]:
    """Declare every concrete font referenced anywhere in the Word package.

    Pandoc contributes fonts such as Consolas/Symbol while an official cover
    template may use localized aliases such as SimSun/SimHei.  A merged DOCX
    must keep those references declared in ``fontTable.xml`` for deterministic
    Word/WPS substitution; merely inheriting the template table is not enough.
    """
    font_part = "word/fontTable.xml"
    if font_part not in members:
        raise ValueError("word/fontTable.xml is required for compatibility normalization")
    roots = []
    for name, payload in members.items():
        if name.startswith("word/") and name.endswith(".xml"):
            roots.append(etree.fromstring(payload))
    referenced: set[str] = set()
    for root in roots:
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            referenced.update(root.xpath(f"//w:rFonts/@w:{attr}", namespaces=NS))
    font_root = etree.fromstring(members[font_part])
    declared = set(font_root.xpath("//w:font/@w:name", namespaces=NS))
    added = sorted(font for font in referenced
                   if font and not font.startswith("+") and font not in declared)
    for font in added:
        etree.SubElement(font_root, f"{{{W}}}font", {f"{{{W}}}name": font})
    if added:
        members[font_part] = etree.tostring(font_root, xml_declaration=True,
                                             encoding="UTF-8", standalone=True)
    return added


def build_compatibility_edition(profile_path: Path, output: Path, manifest: Path, *,
                                source: Path | None = None, replacements: dict[str, str] | None = None,
                                work_dir: Path | None = None) -> dict[str, Any]:
    profile_path = profile_path.resolve(); output = output.resolve(); manifest = manifest.resolve()
    if output == manifest or source and output == source.resolve():
        raise ValueError("source, compatibility output, and manifest must be distinct paths")
    compiled = compile_profile(profile_path)
    generated_source = False
    if source is None:
        generated_source = True
        work_dir = (work_dir or output.parent).resolve(); work_dir.mkdir(parents=True, exist_ok=True)
        source = work_dir / "cau-official-trimmed-master.docx"
        build_template(profile_path, source)
    source = source.resolve()
    if not source.is_file():
        raise ValueError(f"trimmed master does not exist: {source}")
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = etree.fromstring(members["word/document.xml"])
    fixed_before = _fixed_hashes(root, compiled)
    changes = []
    anchor_count = 0
    for part in _word_story_parts(members):
        story_root = root if part == "word/document.xml" else etree.fromstring(members[part])
        part_changes = _flatten_complex_fields(story_root, replacements or {})
        part_changes.extend(_flatten_cross_container_fields(story_root, replacements or {}))
        part_changes.extend(_flatten_simple_fields(story_root, replacements or {}))
        for change in part_changes:
            change["part"] = part
        changes.extend(part_changes)
        anchor_count += _anchors_to_inline(story_root)
        if part != "word/document.xml":
            members[part] = etree.tostring(story_root, xml_declaration=True,
                                             encoding="UTF-8", standalone=True)
    for fixed_id, expected in fixed_before.items():
        count = sum(_canonical_sha(node) == expected for node in root.xpath("//w:p | //w:tbl", namespaces=NS))
        if count != 1:
            raise ValueError(f"compatibility transform would alter fixed declaration block {fixed_id!r}")
    members["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    added_fonts = _normalize_declared_fonts(members)
    removed_wps = _remove_wps_custom_xml(members)
    removed_external = _remove_external_relationships(members)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    audit = audit_docx(output)
    audit_path = manifest.with_name(manifest.stem + "-compatibility-audit.json")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not audit["compatible"]:
        output.unlink(missing_ok=True)
        raise ValueError(f"compatibility output failed static audit; see {audit_path}")
    result = {
        "schema_version": "1.0", "contract": "wps_word_dual_output_v1",
        "profile_id": load_profile(profile_path)["profile_id"],
        "source": {"path": str(source), "sha256": sha256(source), "generated": generated_source},
        "compatibility_editing_edition": {"path": str(output), "sha256": sha256(output),
                                           "static_compatibility_audit": str(audit_path),
                                           "static_compatibility_passed": True},
        "transformations": {"flattened_fields": changes, "floating_drawings_inlined": anchor_count,
                            "font_declarations_added": added_fonts,
                            "wps_parts_removed": removed_wps,
                            "external_relationships_removed": removed_external,
                            "fixed_declaration_ooxml_preserved": sorted(fixed_before)},
        "word_finalization": {
            "status": "pending_trusted_word_render", "submission_ready": False,
            "claim_policy": "submission_ready MUST remain false until a trusted Microsoft Word export receipt is verified and submission_audit.py passes on the final post-Word DOCX.",
            "required_command": "python3 scripts/word_render_export.py COMPAT.docx FINAL.docx FINAL.pdf --report WORD-RENDER.json",
            "required_follow_up": "python3 scripts/submission_audit.py FINAL.docx --render-report WORD-RENDER.json --out SUBMISSION-AUDIT.json",
            "final_docx": None, "render_receipt": None, "submission_audit": None,
        },
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, help="existing profile-built trimmed master")
    parser.add_argument("--replacements", type=Path, help="JSON object keyed by exact instruction or macro name")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args(argv)
    replacements = strict_json_read(args.replacements) if args.replacements else {}
    if not isinstance(replacements, dict) or not all(isinstance(v, str) for v in replacements.values()):
        parser.error("--replacements must contain a JSON object of string values")
    try:
        result = build_compatibility_edition(args.profile, args.output, args.manifest, source=args.source,
                                             replacements=replacements, work_dir=args.work_dir)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
