from pathlib import Path

p = Path('/Users/cheng/clawd/deliverables/天津财经大学博士毕业论文规范tjufe-pandoc-docx-kit-20260326/scripts/preprocess_tjufe_tex.py')
text = p.read_text()

text = text.replace("LABEL_MARKER_FMT = '[[[TJUFE_LABEL:{label}]]]'", "LABEL_MARKER_FMT = 'TJUFE_LABEL__{label}__'")

anchor = "EQUATION_ENVS = (\n    'equation', 'equation*',\n    'align', 'align*',\n    'gather', 'gather*',\n    'multline', 'multline*',\n    'eqnarray', 'eqnarray*',\n)\n"
replacement = anchor + "\nTHEOREM_ENVS = (\n    'theorem', 'lemma', 'proposition', 'corollary', 'definition', 'remark',\n)\n"
if anchor not in text:
    raise SystemExit('equation env anchor not found')
text = text.replace(anchor, replacement, 1)

old = '''def inject_generic_label_markers(text: str) -> str:\n    return GENERIC_LABEL_RE.sub(lambda m: f'\\n{LABEL_MARKER_FMT.format(label=m.group(1))}\\n', text)\n'''
new = '''def inject_caption_label_markers(text: str) -> str:\n    out: list[str] = []\n    i = 0\n    needle = '\\caption'\n    while True:\n        idx = text.find(needle, i)\n        if idx == -1:\n            out.append(text[i:])\n            break\n        out.append(text[i:idx])\n        j = idx + len(needle)\n        while j < len(text) and text[j].isspace():\n            j += 1\n        optional = ''\n        if j < len(text) and text[j] == '[':\n            optional, j = extract_balanced(text, j, '[', ']')\n            while j < len(text) and text[j].isspace():\n                j += 1\n        if j >= len(text) or text[j] != '{':\n            out.append(needle)\n            i = idx + len(needle)\n            continue\n        group, j = extract_balanced(text, j, '{', '}')\n        caption_body = group[1:-1]\n        k = j\n        while k < len(text) and text[k].isspace():\n            k += 1\n        m = GENERIC_LABEL_RE.match(text, k)\n        if m:\n            label = m.group(1)\n            rebuilt = f'\\caption{optional}{{{caption_body} {LABEL_MARKER_FMT.format(label=label)}}}'\n            out.append(rebuilt)\n            i = m.end()\n        else:\n            out.append(text[idx:j])\n            i = j\n    return ''.join(out)\n\n\ndef inject_theorem_label_markers(text: str) -> str:\n    for env in THEOREM_ENVS:\n        pattern = re.compile(rf'(\\begin\\{{{re.escape(env)}\\}})\\s*\\label\\{{([^{{}}]+)\\}}')\n        text = pattern.sub(lambda m: f"\\n{LABEL_MARKER_FMT.format(label=m.group(2))}\\n{m.group(1)}", text)\n    return text\n\n\ndef inject_generic_label_markers(text: str) -> str:\n    text = inject_caption_label_markers(text)\n    text = inject_theorem_label_markers(text)\n    return GENERIC_LABEL_RE.sub(lambda m: f'\\n{LABEL_MARKER_FMT.format(label=m.group(1))}\\n', text)\n'''
if old not in text:
    raise SystemExit('inject_generic_label_markers block not found')
text = text.replace(old, new, 1)

p.write_text(text)
print('patched', p)
