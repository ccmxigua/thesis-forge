"""Reversible, declared boundary punctuation edits without rebuilding runs."""
from __future__ import annotations

import hashlib
import re
from lxml import etree
from word_reference_inventory import W, roots, boundary_range_proof


def _digest(node):
    return hashlib.sha256(etree.tostring(node, method='c14n', exclusive=True)).hexdigest()


def _nodes(paragraph):
    ppr=paragraph.findall(W+'pPr')
    if len(ppr)>1 or (ppr and paragraph[0] is not ppr[0]):
        raise ValueError('paragraph property boundary is malformed')
    markers = {W+k for k in ('bookmarkStart','bookmarkEnd','commentRangeStart','commentRangeEnd')}
    if any(n.tag not in markers | {W+'pPr',W+'r'} for n in paragraph):
        raise ValueError('boundary edit needs direct text runs and stable range markers')
    for run in paragraph.findall(W+'r'):
        if any(n.tag not in {W+'rPr',W+'t',W+'commentReference'} for n in run):
            raise ValueError('boundary has a printed/unknown non-text token')
    return [n for run in paragraph.findall(W+'r') for n in run.findall(W+'t') if n.text]


def stage_boundary_edit(doc, paragraph, clone, rule):
    """Return a minimal edit record and independent range-consumer proof.

    The source paragraph is never mutated. Inverting the single w:t change
    must exactly restore the staged paragraph, including non-text children.
    """
    original = _nodes(paragraph._p); staged = _nodes(clone)
    if not original or len(original) != len(staged):
        raise ValueError('boundary text-node identity unresolved')
    if [(n.text,dict(n.attrib)) for n in original] != [(n.text,dict(n.attrib)) for n in staged]:
        raise ValueError('staged text/attributes changed before the authorized boundary edit')
    source_text = ''.join(n.text or '' for n in original)
    if 'display_prefix' in rule:
        prefix = rule['display_prefix']
        if not source_text.strip() or source_text[0].isspace():
            raise ValueError('empty/leading-space subtitle boundary unresolved')
        if source_text.startswith(prefix*2):
            raise ValueError('duplicate decorative prefix retained')
        if source_text.startswith(prefix):
            return None
        index = 0; before = original[index].text; after = prefix+before
        kind = 'decorative_prefix'
    else:
        config = rule['terminal']
        if not set(config['accepted']+[config['value']]) <= {'.','。','．'}:
            raise ValueError('boundary policy supports terminal full-stop variants only')
        if re.search(r'https?://|www\.|\bdoi\b|10\.\d{4,9}/', source_text, re.I):
            raise ValueError('URL/DOI-bearing reference retained')
        old = next((t for t in config['accepted'] if source_text.endswith(t)), None)
        if old is None:
            raise ValueError('actual terminal full stop not proved')
        if old == config['value']:
            return None
        index = len(original)-1; before = original[index].text
        if not before.endswith(old):
            raise ValueError('terminal token identity unresolved')
        after = before[:-len(old)]+config['value']; kind = 'terminal_full_stop'
    proof = boundary_range_proof(doc, paragraph, original[index])
    previous = _digest(clone)
    staged[index].text = after
    result = _digest(clone)
    if any(n.text != (after if i == index else original[i].text) or dict(n.attrib) != dict(original[i].attrib)
           for i,n in enumerate(staged)):
        raise ValueError('text difference set exceeds the exact boundary ledger')
    staged[index].text = before
    if _digest(clone) != previous:
        raise ValueError('boundary inverse did not preserve the complete subtree')
    staged[index].text = after
    return {'kind':kind, 'part':str(paragraph.part.partname),
            'text_node_path':original[index].getroottree().getpath(original[index]),
            'source_paragraph_path':paragraph._p.getroottree().getpath(paragraph._p),
            'before':before, 'after':after, 'paragraph_before_sha256':previous,
            'paragraph_after_sha256':result, 'inverse_verified':True,
            'range_proof':proof}


def apply_text_records(doc, records, *, inverse=False):
    """Apply an exact receipt to an in-memory document, with no partial writes."""
    bypart = dict(roots(doc)); planned = []; seen = set()
    for record in records:
        root = bypart.get(record['part'])
        if root is None:
            raise ValueError('text receipt part absent')
        path = record.get('final_text_node_path',record['text_node_path']) if inverse else record['text_node_path']
        found = root.getroottree().xpath(path, namespaces={k:v for k,v in root.nsmap.items() if k})
        if len(found) != 1 or found[0].tag != W+'t' or found[0] in seen:
            raise ValueError('text receipt node absent/ambiguous/repeated')
        node = found[0]; seen.add(node)
        before,after = (record['after'],record['before']) if inverse else (record['before'],record['after'])
        if node.text != before:
            raise ValueError('text receipt current bytes differ; no inverse guessing')
        planned.append((node,after))
    for node,after in planned:
        node.text = after
