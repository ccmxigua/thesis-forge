#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


XREF_MARKER_FMT = '[[[TJUFE_XREF:{label}|{text}]]]'
EQ_LABEL_MARKER_FMT = '[[[TJUFE_EQLABEL:{label}]]]'
EQ_CONTROL_MARKER_FMT = '[[[TJUFE_EQCONTROL:{mode}:{tag}]]]'
LABEL_MARKER_FMT = 'TJUFE_LABEL__{label}__'
EQUATION_ENVS = (
    'equation', 'equation*',
    'align', 'align*',
    'gather', 'gather*',
    'multline', 'multline*',
    'eqnarray', 'eqnarray*',
)
THEOREM_ENVS = (
    'theorem', 'lemma', 'proposition', 'corollary', 'definition', 'remark',
)
THEOREM_LABELS = {
    'theorem': '定理',
    'lemma': '引理',
    'proposition': '命题',
    'corollary': '推论',
    'definition': '定义',
    'remark': '注',
}

AUX_NEWLABEL_RE = re.compile(r'^\\newlabel\{([^}]+)\}(.*)$')
AUX_ZREF_LABEL_RE = re.compile(r'^\\zref@newlabel\{([^}]+)\}\{(.*)\}$')
AUX_BIBCITE_RE = re.compile(r'^\\bibcite\{([^}]+)\}(.*)$')
GENERIC_LABEL_RE = re.compile(r'\\label\{([^{}]+)\}')
EQ_CONTROL_RE = re.compile(r'^\[\[\[TJUFE_EQCONTROL:(?P<mode>numbered|unnumbered|tag):(?P<tag>[^\]]*)\]\]\]$')
REF_PATTERN = re.compile(r'\\(zcref|cref|Cref|autoref|ref|eqref)\{([^{}]+)\}')
CITE_PATTERN = re.compile(r'\\(cite|citep|citet|citeauthor|citeyear)\{([^{}]+)\}')


LabelInfo = Dict[str, str]


def extract_balanced(text: str, start: int, open_char: str, close_char: str) -> Tuple[str, int]:
    if start >= len(text) or text[start] != open_char:
        raise ValueError(f"expected {open_char!r} at {start}")
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '\\':
            i += 2
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    raise ValueError(f"unbalanced {open_char}{close_char} starting at {start}")


def top_level_groups(text: str) -> List[str]:
    groups: List[str] = []
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] != '{':
            break
        group, i = extract_balanced(text, i, '{', '}')
        groups.append(group[1:-1])
    return groups


def strip_outer_braces(s: str) -> str:
    s = s.strip()
    while len(s) >= 2 and s[0] == '{' and s[-1] == '}':
        s = s[1:-1].strip()
    return s


def unwrap_subfloat(text: str) -> str:
    out: List[str] = []
    i = 0
    needle = '\\subfloat'
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        j = idx + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        optional_caption = ''
        if j < len(text) and text[j] == '[':
            try:
                optional, j = extract_balanced(text, j, '[', ']')
                optional_caption = optional[1:-1].strip()
            except ValueError:
                out.append(needle)
                i = idx + len(needle)
                continue
            while j < len(text) and text[j].isspace():
                j += 1
        if j >= len(text) or text[j] != '{':
            out.append(needle)
            i = idx + len(needle)
            continue
        try:
            body, j = extract_balanced(text, j, '{', '}')
        except ValueError:
            out.append(needle)
            i = idx + len(needle)
            continue
        # The optional sub-caption is part of the source semantics.  Pandoc's
        # LaTeX reader does not understand ``\subfloat`` reliably, so keep a
        # visible, non-destructive caption line instead of silently dropping
        # it.  The main figure caption is still handled by
        # ``inject_caption_label_markers`` later.
        if optional_caption:
            out.append(f'\\textit{{{optional_caption}}}\\par\n')
        out.append(body[1:-1].strip())
        i = j
    return ''.join(out)


def expand_includes(
    text: str,
    source_path: Path,
    stack: Optional[List[Path]] = None,
    encoding: str = 'utf-8',
    dependencies: Optional[List[Path]] = None,
) -> str:
    """Expand a bounded ``\\input``/``\\include`` graph without editing files.

    Pandoc cannot apply this preprocessor's label and opaque-region rules to
    files it discovers later.  Expanding the graph here makes every consumed
    source explicit and lets us reject missing files and include cycles.
    """
    active = list(stack or [])
    source_path = source_path.resolve()
    if source_path in active:
        chain = ' -> '.join(str(item) for item in active + [source_path])
        raise ValueError(f'cyclic TeX include graph: {chain}')
    active.append(source_path)
    if dependencies is not None and source_path not in dependencies:
        dependencies.append(source_path)

    include_re = re.compile(r'\\(?P<command>input|include)(?![A-Za-z])\s*(?:\{(?P<braced>[^{}]+)\}|(?P<plain>[^\s%]+))')

    def repl(match: re.Match[str]) -> str:
        requested = (match.group('braced') or match.group('plain') or '').strip()
        candidate = Path(requested)
        if not candidate.suffix:
            candidate = candidate.with_suffix('.tex')
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f'\\{match.group("command")} file not found: {candidate}')
        try:
            child = candidate.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            raise UnicodeError(f'cannot decode included TeX file as {encoding}: {candidate}') from exc
        # Protect each child before recursively expanding it.  Otherwise an
        # ``\input`` appearing inside a child verbatim/listing/comment block
        # would be executed before the top-level protection pass sees it.
        child, child_opaque = protect_opaque_regions(
            child, token_prefix=f'TJUFE_INCLUDE_OPAQUE_{len(active):03d}'
        )
        expanded = expand_includes(
            child, candidate, active, encoding=encoding, dependencies=dependencies
        )
        return restore_opaque_regions(expanded, child_opaque)

    return include_re.sub(repl, text)


OPAQUE_ENVIRONMENTS = ('verbatim', 'Verbatim', 'lstlisting', 'minted', 'comment')


def protect_opaque_regions(
    text: str, token_prefix: str = 'TJUFE_OPAQUE_REGION'
) -> tuple[str, dict[str, str]]:
    """Protect code/comments from global TeX reference substitutions."""
    spans: list[tuple[int, int]] = []
    for env in OPAQUE_ENVIRONMENTS:
        pattern = re.compile(
            rf'\\begin\{{{re.escape(env)}\}}.*?\\end\{{{re.escape(env)}\}}', re.S
        )
        spans.extend((m.start(), m.end()) for m in pattern.finditer(text))
    verb_re = re.compile(r'\\verb(?P<star>\*)?(?P<delim>[^A-Za-z\s])(?P<body>.*?)(?P=delim)')
    spans.extend((m.start(), m.end()) for m in verb_re.finditer(text))
    comment_re = re.compile(r'(?m)(?<!\\)%[^\n]*')
    spans.extend((m.start(), m.end()) for m in comment_re.finditer(text))

    spans = sorted(spans, key=lambda item: (item[0], -item[1]))
    accepted: list[tuple[int, int]] = []
    for start, end in spans:
        if accepted and start < accepted[-1][1]:
            continue
        accepted.append((start, end))

    protected: dict[str, str] = {}
    out: list[str] = []
    cursor = 0
    for idx, (start, end) in enumerate(accepted):
        token = f'{token_prefix}_{idx:06d}'
        protected[token] = text[start:end]
        out.append(text[cursor:start])
        out.append(token)
        cursor = end
    out.append(text[cursor:])
    return ''.join(out), protected


def restore_opaque_regions(text: str, protected: dict[str, str]) -> str:
    for token, original in protected.items():
        text = text.replace(token, original)
    return text


def replace_braced_macro(text: str, macro: str, repl) -> str:
    out: List[str] = []
    i = 0
    needle = '\\' + macro
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        j = idx + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        if j >= len(text) or text[j] != '{':
            out.append(needle)
            i = idx + len(needle)
            continue
        try:
            group, j = extract_balanced(text, j, '{', '}')
        except ValueError:
            out.append(needle)
            i = idx + len(needle)
            continue
        out.append(repl(group[1:-1]))
        i = j
    return ''.join(out)


def normalize_math_macros(text: str) -> str:
    text = replace_braced_macro(text, 'widebar', lambda body: f'\\overline{{{body}}}')
    text = replace_braced_macro(text, 'textup', lambda body: f'\\mathrm{{{body}}}')
    text = replace_braced_macro(text, 'mathclap', lambda body: body)
    text = re.sub(r'\\overline\{\\rule\{0pt\}\{[^{}]+\}\s*', r'\\overline{', text)
    return text


FORMULA_LAYOUT_RULE_NAMES = (
    'parabolic_square_completion',
    'nonlinear_operator_radical',
)


def _equation_block_transform(text: str, transform) -> Tuple[str, int]:
    """Apply a layout transform only inside numbered equation environments."""
    pattern = re.compile(r'\\begin\{equation\*?\}.*?\\end\{equation\*?\}', re.S)
    hits = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal hits
        original = match.group(0)
        updated = transform(original)
        if updated != original:
            hits += 1
        return updated

    return pattern.sub(repl, text), hits


def apply_formula_layout_overrides(text: str) -> Tuple[str, Dict[str, int]]:
    """Add narrow, evidence-driven continuation rows for audited formulas.

    The historical Word/PDF evidence identified specific display structures
    whose rendered line reached the A4 right edge. Each rule below requires a
    unique mathematical signature, preserves every math token, and only adds
    an aligned continuation row. No equation font size or document-wide style
    is changed here.
    """
    hits: Dict[str, int] = {name: 0 for name in FORMULA_LAYOUT_RULE_NAMES}

    def split_top_level_additions(body: str) -> list[str]:
        """Split a math body at additions outside all delimiters."""
        parts: list[str] = []
        start = 0
        brace_depth = 0
        paren_depth = 0
        bracket_depth = 0
        for index, char in enumerate(body):
            if char == '{':
                brace_depth += 1
            elif char == '}':
                brace_depth -= 1
            elif char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
            elif char == '[':
                bracket_depth += 1
            elif char == ']':
                bracket_depth -= 1
            elif char == '+' and not any((brace_depth, paren_depth, bracket_depth)):
                parts.append(body[start:index].strip())
                start = index + 1
        parts.append(body[start:].strip())
        return parts

    def parabolic_rule(block: str) -> str:
        # Formula (3.127) is a single, unusually wide square-completion
        # display in the source. Split only its four top-level summands and
        # keep every mathematical token unchanged. The aligned rows are
        # deliberately source-level so Pandoc serializes them as OMML rows.
        if r'\begin{aligned}' in block:
            return block
        if (
            r'\sqrt{\mathrm{e}^{y_1}-\theta_1}' not in block
            or r'\sqrt{\mathrm{e}^{y_2}-\theta_1}' not in block
            or r'\rho_1^2 \sigma_1^2' not in block
            or r'\rho_2^2 \sigma_2^2' not in block
            or block.count(r'\right)^2 +') != 2
            or block.count(r'\xi_2^2 + ') != 1
        ):
            return block
        body_start = block.find('\n') + 1
        body_end = block.rfind('\n')
        body = block[body_start:body_end].strip()
        first_sep = body.find(r'\right)^2 +')
        second_sep = body.find(r'\right)^2 +', first_sep + 1)
        third_sep = body.find(r'\xi_2^2 + ')
        if first_sep < 0 or second_sep < 0 or third_sep < 0:
            return block

        square_end = len(r'\right)^2')
        part1 = body[:first_sep + square_end]
        part2 = body[first_sep + len(r'\right)^2 +'):second_sep + square_end]
        part3 = body[second_sep + len(r'\right)^2 +'):third_sep + len(r'\xi_2^2')]
        part4 = body[third_sep + len(r'\xi_2^2 + '):]
        return (
            '\\begin{equation}\n'
            '  \\begin{aligned}\n'
            f'  &{part1}\\\\\n'
            f'  &+ {part2}\\\\\n'
            f'  &+ {part3}\\\\\n'
            f'  &+ {part4}\n'
            '  \\end{aligned}\n'
            '\\end{equation}'
        )

    def nonlinear_operator_rule(block: str) -> str:
        # Formula (3.125) has a long radical whose four top-level terms reach
        # beyond the A4 text width in Word. Keep the outer aligned equation,
        # but place an aligned scaffold inside the radical so the root remains
        # a single mathematical object while its additive terms stay
        # separable. Pandoc serializes this nested scaffold as a five-cell
        # m:m matrix; patch_omml_formula.py converts those four non-empty cells
        # to the final four-row m:eqArr after the Word baseline is selected.
        required = (
            r'\kappa_{\textup{cost}}',
            r'\sqrt{\frac{2}{\pi\delta t}}',
            r'\mathrm{e}^{-y_1}',
            r'\mathrm{e}^{-y_2}',
            r'\rho_1 \sigma_1',
            r'\rho_2 \sigma_2',
        )
        if r'\begin{aligned}' not in block or any(token not in block for token in required):
            return block
        marker = r'\sqrt{\frac{2}{\pi\delta t}}'
        radical_start = block.find(r'\sqrt{', block.find(marker) + len(marker))
        if radical_start < 0:
            return block
        try:
            radical_body, radical_end = extract_balanced(block, radical_start + len(r'\sqrt'), '{', '}')
        except ValueError:
            return block
        parts = split_top_level_additions(radical_body[1:-1])
        if len(parts) != 4 or r'\begin{aligned}' in radical_body:
            return block
        nested = (
            r'\begin{aligned}'
            + '\n'
            + f'&{parts[0]}\\\n'
            + f'&+ {parts[1]}\\\n'
            + f'&+ {parts[2]}\\\n'
            + f'&+ {parts[3]}'
            + '\n'
            + r'\end{aligned}'
        )
        updated = block[:radical_start] + r'\sqrt{' + nested + '}' + block[radical_end:]
        return updated

    text, hits['parabolic_square_completion'] = _equation_block_transform(text, parabolic_rule)
    text, hits['nonlinear_operator_radical'] = _equation_block_transform(text, nonlinear_operator_rule)
    return text, hits


def expand_visible_date_macros(text: str) -> str:
    r"""Turn datetime2 dates into ordinary text before Pandoc sees them.

    Pandoc's LaTeX reader retains ``\\DTMdate{YYYY-MM-DD}`` only as a raw
    LaTeX inline.  The DOCX writer then drops that inline completely, turning
    phrases such as ``从 \\DTMdate{2018-01-01} 到 ...`` into ``从 到 ...``.
    Keeping the macro payload as plain text is lossless for the ISO dates used
    by thesis sources and makes the date visible in every output format.
    """
    return replace_braced_macro(text, 'DTMdate', lambda body: body.strip())


MATH_ENVIRONMENTS = {
    'math', 'displaymath', 'equation', 'equation*', 'align', 'align*',
    'alignat', 'alignat*', 'gather', 'gather*', 'multline', 'multline*',
    'flalign', 'flalign*', 'split', 'aligned', 'alignedat', 'gathered',
    'cases', 'matrix', 'pmatrix', 'bmatrix', 'Bmatrix', 'vmatrix', 'Vmatrix',
}


def unwrap_text_macros_outside_math(text: str) -> str:
    r"""Preserve ``\text{...}`` accidentally used outside math mode.

    Pandoc treats ``\text`` as a math-only command.  In prose it silently
    drops the argument, which previously turned ``“\text{U\_BS}”`` into
    ``“”``.  This scanner unwraps only prose occurrences and deliberately
    leaves every math occurrence untouched.
    """
    out: List[str] = []
    i = 0
    dollar_mode = 0
    command_math: Optional[str] = None
    math_env_depth = 0
    while i < len(text):
        if text[i] == '%' and (i == 0 or text[i - 1] != '\\'):
            end = text.find('\n', i)
            if end == -1:
                out.append(text[i:])
                break
            out.append(text[i:end + 1])
            i = end + 1
            continue

        env_match = re.match(r'\\(begin|end)\{([^{}]+)\}', text[i:])
        if env_match:
            token = env_match.group(0)
            env = env_match.group(2)
            if env in MATH_ENVIRONMENTS:
                if env_match.group(1) == 'begin':
                    math_env_depth += 1
                else:
                    math_env_depth = max(0, math_env_depth - 1)
            out.append(token)
            i += len(token)
            continue

        if text.startswith('\\[', i) or text.startswith('\\(', i):
            command_math = text[i + 1]
            out.append(text[i:i + 2])
            i += 2
            continue
        if ((command_math == '[' and text.startswith('\\]', i)) or
                (command_math == '(' and text.startswith('\\)', i))):
            command_math = None
            out.append(text[i:i + 2])
            i += 2
            continue
        if text[i] == '$' and (i == 0 or text[i - 1] != '\\') and not command_math:
            width = 2 if text.startswith('$$', i) else 1
            if dollar_mode == 0:
                dollar_mode = width
            elif dollar_mode == width:
                dollar_mode = 0
            out.append(text[i:i + width])
            i += width
            continue

        in_math = bool(dollar_mode or command_math or math_env_depth)
        if not in_math and text.startswith('\\text', i):
            j = i + len('\\text')
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == '{':
                try:
                    group, end = extract_balanced(text, j, '{', '}')
                except ValueError:
                    pass
                else:
                    out.append(group[1:-1])
                    i = end
                    continue

        out.append(text[i])
        i += 1
    return ''.join(out)


def parse_newlabel_line(line: str) -> Optional[Tuple[str, List[str]]]:
    m = AUX_NEWLABEL_RE.match(line.strip())
    if not m:
        return None
    label = m.group(1)
    rest = m.group(2).lstrip()
    if not rest or rest[0] != '{':
        return None
    try:
        payload, _ = extract_balanced(rest, 0, '{', '}')
    except ValueError:
        return None
    return label, top_level_groups(payload[1:-1])


def extract_zref_field(payload: str, name: str) -> str:
    m = re.search(rf'\\{re.escape(name)}\{{([^{{}}]*)\}}', payload)
    return m.group(1).strip() if m else ''


def parse_zref_label_line(line: str) -> Optional[Tuple[str, LabelInfo]]:
    m = AUX_ZREF_LABEL_RE.match(line.strip())
    if not m:
        return None
    label = m.group(1)
    payload = m.group(2)
    number = extract_zref_field(payload, 'thecounter') or extract_zref_field(payload, 'default')
    kind = extract_zref_field(payload, 'zc@type') or extract_zref_field(payload, 'zc@counter')
    page = extract_zref_field(payload, 'page') or extract_zref_field(payload, 'zc@pgfmt')
    return label, {
        'number': number,
        'page': page,
        'title': '',
        'kind': kind,
    }


def parse_bibcite_line(line: str) -> Optional[Tuple[str, LabelInfo]]:
    m = AUX_BIBCITE_RE.match(line.strip())
    if not m:
        return None
    label = m.group(1)
    rest = m.group(2).lstrip()
    if not rest or rest[0] != '{':
        return None
    try:
        payload, _ = extract_balanced(rest, 0, '{', '}')
    except ValueError:
        return None
    fields = top_level_groups(payload[1:-1])
    number = strip_outer_braces(fields[0]) if len(fields) >= 1 else ''
    year = strip_outer_braces(fields[1]) if len(fields) >= 2 else ''
    author = strip_outer_braces(fields[2]) if len(fields) >= 3 else ''
    return label, {
        'number': number,
        'year': year,
        'author': author,
        'kind': 'bibitem',
    }


def count_newlabels(path: Path) -> int:
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'cannot read AUX file as UTF-8: {path}') from exc
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('\\newlabel{') or stripped.startswith('\\zref@newlabel{') or stripped.startswith('\\bibcite{'):
            count += 1
    return count


def discover_aux(input_path: Path, explicit_aux: Optional[Path]) -> Optional[Path]:
    if explicit_aux:
        if not explicit_aux.is_file():
            raise FileNotFoundError(f'explicit AUX file not found: {explicit_aux}')
        return explicit_aux

    # Never borrow an AUX file from a different TeX source merely because it
    # contains more labels. Thesis working directories commonly contain
    # several compiled revisions; selecting an unrelated/stale AUX silently
    # shifts figure and table references. Callers that intentionally want a
    # differently named AUX must opt in with --aux.
    candidates: List[Path] = []
    p = input_path.with_suffix('.aux')
    if p.is_file():
        candidates.append(p)

    if not candidates:
        return None

    # The default path is exact stem + ``.aux`` only.  Do not select a backup
    # or a nearby revision because it happens to contain more labels.
    return candidates[0]


def infer_source_labels(text: str) -> Dict[str, LabelInfo]:
    """Infer deterministic chapter-local numbers when no TeX AUX exists.

    This fallback covers figures, tables, and numbered equation environments.
    It deliberately avoids section/theorem inference, where starred constructs
    and package-specific counters make source-only numbering unsafe.
    """
    labels: Dict[str, LabelInfo] = {}
    chapter = 0
    counters = {"figure": 0, "table": 0, "equation": 0}
    token_re = re.compile(
        r'\\chapter\*?\{[^{}]*\}'
        r'|\\begin\{(figure|table|equation|align|gather|multline|eqnarray)\*?\}(.*?)'
        r'\\end\{\1\*?\}', re.S
    )
    for match in token_re.finditer(text):
        token = match.group(0)
        if token.startswith('\\chapter'):
            chapter += 1
            counters = {"figure": 0, "table": 0, "equation": 0}
            continue
        env = match.group(1)
        kind = env if env in {"figure", "table"} else "equation"
        starred = bool(re.match(rf'\\begin\{{{re.escape(env)}\*\}}', token))
        nonumber = bool(re.search(r'\\(?:notag|nonumber)\b', match.group(2) or ''))
        tag_match = re.search(r'\\tag\*?\{([^{}]*)\}', match.group(2) or '')
        if not starred and not nonumber:
            counters[kind] += 1
        number = (
            tag_match.group(1).strip() if tag_match else
            (f"{chapter}.{counters[kind]}" if chapter else str(counters[kind]))
        ) if not starred and not nonumber else ''
        for label in GENERIC_LABEL_RE.findall(match.group(2) or ''):
            labels[label] = {
                "number": number, "page": "", "title": "", "kind": kind,
                "numbering": (
                    "unnumbered" if starred or nonumber else
                    "tag" if tag_match else "numbered"
                ),
            }
    return labels


def infer_structural_source_labels(text: str) -> Dict[str, LabelInfo]:
    """Infer section and theorem labels from the current source document.

    An AUX file discovered next to a thesis source may legitimately come from
    an older/partial compile.  Structural labels are therefore collected from
    the current source as deterministic evidence and merged with AUX data.
    Numbered headings and theorem-like environments are counted with standard
    chapter-local counters.  This is preferable to leaking raw ``[label]``
    residue into a submission DOCX when a current AUX is unavailable.
    """
    labels: Dict[str, LabelInfo] = {}
    chapter = 0
    section = 0
    subsection = 0
    theorem = 0
    token_re = re.compile(
        r'\\(?P<heading>chapter|section|subsection|subsubsection)(?P<star>\*)?'
        r'\{(?:[^{}]|\{[^{}]*\})*\}'
        r'(?P<heading_tail>\s*\\label\{[^{}]+\})?'
        r'|\\begin\{(?P<theorem>theorem|lemma|proposition|corollary|definition|remark)\}'
        r'(?:\[[^\]]*\])?\s*(?:\\label\{(?P<theorem_label>[^{}]+)\})?',
        re.S,
    )
    for match in token_re.finditer(text):
        heading = match.group('heading')
        if heading:
            if match.group('star'):
                continue
            if heading == 'chapter':
                chapter += 1
                section = subsection = 0
                theorem = 0
            elif heading == 'section':
                section += 1
                subsection = 0
            elif heading == 'subsection':
                subsection += 1
            tail = match.group('heading_tail') or ''
            found = GENERIC_LABEL_RE.search(tail)
            if not found:
                continue
            if heading == 'chapter':
                number = str(chapter)
            elif heading == 'section':
                number = f'{chapter}.{section}' if chapter else str(section)
            elif heading == 'subsection':
                number = f'{chapter}.{section}.{subsection}' if chapter else f'{section}.{subsection}'
            else:
                number = ''
            labels[found.group(1)] = {
                'number': number, 'page': '', 'title': '', 'kind': heading,
            }
            continue
        theorem_label = match.group('theorem_label')
        if theorem_label:
            theorem += 1
            labels[theorem_label] = {
                'number': f'{chapter}.{theorem}' if chapter else str(theorem),
                'page': '', 'title': '', 'kind': match.group('theorem') or '',
            }
    return labels


def duplicate_source_labels(text: str) -> Dict[str, List[int]]:
    """Return duplicate ``\\label`` definitions with their source lines."""
    occurrences: Dict[str, List[int]] = {}
    for match in GENERIC_LABEL_RE.finditer(text):
        line = text.count('\n', 0, match.start()) + 1
        occurrences.setdefault(match.group(1), []).append(line)
    return {label: lines for label, lines in occurrences.items() if len(lines) > 1}


def merge_label_metadata(
    primary: Dict[str, LabelInfo],
    fallback: Dict[str, LabelInfo],
    *,
    reject_number_conflicts: bool = False,
) -> Dict[str, LabelInfo]:
    """Merge label maps, preserving AUX values and filling missing fields.

    When current-source numbering is available, an explicitly selected AUX
    file with a different number for the same label is stale evidence, not a
    harmless missing field.  Strict callers reject that mismatch instead of
    silently borrowing an older compilation's display number.
    """
    merged = {label: dict(info) for label, info in primary.items()}
    for label, info in fallback.items():
        current = merged.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})
        if (reject_number_conflicts and info.get('numbering') == 'unnumbered' and
                current.get('number')):
            raise ValueError(
                f'current source/AUX numbering mismatch for label {label!r}: '
                'source marks the equation unnumbered but AUX provides a number'
            )
        if (reject_number_conflicts and current.get('number') and info.get('number') and
                current.get('number') != info.get('number')):
            raise ValueError(
                f'current source/AUX number mismatch for label {label!r}: '
                f'AUX={current.get("number")!r}, source={info.get("number")!r}'
            )
        for key, value in info.items():
            if value and not current.get(key):
                current[key] = value
    return merged


def load_aux_labels(aux_path: Optional[Path]) -> Dict[str, LabelInfo]:
    labels: Dict[str, LabelInfo] = {}
    if not aux_path:
        return labels
    if not aux_path.is_file():
        raise FileNotFoundError(f'AUX file not found: {aux_path}')

    seen: set[Path] = set()
    active_aux: set[Path] = set()

    def merge(label: str, info: LabelInfo, source: Path) -> None:
        current = labels.get(label)
        if current is None:
            labels[label] = dict(info)
            return
        conflicting = [key for key, value in info.items()
                       if value and current.get(key) and current.get(key) != value]
        if conflicting:
            details = ', '.join(f'{key}={current.get(key)!r}/{info.get(key)!r}' for key in conflicting)
            raise ValueError(f'conflicting AUX label {label!r} ({details}) in {source}')
        for key, value in info.items():
            if value and not current.get(key):
                current[key] = value

    def visit(path: Path) -> None:
        path = path.resolve()
        if path in active_aux:
            raise ValueError(f'cyclic AUX include graph at {path}')
        if path in seen:
            return
        active_aux.add(path)
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f'cannot read AUX file as UTF-8: {path}') from exc
        try:
            for line in lines:
                child_match = re.match(r'^\\@input\{([^{}]+)\}', line.strip())
                if child_match:
                    child = Path(child_match.group(1))
                    if not child.suffix:
                        child = child.with_suffix('.aux')
                    if not child.is_absolute():
                        child = path.parent / child
                    if not child.is_file():
                        raise FileNotFoundError(f'AUX child file not found: {child}')
                    visit(child)
                    continue
                parsed = parse_newlabel_line(line)
                if parsed:
                    label, fields = parsed
                    if len(fields) >= 2:
                        merge(label, {
                            'number': fields[0].strip(),
                            'page': fields[1].strip(),
                            'title': fields[2].strip() if len(fields) >= 3 else '',
                            'kind': fields[3].strip() if len(fields) >= 4 else '',
                        }, path)
                    continue
                zparsed = parse_zref_label_line(line)
                if zparsed:
                    merge(*zparsed, path)
                    continue
                bparsed = parse_bibcite_line(line)
                if bparsed:
                    merge(*bparsed, path)
        finally:
            active_aux.remove(path)
            seen.add(path)

    visit(aux_path)
    return labels


def classify_label(label: str, info: Optional[LabelInfo]) -> str:
    lower = label.lower()
    kind = (info or {}).get('kind', '')
    if kind.startswith('equation'):
        return 'Equation'
    if kind.startswith('figure'):
        return 'Figure'
    if kind.startswith('table'):
        return 'Table'
    if kind.startswith('section') or kind.startswith('subsection') or kind.startswith('subsubsection'):
        return 'Section'
    if kind.startswith('appendix'):
        return 'Appendix'
    if kind.startswith('theorem'):
        return 'Theorem'
    if kind.startswith('lemma'):
        return 'Lemma'
    if kind.startswith('corollary'):
        return 'Corollary'
    if kind.startswith('proposition'):
        return 'Proposition'
    if kind.startswith('definition'):
        return 'Definition'
    if kind.startswith('remark'):
        return 'Remark'
    if kind.startswith('bibitem'):
        return 'Reference'
    if 'lemma' in lower:
        return 'Lemma'
    if 'corollary' in lower:
        return 'Corollary'
    if 'prop' in lower or 'proposition' in lower:
        return 'Proposition'
    return 'Reference'


def resolve_label(label: str, labels: Dict[str, LabelInfo], mode: str) -> str:
    info = labels.get(label)
    number = '' if (info or {}).get('numbering') == 'unnumbered' else (info or {}).get('number', '').strip()
    prefix = classify_label(label, info)

    if mode == 'ref':
        return number or f'[{label}]'
    if mode == 'eqref':
        return f'({number})' if number else f'[{label}]'
    if mode in {'cite', 'citep'}:
        return number or label
    if mode == 'citet':
        author = (info or {}).get('author', '').strip()
        if author and number:
            return f'{author} {number}'
        return author or number or label
    if mode == 'citeauthor':
        return (info or {}).get('author', '').strip() or label
    if mode == 'citeyear':
        return (info or {}).get('year', '').strip() or label
    if number:
        return f'{prefix} {number}'
    return f'{prefix} [{label}]'


def make_xref_placeholder(label: str, text: str) -> str:
    safe_text = text.replace(']', ')')
    return XREF_MARKER_FMT.format(label=label, text=safe_text)


def replace_refs(text: str, labels: Dict[str, LabelInfo]) -> str:
    def repl(match: re.Match[str]) -> str:
        cmd = match.group(1)
        raw = match.group(2)
        items = [x.strip() for x in raw.split(',') if x.strip()]
        if not items:
            return match.group(0)
        mode = 'zcref' if cmd in {'zcref', 'cref', 'Cref', 'autoref'} else cmd
        parts = [make_xref_placeholder(label, resolve_label(label, labels, mode)) for label in items]
        return ', '.join(parts)

    return REF_PATTERN.sub(repl, text)


def replace_cites(text: str, labels: Dict[str, LabelInfo]) -> str:
    def repl(match: re.Match[str]) -> str:
        cmd = match.group(1)
        raw = match.group(2)
        items = [x.strip() for x in raw.split(',') if x.strip()]
        if not items:
            return match.group(0)
        if cmd in {'cite', 'citep'}:
            parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]
            return '[' + '; '.join(parts) + ']'
        if cmd == 'citet':
            pieces = []
            for label in items:
                info = labels.get(label, {})
                author = info.get('author', '').strip()
                num = make_xref_placeholder(label, resolve_label(label, labels, 'cite'))
                if author:
                    pieces.append(f'{author} [{num}]')
                else:
                    pieces.append(f'[{num}]')
            return '; '.join(pieces)
        if cmd == 'citeauthor':
            parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]
            return ', '.join(parts)
        if cmd == 'citeyear':
            parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]
            return ', '.join(parts)
        return match.group(0)

    return CITE_PATTERN.sub(repl, text)


def clean_bib_body(body: str) -> str:
    body = body.strip()
    body = re.sub(r'%.*', '', body)
    body = re.sub(r'\\newblock', ' ', body)
    body = re.sub(r'\\penalty\d+', '', body)
    body = re.sub(r'\\providecommand\{[^{}]+\}\[[^\]]*\]\{[^{}]*\}', ' ', body)
    body = re.sub(r'\\expandafter.*', ' ', body)
    body = re.sub(r'\\(emph|textit|textbf|url|doi|natexlab)\{([^{}]*)\}', r'\2', body)
    body = re.sub(r'\\href\{([^{}]*)\}\{([^{}]*)\}', r'\2', body)
    body = body.replace('\\&', '&').replace('~', ' ')
    body = re.sub(r'\\[A-Za-z@]+', ' ', body)
    body = body.replace('{', '').replace('}', '')
    body = re.sub(r'\s+', ' ', body).strip()
    return body


def parse_bibitem_entries(text: str) -> List[Tuple[str, str]]:
    pattern = re.compile(r'\\bibitem(?:\[[^\]]*\])?\{([^{}]+)\}')
    matches = list(pattern.finditer(text))
    entries: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = text.find('\\end{thebibliography}', start)
            if end == -1:
                end = len(text)
        body = clean_bib_body(text[start:end])
        if body:
            entries.append((key, body))
    return entries


def bibliography_blocks(entries: List[Tuple[str, str]], labels: Dict[str, LabelInfo]) -> str:
    blocks = ['\\section*{参考文献}']
    for key, body in entries:
        number = labels.get(key, {}).get('number', '').strip()
        prefix = f'[{number}] ' if number else ''
        blocks.append(f'{LABEL_MARKER_FMT.format(label=key)} {prefix}{body}\\par')
    return '\n\n'.join(blocks)


def discover_bbl(input_path: Path) -> Optional[Path]:
    p = input_path.with_suffix('.bbl')
    return p if p.exists() else None


def expand_bibliography_commands(text: str, input_path: Path, labels: Dict[str, LabelInfo]) -> str:
    text = re.sub(r'\\bibliographystyle\{[^{}]+\}', '', text)
    bbl_path = discover_bbl(input_path)
    bib_match = re.search(r'\\bibliography\{([^{}]+)\}', text)
    if not bib_match or bbl_path is None or not bbl_path.exists():
        return text
    try:
        bbl_text = bbl_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'cannot read legacy BBL as UTF-8: {bbl_path}') from exc
    entries = parse_bibitem_entries(bbl_text)
    if not entries:
        return text
    replacement = bibliography_blocks(entries, labels)
    text = re.sub(r'\\bibliography\{([^{}]+)\}', lambda _m: replacement, text)
    # ``nocite`` affects bibliography selection.  In the limited legacy path
    # all entries from the trusted BBL are already serialized, so removing it
    # is safe only after that successful parse and replacement.
    return re.sub(r'\\nocite\{[^{}]+\}', '', text)


def expand_inline_thebibliography(text: str, labels: Dict[str, LabelInfo]) -> str:
    pattern = re.compile(r'\\begin\{thebibliography\}\{[^{}]*\}(.*?)\\end\{thebibliography\}', re.S)

    def repl(match: re.Match[str]) -> str:
        entries = parse_bibitem_entries(match.group(1))
        if not entries:
            return '\\section*{参考文献}'
        return bibliography_blocks(entries, labels)

    return pattern.sub(repl, text)


def inject_equation_label_markers(text: str) -> str:
    for env in EQUATION_ENVS:
        pattern = re.compile(rf'\\begin\{{{re.escape(env)}\}}(?P<body>.*?)\\end\{{{re.escape(env)}\}}', re.S)

        def repl(match: re.Match[str]) -> str:
            block = match.group(0)
            labels = re.findall(r'\\label\{([^{}]+)\}', block)
            starred = env.endswith('*')
            rows = [row for row in re.split(r'(?<!\\)\\\\(?:\[[^\]]*\])?', match.group('body'))
                    if row.strip()]
            row_nonumber = [bool(re.search(r'\\(?:notag|nonumber)\b', row)) for row in rows]
            if env.rstrip('*') in {'align', 'gather', 'multline', 'eqnarray'}:
                if any(row_nonumber) and not all(row_nonumber):
                    raise ValueError(
                        f'mixed numbered and unnumbered rows in \\begin{{{env}}} are unsupported; '
                        'split the display equations or make every row use the same numbering mode'
                    )
                if len(labels) > 1 and len(rows) > 1:
                    raise ValueError(
                        f'multiple labels in multi-row \\begin{{{env}}} are unsupported; '
                        'place one label in each independently supported display equation'
                    )
            nonumber = starred or bool(re.search(r'\\(?:notag|nonumber)\b', block))
            tag_match = re.search(r'\\tag\*?\{([^{}]*)\}', block)
            tag = tag_match.group(1).strip() if tag_match else ''
            if not labels and not nonumber and not tag:
                return block
            markers = ''.join(f'\n{EQ_LABEL_MARKER_FMT.format(label=label)}\n' for label in labels)
            if nonumber:
                markers = f'\n{EQ_CONTROL_MARKER_FMT.format(mode="unnumbered", tag="")}\n' + markers
            elif tag:
                markers = f'\n{EQ_CONTROL_MARKER_FMT.format(mode="tag", tag=tag)}\n' + markers
            block_wo_labels = GENERIC_LABEL_RE.sub('', block)
            if tag_match:
                block_wo_labels = block_wo_labels.replace(tag_match.group(0), '')
            return markers + block_wo_labels

        text = pattern.sub(repl, text)
    # Display math written as ``\[...\]`` or ``$$...$$`` is explicitly
    # unnumbered in LaTeX.  Preserve that fact before Pandoc turns it into the
    # same OMML shape as a numbered equation environment.
    bracket_re = re.compile(r'\\\[.*?\\\]', re.S)
    text = bracket_re.sub(
        lambda match: f'\n{EQ_CONTROL_MARKER_FMT.format(mode="unnumbered", tag="")}\n{match.group(0)}',
        text,
    )
    dollar_re = re.compile(r'\$\$.*?\$\$', re.S)
    text = dollar_re.sub(
        lambda match: f'\n{EQ_CONTROL_MARKER_FMT.format(mode="unnumbered", tag="")}\n{match.group(0)}',
        text,
    )
    return text


def inject_caption_label_markers(text: str) -> str:
    out: List[str] = []
    i = 0
    needle = '\\caption'
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        j = idx + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        optional = ''
        if j < len(text) and text[j] == '[':
            optional, j = extract_balanced(text, j, '[', ']')
            while j < len(text) and text[j].isspace():
                j += 1
        if j >= len(text) or text[j] != '{':
            out.append(needle)
            i = idx + len(needle)
            continue
        group, j = extract_balanced(text, j, '{', '}')
        caption_body = group[1:-1]
        k = j
        while k < len(text) and text[k].isspace():
            k += 1
        m = GENERIC_LABEL_RE.match(text, k)
        if m:
            label = m.group(1)
            rebuilt = f'\\caption{optional}{{{caption_body} {LABEL_MARKER_FMT.format(label=label)}}}'
            out.append(rebuilt)
            i = m.end()
        else:
            out.append(text[idx:j])
            i = j
    return ''.join(out)


def inject_theorem_label_markers(text: str) -> str:
    for env in THEOREM_ENVS:
        pattern = re.compile(rf'(\\begin\{{{re.escape(env)}\}})\s*\\label\{{([^{{}}]+)\}}')
        text = pattern.sub(lambda m: f'\n{LABEL_MARKER_FMT.format(label=m.group(2))}\n{m.group(1)}', text)
    return text


def inject_generic_label_markers(text: str) -> str:
    text = inject_caption_label_markers(text)
    text = inject_theorem_label_markers(text)
    return GENERIC_LABEL_RE.sub(lambda m: f'\n{LABEL_MARKER_FMT.format(label=m.group(1))}\n', text)


def normalize_theorem_environments(text: str) -> str:
    r"""Expand theorem-like environments into DOCX-writable LaTeX.

    Pandoc retains unknown theorem environments as raw LaTeX blocks.  The
    DOCX writer intentionally drops raw LaTeX, which can silently discard the
    complete environment, including nested lists, equations, and citations.
    Replace only the environment delimiters with ordinary paragraphs while
    leaving the body untouched for Pandoc to parse normally.
    """
    begin_re = re.compile(
        r'\\begin\{(' + '|'.join(map(re.escape, THEOREM_ENVS)) + r')\}'
        r'(?:\[([^\]]*)\])?'
    )

    def begin_repl(match: re.Match[str]) -> str:
        env = match.group(1)
        title = (match.group(2) or '').strip()
        label = THEOREM_LABELS[env]
        heading = label if not title else f'{label}（{title}）'
        return f'\n\\par\\noindent\\textit{{{heading}：}}\\par\n'

    text = begin_re.sub(begin_repl, text)
    for env in THEOREM_ENVS:
        text = re.sub(
            rf'\\end\{{{re.escape(env)}\}}',
            lambda _match: '\n\\par\n',
            text,
        )
    return text


def strip_latex_preamble(text: str) -> str:
    """Remove LaTeX preamble (everything before \\begin{document}) and \\end{document}."""
    m = re.search(r'\\begin\{document\}', text)
    if m:
        text = text[m.end():]
    text = re.sub(r'\\end\{document\}', '', text)
    return text


def first_braced_macro(text: str, names: Tuple[str, ...]) -> Optional[Tuple[str, str]]:
    """Return the first balanced value for one of ``names``.

    Thesis metadata commonly lives in the LaTeX preamble, which is otherwise
    removed before Pandoc sees the document.  Use the same balanced-brace
    parser as the rest of this preprocessor so titles containing nested TeX
    markup are not truncated by a regular expression.
    """
    matches: List[Tuple[int, str, str]] = []
    for name in names:
        pattern = re.compile(rf'\\{re.escape(name)}\s*')
        for match in pattern.finditer(text):
            start = match.end()
            if start >= len(text) or text[start] != '{':
                continue
            try:
                group, _ = extract_balanced(text, start, '{', '}')
            except ValueError:
                continue
            matches.append((match.start(), name, group[1:-1]))
            break
    if not matches:
        return None
    _, name, value = min(matches, key=lambda item: item[0])
    return name, value


SUPPORTED_PREAMBLE_DEFINITIONS = (
    'newcommand', 'renewcommand', 'providecommand', 'DeclareMathOperator',
    'DeclareMathOperator*',
)
UNSUPPORTED_PREAMBLE_DEFINITIONS = (
    'def', 'gdef', 'xdef', 'edef', 'newenvironment', 'renewenvironment',
    'DeclareRobustCommand',
)


def _skip_tex_space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def extract_supported_preamble_definitions(preamble: str) -> List[str]:
    """Preserve balanced, Pandoc-readable macro definitions from the preamble.

    Package setup and class-specific switches are intentionally not copied to
    the Pandoc input, but dropping a simple ``\\newcommand`` can change every
    formula that uses it.  Keep only definitions with a fully parsed balanced
    body.  Definitions outside this small supported grammar fail explicitly
    instead of being silently discarded.
    """
    command_re = re.compile(
        r'\\(?P<command>newcommand|renewcommand|providecommand|DeclareMathOperator\*?|'
        r'def|gdef|xdef|edef|newenvironment|renewenvironment|DeclareRobustCommand)'
        r'(?=[\s{\\])'
    )
    definitions: List[str] = []
    for match in command_re.finditer(preamble):
        command = match.group('command')
        start = match.start()
        if command in UNSUPPORTED_PREAMBLE_DEFINITIONS:
            raise ValueError(
                f'unsupported preamble definition \\{command}; '
                'use a supported balanced \\newcommand or provide a Pandoc-readable source'
            )

        position = _skip_tex_space(preamble, match.end())
        # All supported declarations name a command in their first group,
        # except the common shorthand ``\\newcommand{\\foo}`` is still a
        # balanced group and follows the same path.
        if position >= len(preamble):
            raise ValueError(f'incomplete preamble definition \\{command}')
        if preamble[position] == '{':
            _target, position = extract_balanced(preamble, position, '{', '}')
        elif preamble[position] == '\\':
            name_match = re.match(r'\\[A-Za-z@]+\*?', preamble[position:])
            if not name_match:
                raise ValueError(f'invalid preamble definition \\{command}')
            position += len(name_match.group(0))
        else:
            raise ValueError(f'incomplete preamble definition \\{command}')

        if command in {'newcommand', 'renewcommand', 'providecommand'}:
            # Optional argument count and optional default argument.
            for _ in range(2):
                position = _skip_tex_space(preamble, position)
                if position < len(preamble) and preamble[position] == '[':
                    _optional, position = extract_balanced(preamble, position, '[', ']')
        position = _skip_tex_space(preamble, position)
        if position >= len(preamble) or preamble[position] != '{':
            raise ValueError(f'incomplete preamble definition \\{command} body')
        _body, position = extract_balanced(preamble, position, '{', '}')
        definitions.append(preamble[start:position].strip())
    return definitions


def preserve_preamble_metadata(text: str) -> str:
    """Re-emit title metadata before stripping the LaTeX preamble."""
    document = re.search(r'\\begin\{document\}', text)
    preamble = text[:document.start()] if document else text
    preserved: List[str] = []
    preserved.extend(extract_supported_preamble_definitions(preamble))
    title = first_braced_macro(preamble, ('title',))
    english_title = first_braced_macro(preamble, ('englishtitle', 'entitle', 'titleen'))
    if title:
        preserved.append(f'\\title{{{title[1]}}}')
    if english_title:
        # Unknown commands are silently discarded by Pandoc's LaTeX reader.
        # An unknown environment, however, is retained as a Div whose class is
        # the environment name, giving the Lua filter a stable metadata carrier.
        preserved.append('\\begin{english-title}')
        preserved.append(english_title[1])
        preserved.append('\\end{english-title}')
    body = strip_latex_preamble(text)
    return ('\n'.join(preserved) + '\n\n' if preserved else '') + body


def write_dependency_manifest(path: Path, dependencies: List[Path], encoding: str) -> None:
    """Persist the exact source files consumed by this preprocessing run."""
    entries = []
    for dependency in dependencies:
        digest = hashlib.sha256(dependency.read_bytes()).hexdigest()
        entries.append({'path': str(dependency.resolve()), 'sha256': digest, 'encoding': encoding})
    payload = {
        'schema_version': '1.0',
        'entrypoint': 'scripts/preprocess_tex.py',
        'encoding': encoding,
        'files': entries,
    }
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}-', suffix='.tmp', dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def replace_ctexbook_commands(text: str) -> str:
    """Convert ctexbook-specific commands to standard LaTeX that pandoc understands."""
    # Abstract: \begin{abstract} → \chapter*{摘要}
    #           \begin{abstract}[english] → \chapter*{Abstract}
    #           \end{abstract} → remove
    text = re.sub(r'\\begin\{abstract\}\[(?:english|en)\]', r'\\chapter*{Abstract}', text)
    text = re.sub(r'\\begin\{abstract\}', r'\\chapter*{摘要}', text)
    text = re.sub(r'\\end\{abstract\}', '', text)

    # Keywords: \keywords{...} → \textbf{关键词：}...
    #           \keywords[english|en]{...} → \textbf{Keywords:}...
    result = []
    i = 0
    while i < len(text):
        m_en = re.match(r'\\keywords\[(?:english|en)\]\{', text[i:])
        m_cn = re.match(r'\\keywords\{', text[i:])
        if m_en:
            prefix = '\n\\textbf{Keywords:} '
            open_pos = i + m_en.group().find('{')
            body, end = extract_balanced(text, open_pos, '{', '}')
            result.append(prefix + body[1:-1])
            i = end
        elif m_cn:
            prefix = '\n\\textbf{关键词：} '
            open_pos = i + m_cn.group().find('{')
            body, end = extract_balanced(text, open_pos, '{', '}')
            result.append(prefix + body[1:-1])
            i = end
        else:
            result.append(text[i])
            i += 1
    text = ''.join(result)

    # Acknowledgments: \acknowledgments{...} → \chapter*{后记}...
    result = []
    i = 0
    while i < len(text):
        if text[i:].startswith('\\acknowledgments{'):
            open_pos = i + len('\\acknowledgments')
            body, end = extract_balanced(text, open_pos, '{', '}')
            inner = body[1:-1]
            result.append('\n\\chapter*{后记}\n')
            result.append(inner)
            i = end
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def main() -> None:
    parser = argparse.ArgumentParser(description='Preprocess TJUFE LaTeX before pandoc DOCX conversion.')
    parser.add_argument('input')
    parser.add_argument('output')
    parser.add_argument('--aux', dest='aux', default=None)
    parser.add_argument('--encoding', default='utf-8', help='input encoding (default: utf-8)')
    parser.add_argument('--dependency-manifest', type=Path,
                        help='write the exact input/include files and hashes consumed by this run')
    parser.add_argument(
        '--preserve-citations', action='store_true',
        help='leave citation commands for pandoc citeproc and remove biblatex print commands',
    )
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    aux_path = discover_aux(input_path, Path(args.aux).resolve() if args.aux else None)

    if not input_path.is_file():
        parser.error(f'input TeX file not found: {input_path}')
    try:
        text = input_path.read_text(encoding=args.encoding)
    except (OSError, UnicodeDecodeError) as exc:
        parser.error(f'cannot decode input TeX as {args.encoding}: {input_path}: {exc}')

    # Protect before expansion so an ``\input`` appearing in a verbatim/code
    # block is not executed.  Protect once more after expansion for opaque
    # regions introduced by child files.
    text, protected_regions = protect_opaque_regions(text)
    dependencies: List[Path] = []
    text = expand_includes(
        text, input_path, encoding=args.encoding, dependencies=dependencies
    )
    # Child files are protected during recursive expansion.  This second pass
    # catches opaque regions that were present in the expanded text while
    # using a disjoint token namespace so two protection maps cannot overwrite
    # one another.
    text, child_protected_regions = protect_opaque_regions(
        text, token_prefix='TJUFE_EXPANDED_OPAQUE_REGION'
    )
    protected_regions.update(child_protected_regions)
    duplicates = duplicate_source_labels(text)
    if duplicates:
        details = '; '.join(f'{label} at lines {",".join(map(str, lines))}'
                            for label, lines in sorted(duplicates.items()))
        parser.error(f'duplicate LaTeX labels are not allowed: {details}')
    source_labels = merge_label_metadata(
        infer_source_labels(text), infer_structural_source_labels(text)
    )
    text = preserve_preamble_metadata(text)
    text = unwrap_subfloat(text)
    text, _formula_layout_hits = apply_formula_layout_overrides(text)
    text = normalize_math_macros(text)
    text = expand_visible_date_macros(text)
    text = unwrap_text_macros_outside_math(text)
    text = replace_ctexbook_commands(text)
    text = inject_equation_label_markers(text)
    text = inject_generic_label_markers(text)
    text = normalize_theorem_environments(text)

    labels = merge_label_metadata(
        load_aux_labels(aux_path), source_labels, reject_number_conflicts=aux_path is not None
    )
    if args.preserve_citations:
        # Pandoc citeproc owns citation rendering and bibliography generation.
        # biblatex's output command is raw TeX and otherwise disappears without
        # producing a reference list in DOCX.
        text = re.sub(r'\\printbibliography(?:\[[^\]]*\])?', '', text)
        text = re.sub(r'\\addbibresource(?:\[[^\]]*\])?\{[^{}]+\}', '', text)
    else:
        text = expand_bibliography_commands(text, input_path, labels)
        text = expand_inline_thebibliography(text, labels)
        text = replace_cites(text, labels)
    text = replace_refs(text, labels)
    text = restore_opaque_regions(text, protected_regions)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding='utf-8')
    if args.dependency_manifest:
        write_dependency_manifest(args.dependency_manifest, dependencies, args.encoding)


if __name__ == '__main__':
    main()
