"""Make the standard delimiter-growth default explicit, without editing math.

ISO/IEC 29500 defaults an omitted dPr/grow to true for delimiters (but not
for n-ary operators). Some exporters fail to honor that default. This small
compatibility normalization is reversible; it is not evidence that a native
exporter honored it. Explicit author choices, fonts, shape, and math stay put.
"""
from __future__ import annotations

import hashlib
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree


def canonical(node):
    return etree.tostring(node, method="c14n")


def digest(node):
    return hashlib.sha256(canonical(node)).hexdigest()


def tall_delimiters(node):
    """Only a matrix/array with more than one row establishes this scope."""
    return [d for d in node.iter(qn("m:d")) if
            any(len(m.findall(qn("m:mr"))) > 1 for m in d.iter(qn("m:m"))) or
            any(len(a.findall(qn("m:e"))) > 1 for a in d.iter(qn("m:eqArr")))]


def materialize_growth(source):
    """Return a copy; do not override grow=0, shape, or any other property."""
    result = etree.fromstring(canonical(source), etree.XMLParser(resolve_entities=False, no_network=True))
    for delimiter in tall_delimiters(result):
        pr = delimiter.find(qn("m:dPr"))
        if pr is None:
            pr = OxmlElement("m:dPr"); delimiter.insert(0, pr)
        if pr.find(qn("m:grow")) is not None:
            continue
        grow = OxmlElement("m:grow"); grow.set(qn("m:val"), "1")
        # CT_DPr order: begChr, sepChr, endChr, grow, shp, ctrlPr.
        position = next((i for i, child in enumerate(pr)
                         if child.tag in {qn("m:shp"), qn("m:ctrlPr")}), len(pr))
        pr.insert(position, grow)
    return result


def proof(source, actual, math_index):
    record = {"kind": "delimiter_growth_defaults", "math_index": math_index,
              "source_sha256": digest(source), "target_sha256": digest(actual),
              "source_math_xml": canonical(source).decode("utf-8"),
              "font_size_changed": False, "math_content_changed": False,
              "native_export_verified": False}
    if not validates(record, actual):
        raise ValueError("delimiter normalization does not exactly replay")
    return record


def validates(record, actual):
    try:
        if record.get("kind") != "delimiter_growth_defaults":
            return False
        source = etree.fromstring(record["source_math_xml"].encode(),
                                  etree.XMLParser(resolve_entities=False, no_network=True))
        return (digest(source) == record["source_sha256"]
                and digest(actual) == record["target_sha256"]
                and canonical(materialize_growth(source)) == canonical(actual))
    except (KeyError, TypeError, ValueError, etree.XMLSyntaxError):
        return False


def restore(record, actual):
    """Invert only this exact authorized edit; reject altered cells/properties."""
    if not validates(record, actual):
        raise ValueError("cannot restore an altered compatibility candidate")
    restored = etree.fromstring(canonical(actual))
    source = etree.fromstring(record["source_math_xml"].encode())
    old = tall_delimiters(source); new = tall_delimiters(restored)
    for original, candidate in zip(old, new):
        old_pr = original.find(qn("m:dPr")); pr = candidate.find(qn("m:dPr"))
        if old_pr is None or old_pr.find(qn("m:grow")) is None:
            pr.remove(pr.find(qn("m:grow")))
            if old_pr is None:
                candidate.remove(pr)
    if canonical(restored) != canonical(source):
        raise ValueError("inverse did not reconstruct the exact original math XML")
    return restored
