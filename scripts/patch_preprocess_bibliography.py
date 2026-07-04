from pathlib import Path

p = Path('/Users/cheng/clawd/deliverables/天津财经大学博士毕业论文规范tjufe-pandoc-docx-kit-20260326/scripts/preprocess_tjufe_tex.py')
text = p.read_text()

text = text.replace(
"GENERIC_LABEL_RE = re.compile(r'\\label\\{([^{}]+)\\}')\n",
"GENERIC_LABEL_RE = re.compile(r'\\label\\{([^{}]+)\\}')\nBIBCITE_RE = re.compile(r'^\\\\bibcite\\{([^}]+)\\}(.*)$')\n"
)

needle = "def parse_zref_label_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:\n"
insert = '''def strip_outer_braces(s: str) -> str:\n    s = s.strip()\n    while len(s) >= 2 and s[0] == '{' and s[-1] == '}':\n        s = s[1:-1].strip()\n    return s\n\n\ndef parse_bibcite_line(line: str) -> Optional[Tuple[str, Dict[str, str]]]:\n    m = BIBCITE_RE.match(line.strip())\n    if not m:\n        return None\n    label = m.group(1)\n    rest = m.group(2).lstrip()\n    if not rest or rest[0] != '{':\n        return None\n    try:\n        payload, _ = extract_balanced(rest, 0, '{', '}')\n    except ValueError:\n        return None\n    fields = top_level_groups(payload[1:-1])\n    number = strip_outer_braces(fields[0]) if len(fields) >= 1 else ''\n    year = strip_outer_braces(fields[1]) if len(fields) >= 2 else ''\n    author = strip_outer_braces(fields[2]) if len(fields) >= 3 else ''\n    return label, {\n        'number': number,\n        'year': year,\n        'author': author,\n        'kind': 'bibitem',\n    }\n\n\n''' + needle
if needle not in text:
    raise SystemExit('needle parse_zref not found')
text = text.replace(needle, insert, 1)

old = """        zparsed = parse_zref_label_line(line)\n        if zparsed:\n            label, info = zparsed\n            current = labels.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})\n            for key, value in info.items():\n                if value and not current.get(key):\n                    current[key] = value\n    return labels\n"""
new = """        zparsed = parse_zref_label_line(line)\n        if zparsed:\n            label, info = zparsed\n            current = labels.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})\n            for key, value in info.items():\n                if value and not current.get(key):\n                    current[key] = value\n            continue\n        bparsed = parse_bibcite_line(line)\n        if bparsed:\n            label, info = bparsed\n            current = labels.setdefault(label, {'number': '', 'page': '', 'title': '', 'kind': ''})\n            for key, value in info.items():\n                if value and not current.get(key):\n                    current[key] = value\n    return labels\n"""
if old not in text:
    raise SystemExit('load_aux_labels block not found')
text = text.replace(old, new, 1)

old = """    if kind.startswith('theorem'):\n        return 'Theorem'\n    return 'Reference'\n"""
new = """    if kind.startswith('theorem'):\n        return 'Theorem'\n    if kind.startswith('bibitem'):\n        return 'Reference'\n    return 'Reference'\n"""
text = text.replace(old, new, 1)

old = """    if mode == 'ref':\n        return number or f'[{label}]'\n    if mode == 'eqref':\n        return f'({number})' if number else f'[{label}]'\n    if number:\n        return f'{prefix} {number}'\n    return f'{prefix} [{label}]'\n"""
new = """    if mode == 'ref':\n        return number or f'[{label}]'\n    if mode == 'eqref':\n        return f'({number})' if number else f'[{label}]'\n    if mode in {'cite', 'citep'}:\n        return f'[{number}]' if number else f'[{label}]'\n    if mode == 'citet':\n        author = (info or {}).get('author', '').strip()\n        return f'{author} [{number}]'.strip() if number or author else f'[{label}]'\n    if mode == 'citeauthor':\n        author = (info or {}).get('author', '').strip()\n        return author or f'[{label}]'\n    if mode == 'citeyear':\n        year = (info or {}).get('year', '').strip()\n        return year or f'[{label}]'\n    if number:\n        return f'{prefix} {number}'\n    return f'{prefix} [{label}]'\n"""
text = text.replace(old, new, 1)

needle = "REF_PATTERN = re.compile(r'\\\\(zcref|cref|Cref|autoref|ref|eqref)\\{([^{}]+)\\}')\n"
replacement = needle + "CITE_PATTERN = re.compile(r'\\\\(cite|citep|citet|citeauthor|citeyear)\\{([^{}]+)\\}')\n"
text = text.replace(needle, replacement, 1)

insert_after = "def make_xref_placeholder(label: str, text: str) -> str:\n    safe_text = text.replace(']', ')')\n    return XREF_MARKER_FMT.format(label=label, text=safe_text)\n\n\n"
addition = '''def replace_cites(text: str, labels: Dict[str, LabelInfo]) -> str:\n    def repl(match: re.Match[str]) -> str:\n        cmd = match.group(1)\n        raw = match.group(2)\n        items = [x.strip() for x in raw.split(',') if x.strip()]\n        if not items:\n            return match.group(0)\n        parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]\n        if cmd in {'cite', 'citep'}:\n            return '[' + ', '.join(p.strip('[]') for p in [resolve_label(label, labels, cmd) for label in items]) + ']'\n        return ', '.join(parts)\n\n    def repl_linked(match: re.Match[str]) -> str:\n        cmd = match.group(1)\n        raw = match.group(2)\n        items = [x.strip() for x in raw.split(',') if x.strip()]\n        if not items:\n            return match.group(0)\n        if cmd in {'cite', 'citep'}:\n            parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]\n            joined = ', '.join(part[len('[[[TJUFE_XREF:' + label + '|'):] for part, label in zip(parts, items)] )\n            return '[' + ', '.join(make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items) + ']'\n        parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]\n        return ', '.join(parts)\n\n    def repl2(match: re.Match[str]) -> str:\n        cmd = match.group(1)\n        raw = match.group(2)\n        items = [x.strip() for x in raw.split(',') if x.strip()]\n        if not items:\n            return match.group(0)\n        parts = [make_xref_placeholder(label, resolve_label(label, labels, cmd)) for label in items]\n        if cmd in {'cite', 'citep'}:\n            return '[' + '; '.join(parts) + ']'\n        return ', '.join(parts)\n\n    return CITE_PATTERN.sub(repl2, text)\n\n\ndef discover_bbl(input_path: Path) -> Optional[Path]:\n    p = input_path.with_suffix('.bbl')\n    return p if p.exists() else None\n\n\ndef parse_bbl_entries(bbl_path: Path) -> List[Tuple[str, str]]:\n    text = bbl_path.read_text(errors='ignore')\n    pattern = re.compile(r'\\\\bibitem(?:\\[[^\\]]*\\])?\\{([^{}]+)\\}')\n    matches = list(pattern.finditer(text))\n    entries: List[Tuple[str, str]] = []\n    for i, m in enumerate(matches):\n        key = m.group(1).strip()\n        start = m.end()\n        end = matches[i + 1].start() if i + 1 < len(matches) else text.find('\\\\end{thebibliography}', start)\n        if end == -1:\n            end = len(text)\n        body = text[start:end].strip()\n        body = re.sub(r'\\\\newblock', ' ', body)\n        body = re.sub(r'\\\\providecommand\\{[^{}]+\\}\\[[^\\]]*\\]\\{[^{}]*\\}', ' ', body)\n        body = re.sub(r'\\\\expandafter.*', ' ', body)\n        body = re.sub(r'\n+', ' ', body)\n        body = re.sub(r'\s+', ' ', body).strip()\n        if body:\n            entries.append((key, body))\n    return entries\n\n\ndef expand_bibliography(text: str, input_path: Path, labels: Dict[str, LabelInfo]) -> str:\n    bbl_path = discover_bbl(input_path)\n    text = re.sub(r'\\\\bibliographystyle\\{[^{}]+\\}', '', text)\n    bib_match = re.search(r'\\\\bibliography\\{([^{}]+)\\}', text)\n    if not bib_match or bbl_path is None or not bbl_path.exists():\n        return text\n    entries = parse_bbl_entries(bbl_path)\n    if not entries:\n        return text\n    blocks = ['\\section*{参考文献}']\n    for key, body in entries:\n        number = labels.get(key, {}).get('number', '').strip()\n        prefix = f'[{number}] ' if number else ''\n        blocks.append(f'{LABEL_MARKER_FMT.format(label=key)} {prefix}{body}\\par')\n    replacement = '\n\n'.join(blocks)\n    return re.sub(r'\\\\bibliography\\{([^{}]+)\\}', replacement, text)\n\n\n''' + "def replace_refs(text: str, labels: Dict[str, LabelInfo]) -> str:\n"
if insert_after not in text:
    raise SystemExit('insert_after not found')
text = text.replace(insert_after, insert_after + addition, 1)

text = text.replace(
"    labels = load_aux_labels(aux_path)\n    text = replace_refs(text, labels)\n",
"    labels = load_aux_labels(aux_path)\n    text = expand_bibliography(text, input_path, labels)\n    text = replace_cites(text, labels)\n    text = replace_refs(text, labels)\n"
)

p.write_text(text)
print('patched', p)
