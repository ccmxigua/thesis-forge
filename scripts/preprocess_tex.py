#!/opt/homebrew/bin/python3
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple


XREF_MARKER_FMT = '[[[TJUFE_XREF:{label}|{text}]]]'
EQ_LABEL_MARKER_FMT = '[[[TJUFE_EQLABEL:{label}]]]'
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

AUX_NEWLABEL_RE = re.compile(r'^\\newlabel\{([^}]+)\}(.*)$')
AUX_ZREF_LABEL_RE = re.compile(r'^\\zref@newlabel\{([^}]+)\}\{(.*)\}$')
AUX_BIBCITE_RE = re.compile(r'^\\bibcite\{([^}]+)\}(.*)$')
GENERIC_LABEL_RE = re.compile(r'\\label\{([^{}]+)\}')
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
        if j < len(text) and text[j] == '[':
            try:
                _, j = extract_balanced(text, j, '[', ']')
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
        out.append(body[1:-1].strip())
        i = j
    return ''.join(out)


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
        text = path.read_text(errors='ignore')
    except OSError:
        return 0
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('\\newlabel{') or stripped.startswith('\\zref@newlabel{') or stripped.startswith('\\bibcite{'):
            count += 1
    return count


def discover_aux(input_path: Path, explicit_aux: Optional[Path]) -> Optional[Path]:
    if explicit_aux:
        return explicit_aux if explicit_aux.exists() else None

    candidates: List[Path] = []
    for suffix in ('.aux', '.aux.bak'):
        p = input_path.with_suffix(suffix)
        if p.exists():
            candidates.append(p)

    current = input_path.parent
    seen = {p.resolve() for p in candidates if p.exists()}
    for _ in range(4):
        try:
            for cand in current.glob('*.aux*'):
                if cand.is_file():
                    resolved = cand.resolve()
                    if resolved not in seen:
                        candidates.append(cand)
                        seen.add(resolved)
        except OSError:
            pass
        if current.parent == current:
            break
        current = current.parent

    if not candidates:
        return None

    def score(path: Path) -> Tuple[int, int, int, str]:
        stem_match = int(path.stem == input_path.stem)
        label_count = count_newlabels(path)
        not_backup = int(not str(path).endswith('.bak'))
        return (stem_match, label_count, not_backup, str(path))

    return max(candidates, key=score)


def load_aux_labels(aux_path: Optional[Path]) -> Dict[str, LabelInfo]:
    labels: Dict[str, LabelInfo] = {}
    if not aux_path or not aux_path.exists():
        return labels
    for line in aux_path.read_text(errors='ignore').splitlines():
        parsed = parse_newlabel_line(line)
        if parsed:
            label, fields = parsed
            if len(fields) >= 4:
                labels[label] = {
                    'number': fields[0].strip(),
                    'page': fields[1].strip(),
                    'title': fields[2].strip(),
                    'kind': fields[3].strip(),
                }
            continue
        zparsed = parse_zref_label_line(line)
        if zparsed:
            label, info = zparsed
            current = labels.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})
            for key, value in info.items():
                if value and not current.get(key):
                    current[key] = value
            continue
        bparsed = parse_bibcite_line(line)
        if bparsed:
            label, info = bparsed
            current = labels.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})
            for key, value in info.items():
                if value and not current.get(key):
                    current[key] = value
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
    number = (info or {}).get('number', '').strip()
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
    text = re.sub(r'\\nocite\{[^{}]+\}', '', text)
    bbl_path = discover_bbl(input_path)
    bib_match = re.search(r'\\bibliography\{([^{}]+)\}', text)
    if not bib_match or bbl_path is None or not bbl_path.exists():
        return text
    entries = parse_bibitem_entries(bbl_path.read_text(errors='ignore'))
    if not entries:
        return text
    replacement = bibliography_blocks(entries, labels)
    return re.sub(r'\\bibliography\{([^{}]+)\}', lambda _m: replacement, text)


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
            if not labels:
                return block
            markers = ''.join(f'\n{EQ_LABEL_MARKER_FMT.format(label=label)}\n' for label in labels)
            block_wo_labels = GENERIC_LABEL_RE.sub('', block)
            return markers + block_wo_labels

        text = pattern.sub(repl, text)
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


def strip_latex_preamble(text: str) -> str:
    """Remove LaTeX preamble (everything before \\begin{document}) and \\end{document}."""
    m = re.search(r'\\begin\{document\}', text)
    if m:
        text = text[m.end():]
    text = re.sub(r'\\end\{document\}', '', text)
    return text


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
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    aux_path = discover_aux(input_path, Path(args.aux).resolve() if args.aux else None)

    text = input_path.read_text(errors='ignore')
    text = strip_latex_preamble(text)
    text = unwrap_subfloat(text)
    text = normalize_math_macros(text)
    text = replace_ctexbook_commands(text)
    text = inject_equation_label_markers(text)
    text = inject_generic_label_markers(text)

    labels = load_aux_labels(aux_path)
    text = expand_bibliography_commands(text, input_path, labels)
    text = expand_inline_thebibliography(text, labels)
    text = replace_cites(text, labels)
    text = replace_refs(text, labels)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)


if __name__ == '__main__':
    main()
