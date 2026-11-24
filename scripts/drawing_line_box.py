"""Conservative inline line-box observations, never rendered layout acceptance."""
from __future__ import annotations
from typing import Any

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'


def _tag(namespace: str, local: str) -> str:
    return f'{{{namespace}}}{local}'


def inline_drawing_geometry(paragraph: Any) -> dict[str, Any]:
    heights, issues = [], []
    inline_count = anchored_count = 0
    for drawing in paragraph.iter(_tag(W, 'drawing')):
        owner = next((p for p in drawing.iterancestors() if p.tag == _tag(W, 'p')), None)
        if owner is not paragraph:
            continue  # A nested textbox drawing belongs to its own paragraph.
        frames = [n for n in drawing if n.tag in {_tag(WP, 'inline'), _tag(WP, 'anchor')}]
        if len(frames) != 1:
            issues.append('drawing has no unique inline or anchored frame')
            continue
        frame = frames[0]
        if frame.tag == _tag(WP, 'anchor'):
            anchored_count += 1
            continue
        inline_count += 1
        extents, effects = frame.findall(_tag(WP, 'extent')), frame.findall(_tag(WP, 'effectExtent'))
        try:
            if len(extents) != 1 or len(effects) > 1:
                raise ValueError('non-unique bounds')
            height, width = (int(extents[0].get(key)) for key in ('cy', 'cx'))
            if height <= 0 or width <= 0:
                raise ValueError('nonpositive extent')
            top, bottom = (int(effects[0].get(key)) for key in ('t', 'b')) if effects else (0, 0)
            if top < 0 or bottom < 0:
                raise ValueError('negative effect bound')
        except (TypeError, ValueError):
            issues.append(f'inline object {inline_count} has unobservable extent or effect bounds')
            continue
        heights.append(height + top + bottom)
    maximum = max(heights, default=0)
    return {
        'inline_count': inline_count, 'anchored_count': anchored_count,
        'height_emu': heights, 'height_pt': [value / 12700.0 for value in heights],
        'minimum_twips': (maximum + 634) // 635 if maximum else None,
        'status': 'unverified' if issues else ('observed' if inline_count else 'not_applicable'),
        'issues': issues,
    }


def effective_line_spacing(paragraph: Any, styles_root: Any) -> dict[str, Any]:
    """Resolve paragraph/style/default attributes; keep relative units separate."""
    issues = []

    def unique(nodes: list[Any], label: str) -> Any:
        if len(nodes) > 1:
            issues.append(f'duplicate {label}')
            return None
        return nodes[0] if nodes else None

    def reference(node: Any, label: str) -> str | None:
        if node is None:
            return None
        value = node.get(_tag(W, 'val'))
        if not value or not value.strip():
            issues.append(f'empty {label} reference')
            return None
        return value

    styles = {}
    for node in styles_root.findall(_tag(W, 'style')):
        styles.setdefault(node.get(_tag(W, 'styleId')), []).append(node)
    ppr = unique(paragraph.findall(_tag(W, 'pPr')), 'paragraph properties')
    pstyle = unique(ppr.findall(_tag(W, 'pStyle')) if ppr is not None else [], 'paragraph style')
    if pstyle is None:
        defaults = [node for nodes in styles.values() for node in nodes
                    if node.get(_tag(W, 'type')) == 'paragraph'
                    and node.get(_tag(W, 'default')) in {'1', 'true', 'on'}]
        default = unique(defaults, 'default paragraph style')
        sid = default.get(_tag(W, 'styleId')) if default is not None else None
        if default is not None and (not sid or not sid.strip()):
            issues.append('default paragraph style has no usable ID')
    else:
        sid = reference(pstyle, 'paragraph style')
    sources = [('paragraph', ppr)]
    seen, unresolved = set(), False
    while sid:
        if sid in seen or sid not in styles:
            unresolved = True
            break
        seen.add(sid)
        style = unique(styles[sid], 'referenced style ID')
        if style is None or style.get(_tag(W, 'type')) != 'paragraph':
            issues.append('referenced style is not a unique paragraph style')
            break
        sources.append((sid, unique(style.findall(_tag(W, 'pPr')), 'style paragraph properties')))
        parent = unique(style.findall(_tag(W, 'basedOn')), 'base style')
        sid = reference(parent, 'base style')
    defaults = unique(styles_root.findall(
        f'{_tag(W, "docDefaults")}/{_tag(W, "pPrDefault")}/{_tag(W, "pPr")}'),
        'default paragraph properties')
    sources.append(('docDefaults', defaults))
    values, owners = {}, {}
    for owner, ppr in sources:
        node = unique(ppr.findall(_tag(W, 'spacing')), f'{owner} spacing') if ppr is not None else None
        if node is None:
            continue
        for key in ('line', 'lineRule'):
            raw = node.get(_tag(W, key))
            if raw is not None and key not in values:
                values[key], owners[key] = raw, owner
    rule = values.get('lineRule', 'auto')
    result = {'rule': {'atLeast': 'at_least'}.get(rule, rule), 'value_pt': None,
              'multiple': None, 'line_twips': None, 'owner': owners,
              'status': 'observed', 'issues': issues}
    try:
        if issues:
            raise ValueError('malformed spacing inheritance')
        if unresolved:
            raise ValueError('unresolved style inheritance')
        if rule not in {'auto', 'exact', 'atLeast'}:
            raise ValueError('unknown line rule')
        line = int(values.get('line', '240' if rule == 'auto' else None))
        if line <= 0:
            raise ValueError('nonpositive line spacing')
        if rule == 'auto':
            result['multiple'] = line / 240.0
        else:
            result['line_twips'], result['value_pt'] = line, line / 20.0
    except (TypeError, ValueError) as exc:
        result['status'] = 'unverified'
        result['issues'].append(str(exc))
    return result


def line_box_assessment(geometry: dict[str, Any], spacing: dict[str, Any]) -> str:
    if geometry['status'] == 'not_applicable':
        return 'not_applicable'
    if geometry['status'] == 'unverified' or spacing['status'] == 'unverified':
        return 'unverified'
    if spacing['rule'] == 'exact' and spacing['line_twips'] < geometry['minimum_twips']:
        return 'restrictive_exact'
    return 'no_static_clipping_proven'
