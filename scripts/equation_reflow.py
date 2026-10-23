"""Reversible layout of strictly flat additive expressions.

This is deliberately not a general algebra parser. Structured math, authored
alignment/breaks, literal prose, and ambiguous operators require an author.
The source XML plus operator offsets reconstruct the exact permitted output;
a token bag or an ignored-math-properties hash cannot authorize this change.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

# This grammar has no precedence or grouping decision to make. Splits are
# before existing binary '+' characters, with every character kept in order.
_ATOM = r"(?:[^\W\d]\w*|\d+(?:\.\d+)?)"
_SPACE = r"[ ]*"
_SUM = re.compile(rf"{_SPACE}(?:{_ATOM}{_SPACE}={_SPACE})?{_ATOM}(?:{_SPACE}\+{_SPACE}{_ATOM})+{_SPACE}\Z")
_MAX_ATTEMPTS = 5
_MAX_ROWS = 24


def canonical(node):
    return etree.tostring(node, method="c14n")


def digest(node):
    return hashlib.sha256(canonical(node)).hexdigest()


def _flat_parts(source):
    if source.tag == qn("m:oMathPara"):
        math_nodes = source.findall(qn("m:oMath"))
        if len(math_nodes) != 1 or any(n.tag not in {qn("m:oMathParaPr"), qn("m:oMath")} for n in source):
            return None
        math_node = math_nodes[0]
    elif source.tag == qn("m:oMath"):
        math_node = source
    else:
        return None
    runs = list(math_node)
    if not runs or (math_node.text and math_node.text.strip()):
        return None
    values = []
    for run in runs:
        if run.tag != qn("m:r") or run.attrib:
            return None
        if ((run.text and run.text.strip()) or (run.tail and run.tail.strip())
                or any(child.tail and child.tail.strip() for child in run)):
            return None
        if any(n.tag not in {qn("m:rPr"), qn("w:rPr"), qn("m:t")} for n in run):
            return None
        texts = run.findall(qn("m:t"))
        if len(texts) != 1 or not texts[0].text or len(texts[0]):
            return None
        props = run.find(qn("m:rPr"))
        if props is not None and any(n.tag not in {qn("m:scr"), qn("m:sty")} for n in props):
            return None
        if run.find(".//"+qn("w:rPrChange")) is not None:
            return None
        values.append(texts[0].text)
    text = "".join(values)
    if not _SUM.fullmatch(text):
        return None
    return math_node, runs, values, text


def candidate_breaks(source, natural_width_pt, target_width_pt):
    """Bounded proposals only; actual rendering decides whether any fits."""
    flat = _flat_parts(source)
    if (flat is None or not math.isfinite(natural_width_pt) or natural_width_pt <= 0
            or not math.isfinite(target_width_pt) or target_width_pt <= 0):
        return
    text = flat[3]
    available = [match.start() for match in re.finditer(r"\+", text)]
    first_count = max(2, math.ceil(natural_width_pt / target_width_pt))
    last_count = min(len(available)+1, _MAX_ROWS, first_count+_MAX_ATTEMPTS-1)
    for count in range(first_count, last_count+1):
        selected = []; start = 0; next_index = 0
        for row in range(count-1):
            remaining_rows = count-row
            maximum_index = len(available)-(remaining_rows-1)
            choices = range(next_index, maximum_index+1)
            ideal = start+(len(text)-start)/remaining_rows
            chosen = min(choices, key=lambda index: abs(available[index]-ideal))
            start = available[chosen]; selected.append(start); next_index = chosen+1
        yield selected


def reflow(source, breaks):
    """Build only a continuation array from validated additive operator cuts."""
    flat = _flat_parts(source)
    if flat is None:
        raise ValueError("not a supported flat additive expression")
    _, _, _, text = flat
    allowed = {m.start() for m in re.finditer(r"\+", text)}
    if (not isinstance(breaks, list) or not breaks or len(breaks) >= _MAX_ROWS
            or any(type(offset) is not int or offset not in allowed for offset in breaks)
            or breaks != sorted(set(breaks))):
        raise ValueError("breaks must be ordered original binary plus offsets")
    # Materialize the full source namespace context before adding a layout
    # container. A detached deepcopy can discard unused ancestor bindings,
    # which would make an exact proof depend on where it was validated.
    result = etree.fromstring(canonical(source), etree.XMLParser(resolve_entities=False, no_network=True))
    target, runs, values, _ = _flat_parts(result)
    array = OxmlElement("m:eqArr")
    boundaries = [0, *breaks, len(text)]
    offsets = []; total = 0
    for value in values:
        offsets.append(total); total += len(value)
    for begin, end in zip(boundaries, boundaries[1:]):
        row = OxmlElement("m:e")
        for run, value, offset in zip(runs, values, offsets):
            lo = max(begin, offset); hi = min(end, offset+len(value))
            if lo < hi:
                fragment = copy.deepcopy(run)
                fragment.find(qn("m:t")).text = value[lo-offset:hi-offset]
                row.append(fragment)
        array.append(row)
    for run in runs:
        target.remove(run)
    target.append(array)
    if "".join(n.text or "" for n in result.iter(qn("m:t"))) != text:
        raise ValueError("reflow did not preserve the exact ordered math text")
    return result


def proof(source, result, breaks, math_index):
    """Use the original namespace context captured before unlinking source."""
    record = {"kind": "flat_additive_continuation", "math_index": math_index,
              "source_sha256": digest(source), "target_sha256": digest(result),
              "source_math_xml": canonical(source).decode("utf-8"),
              "break_offsets": list(breaks), "rows": len(breaks)+1,
              "font_size_changed": False, "operators_added_or_removed": False}
    if not validates(record, result):
        raise ValueError("proposed math layout does not exactly replay from its source")
    return record


def validates(record, actual):
    """A precise replay, never a blanket exception to math integrity checks."""
    if record.get("kind") == "delimiter_growth_defaults":
        from equation_compat import validates as validates_compatibility
        return validates_compatibility(record, actual)
    try:
        if record.get("kind") != "flat_additive_continuation":
            return False
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        source = etree.fromstring(record["source_math_xml"].encode("utf-8"), parser)
        if digest(source) != record["source_sha256"]:
            return False
        expected = reflow(source, record["break_offsets"])
        return (digest(actual) == record["target_sha256"]
                and canonical(expected) == canonical(actual))
    except (KeyError, TypeError, ValueError, etree.XMLSyntaxError):
        return False


def integrity_matches(source_hashes, actual_nodes, repairs):
    if len(source_hashes) != len(actual_nodes):
        return False
    proofs = {}
    for record in repairs:
        index = record.get("math_index")
        if type(index) is not int or index in proofs or not 0 <= index < len(source_hashes):
            return False
        if source_hashes[index] != record.get("source_sha256"):
            return False
        proofs[index] = record
    for index, (original, actual) in enumerate(zip(source_hashes, actual_nodes)):
        if index in proofs:
            if not validates(proofs[index], actual):
                return False
        elif digest(actual) != original:
            return False
    return True
