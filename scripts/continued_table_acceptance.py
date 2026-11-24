"""Read-only, target-app-scoped review of one numbered, continued DOCX table."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from PIL import Image
from semantic_contract import strict_json_read

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NUMBER = r"([0-9]+(?:\s*[-.－–]\s*[0-9]+)*)"
BASE = re.compile(r"^表\s*" + NUMBER + r"(?=$|\s|[^0-9A-Za-z.\-–])")
PREFIX = re.compile(r"^续表\s*" + NUMBER + r"(?=$|\s|[^0-9A-Za-z.\-–])")
SUFFIX = re.compile(r"\(\s*续\s*\)$")
PLACEHOLDER = re.compile(r"^(?:[×xX*＊…·_—\-]+|占位(?:符|文本)?|待填(?:写)?|示例(?:文字|文本)?|n/?a)$", re.I)
CHECKS = {"same_logical_table", "header_repeated", "header_is_not_placeholder",
          "caption_on_continuation_page"}


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def text(element):
    return " ".join(unicodedata.normalize("NFKC", "".join(
        t.text or "" for t in element.iter(W + "t"))).split())


def caption(node):
    if node is None or node.tag != W + "p" or any(
        item.tag in {W + "del", W + "ins", W + "drawing", W + "fldSimple", W + "instrText", W + "tbl"}
        for item in node.iter()
    ):
        return None
    s = text(node)
    for prefix in (PREFIX, BASE):
        m = prefix.match(s)
        if m:
            # Never accept decimal/prose prefixes as a different table number.
            if re.match(r"\s*[-.–]\s*[\dA-Za-z]", s[m.end():]):
                return None
            number = re.sub(r"\s+", "", m.group(1)).replace("–", "-")
            return ("continuation" if prefix is PREFIX or SUFFIX.search(s) else "original", number)
    return None


def plain_blank(node):
    # Do not skip native break, field, non-text or ambiguous content.
    return node.tag == W + "p" and all(i.tag in {W + "p", W + "pPr", W + "r", W + "rPr", W + "t"} for i in node.iter()) and not text(node)


def preceding(nodes, index):
    index -= 1
    while index >= 0 and plain_blank(nodes[index]):
        index -= 1
    return index


def header_flags(rows):
    top = 0
    ignored = []
    ambiguous = []
    for i, row in enumerate(rows, 1):
        properties = row.findall(W + "trPr")
        tags = [el for prop in properties for el in prop.findall(W + "tblHeader")]
        value = tags[0].get(W + "val", "true") if len(tags) == 1 else ""
        if len(properties) > 1 or (tags and (len(tags) != 1 or value not in {"0","1","true","false","off","on"})):
            ambiguous.append(i)
        if len(tags) == 1 and value in {"1", "true", "on"}:
            if i == top + 1:
                top += 1
            else:
                ignored.append(i)
    return top, ignored, ambiguous


def cells(row):
    # Manual role comparison cannot safely flatten fields, merges, nested
    # tables, edits or omitted grid columns into an equivalent header.
    nonplain = {W+i for i in ("tbl","drawing","object","del","ins","sdt",
                              "gridBefore","gridAfter","gridSpan","vMerge","hMerge",
                              "fldSimple","fldChar","instrText","tab","br")}
    if any(e.tag in nonplain for e in row.iter()):
        return None
    return tuple(text(cell) for cell in row.findall(W + "tc"))


def usable(line):
    return bool(line and all(v and not PLACEHOLDER.fullmatch(v) for v in line)
                and (len(line) == 1 or len(set(line)) > 1))


def continuation_geometry_diagnostic(table_spec):
    """A separate diagnostic; never fed to structural validity or release."""
    contract = table_spec.get("continuation") if isinstance(table_spec, dict) else None
    if not isinstance(contract, dict) or not contract:
        return None
    return {"status": "pending_native_geometry", "blocking": False,
            "native_observation_verified": False, "release_verified": False,
            "host_provenance_authenticated": False,
            "caption_suffix": contract.get("caption_suffix"),
            "verification": contract.get("verification"),
            "reason": "requires exact-candidate target-app verification through the existing native/visual receipt gate"}


def valid_capture(blob):
    if not isinstance(blob, bytes) or not blob or len(blob) > 32_000_000:
        return False
    try:
        with Image.open(BytesIO(blob)) as picture:
            if picture.format not in {"PNG", "JPEG"} or min(picture.size) < 200 or picture.width * picture.height > 32_000_000:
                return False
            picture.verify()
        # Verify decode too: JPEG verify() may not reject truncation.
        with Image.open(BytesIO(blob)) as picture:
            picture.load()
            if "A" in picture.getbands() or "transparency" in picture.info:
                rgba = picture.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, "white")
                canvas.alpha_composite(rgba)
                picture = canvas
            preview = picture.convert("L").resize((128, 128))
            histogram = preview.histogram()
        # A decoded but blank page, or a tiny stray mark, is not usable.
        # This is quality control only; pixels cannot authenticate observer claims.
        lo = next(i for i in range(256) if sum(histogram[:i+1]) >= 80)
        hi = next(i for i in reversed(range(256)) if sum(histogram[i:]) >= 80)
        return hi - lo >= 24
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return False


def native_match(e, captures, app, candidate_hash, table_ordinal):
    if not isinstance(e, dict) or e.get("app") != app or e.get("candidate_sha256") != candidate_hash or type(e.get("table_ordinal")) is not int or e["table_ordinal"] != table_ordinal:
        return False
    if any(not isinstance(e.get(k), str) or not e[k].strip() for k in
           ("native_document_ref", "native_document_version", "review_ref")):
        return False
    checks = e.get("checks")
    if not isinstance(checks, dict) or any(checks.get(k) is not True for k in CHECKS):
        return False
    pages = e.get("pages")
    if not isinstance(pages, list) or len(pages) != 2 or len(captures) != 2:
        return False
    prior, next_page = pages
    if not all(isinstance(p, dict) and type(p.get("physical_page")) is int and p["physical_page"] >= 1
               and isinstance(p.get("capture_ref"), str) and p["capture_ref"].strip() for p in pages):
        return False
    if next_page["physical_page"] != prior["physical_page"] + 1 or prior["capture_ref"] == next_page["capture_ref"]:
        return False
    if prior.get("original_segment_visible") is not True or next_page.get("continuation_segment_visible") is not True:
        return False
    if not all(valid_capture(b) for b in captures):
        return False
    hashes = [sha(b) for b in captures]
    return hashes[0] != hashes[1] and hashes == [p.get("capture_sha256") for p in pages]


def permission_match(permission, school_rules, candidate_hash, table_ordinal):
    return (isinstance(permission, dict) and isinstance(school_rules, bytes) and bool(school_rules)
            and permission.get("allow_manual") is True
            and permission.get("requirements_sha256") == sha(school_rules)
            and permission.get("candidate_sha256") == candidate_hash
            and type(permission.get("table_ordinal")) is int
            and permission["table_ordinal"] == table_ordinal
            and all(isinstance(permission.get(k), str) and permission[k].strip()
                    for k in ("clause_ref", "review_ref")))


def audit_continued_table(candidate: bytes, table_ordinal: int, *, target_app: str, strategy: str,
                          manual_permission: dict | None = None, school_rules: bytes | None = None,
                          evidence: dict | None = None,
                          page_captures: tuple[bytes, ...] = ()) -> dict:
    """Return evidence status; never modify a source, run, or release ledger."""
    if target_app not in {"wps","word"} or strategy not in {"automatic","manual"}:
        raise ValueError("explicit named target app and continuation strategy required")
    if type(table_ordinal) is not int or table_ordinal < 1:
        raise ValueError("positive one-based top-level table ordinal required")
    with ZipFile(BytesIO(candidate)) as package:
        members = [entry for entry in package.infolist() if entry.filename == "word/document.xml"]
        if len(members) != 1 or members[0].file_size > 64_000_000:
            raise ValueError("expected one supported document.xml member")
        xml = ET.fromstring(package.read(members[0]))
    body = xml.find(W + "body")
    if xml.tag != W + "document" or body is None:
        raise ValueError("expected a transitional OOXML document body")
    nodes = list(body)
    positions = [i for i, item in enumerate(nodes) if item.tag == W + "tbl"]
    if table_ordinal > len(positions):
        raise ValueError("table ordinal outside known top-level document tables")
    current = positions[table_ordinal - 1]
    rows = nodes[current].findall(W + "tr")
    top, ignored, ambiguous = header_flags(rows)
    cap_i = preceding(nodes, current)
    marker = caption(nodes[cap_i]) if cap_i >= 0 else None
    structural = {"table_ordinal": table_ordinal, "caption_kind": marker[0] if marker else None,
                  "caption_number": marker[1] if marker else None,
                  "top_repeating_row_count": top, "ignored_noncontiguous_flag_rows": ignored,
                  "ambiguous_flag_rows": ambiguous, "predecessor_table_ordinal": None,
                  "first_row_relationship": "unbound"}
    out = {"status": "hold_structure", "structural": structural, "strategy": strategy,
           "target_app": target_app, "candidate_sha256": sha(candidate), "release_verified": False,
           "host_provenance_authenticated": False, "native_observation_verified": False,
           "physical_pagination_inferred_from_ooxml": False}
    if ambiguous or not rows or not marker or any(
        c.tag not in {W + "tblPr", W + "tblGrid", W + "tr"} for c in nodes[current]):
        return out
    if strategy == "automatic":
        if marker[0] != "original" or not top or len(rows) <= top or not all(usable(cells(row)) for row in rows[:top]):
            return out
        structural["first_row_relationship"] = "top_contiguous_ooxml_header"
    else:
        if marker[0] != "continuation" or len(rows) < 2:
            return out
        before = preceding(nodes, cap_i)
        chain = []
        while before >= 0 and before in positions:
            original_rows = nodes[before].findall(W + "tr")
            if len(original_rows) < 2 or any(c.tag not in {W + "tblPr", W + "tblGrid", W + "tr"} for c in nodes[before]):
                return out
            chain.append((before, cells(original_rows[0])))
            earlier_cap = preceding(nodes, before)
            earlier = caption(nodes[earlier_cap]) if earlier_cap >= 0 else None
            if not earlier or earlier[1] != marker[1]:
                return out
            if earlier[0] == "original":
                break
            before = preceding(nodes, earlier_cap)
        else:
            return out
        structural["predecessor_table_ordinal"] = positions.index(chain[0][0]) + 1
        first = cells(rows[0])
        if not usable(first) or not all(usable(line) for _, line in chain):
            structural["first_row_relationship"] = "placeholder_or_non_distinct"
            return out
        if any(first != line for _, line in chain):
            structural["first_row_relationship"] = "differs_from_preceding_table"
            return out
        structural["first_row_relationship"] = "copied_from_preceding_table"
        if not permission_match(manual_permission, school_rules, out["candidate_sha256"], table_ordinal):
            out["status"] = "hold_school_rule"
            return out
        out["host_school_permission"] = {k:manual_permission[k] for k in
                                          ("requirements_sha256", "clause_ref", "review_ref")}
    out["status"] = "pending_native_evidence"
    if evidence is not None:
        if native_match(evidence, page_captures, target_app, out["candidate_sha256"], table_ordinal):
            out["status"] = "host_evidence_bound_unverified"
            out["host_evidence"] = {k:evidence[k] for k in ("native_document_ref", "native_document_version", "review_ref", "pages")}
        else:
            out["status"] = "hold_native_evidence"
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("candidate", type=Path)
    p.add_argument("--table", type=int, required=True)
    p.add_argument("--target-app", choices=("wps","word"), required=True)
    p.add_argument("--strategy", choices=("automatic","manual"), required=True)
    p.add_argument("--manual-permission", type=Path)
    p.add_argument("--school-rules", type=Path)
    p.add_argument("--evidence", type=Path)
    p.add_argument("--page-capture", type=Path, action="append", default=[])
    a = p.parse_args()
    evidence = strict_json_read(a.evidence) if a.evidence else None
    captures = tuple(path.read_bytes() for path in a.page_capture)
    result = audit_continued_table(a.candidate.read_bytes(), a.table, target_app=a.target_app,
                                    strategy=a.strategy,
                                    manual_permission=strict_json_read(a.manual_permission) if a.manual_permission else None,
                                    school_rules=a.school_rules.read_bytes() if a.school_rules else None,
                                    evidence=evidence, page_captures=captures)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A supporting packet is not independently verified in this command.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
