from pathlib import Path

p = Path('/Users/cheng/clawd/deliverables/天津财经大学博士毕业论文规范tjufe-pandoc-docx-kit-20260326/scripts/postprocess_tjufe_docx.py')
text = p.read_text()

anchor1 = "EQLABEL_RE = re.compile(r'^\\[\\[\\[TJUFE_EQLABEL:([^\\]]+)\\]\\]\\]$')\n"
replace1 = anchor1 + "LABEL_RE = re.compile(r'\\[\\[\\[TJUFE_LABEL:([^\\]]+)\\]\\]\\]')\n"
if anchor1 not in text:
    raise SystemExit('anchor1 not found')
text = text.replace(anchor1, replace1, 1)

old_helpers = """def extract_eq_label_marker(text: str) -> str | None:\n    m = EQLABEL_RE.match(text.strip())\n    if not m:\n        return None\n    return m.group(1).strip()\n\n\ndef rebuild_heading_with_tab(p: ET.Element, prefix: str, title: str) -> None:\n"""
new_helpers = """def extract_eq_label_marker(text: str) -> str | None:\n    m = EQLABEL_RE.match(text.strip())\n    if not m:\n        return None\n    return m.group(1).strip()\n\n\ndef extract_generic_label_markers(text: str) -> list[str]:\n    return [m.group(1).strip() for m in LABEL_RE.finditer(text) if m.group(1).strip()]\n\n\ndef strip_label_markers_in_paragraph(p: ET.Element) -> None:\n    for t in p.findall('.//' + qn('w', 't')):\n        if t.text:\n            t.text = LABEL_RE.sub('', t.text)\n\n\ndef attach_bookmarks_to_paragraph(p: ET.Element, labels: list[str], bookmark_map: dict[str, str], next_bookmark_id: int) -> int:\n    if not labels:\n        return next_bookmark_id\n    insert_at = 1 if len(p) > 0 and p[0].tag == qn('w', 'pPr') else 0\n    for label in labels:\n        bookmark_name = sanitize_bookmark_name(label)\n        bookmark_map[label] = bookmark_name\n        bm_start = ET.Element(qn('w', 'bookmarkStart'))\n        bm_start.set(qn('w', 'id'), str(next_bookmark_id))\n        bm_start.set(qn('w', 'name'), bookmark_name)\n        bm_end = ET.Element(qn('w', 'bookmarkEnd'))\n        bm_end.set(qn('w', 'id'), str(next_bookmark_id))\n        p.insert(insert_at, bm_start)\n        p.insert(insert_at + 1, bm_end)\n        insert_at += 2\n        next_bookmark_id += 1\n    return next_bookmark_id\n\n\ndef rebuild_heading_with_tab(p: ET.Element, prefix: str, title: str) -> None:\n"""
if old_helpers not in text:
    raise SystemExit('helper block not found')
text = text.replace(old_helpers, new_helpers, 1)

start = text.index("def normalize_body(body: ET.Element) -> None:\n")
end = text.index("\n\ndef process_docx(input_path: Path, output_path: Path) -> None:\n", start)
new_normalize = '''def normalize_body(body: ET.Element) -> None:
    unwrap_figure_tables(body)

    current_chapter_label: str | None = None
    equation_no = 0
    table_no = 0
    figure_no = 0
    table_labels: dict[str, str] = {}
    figure_labels: dict[str, str] = {}
    first_abstract_idx = None
    pending_eq_label: str | None = None
    pending_generic_labels: list[str] = []
    bookmark_map: dict[str, str] = {}
    eq_display_map: dict[str, str] = {}
    next_bookmark_id = 1

    paragraphs = list(body.findall(qn('w', 'p')))
    for idx, p in enumerate(paragraphs):
        style = paragraph_style(p)
        text = paragraph_text(p)
        marker_label = extract_eq_label_marker(text)
        if marker_label:
            pending_eq_label = marker_label
            if p in list(body):
                body.remove(p)
            continue

        generic_labels = extract_generic_label_markers(text)
        marker_only_text = LABEL_RE.sub('', text).strip()
        if generic_labels and marker_only_text == '':
            prev_idx = prev_nonblank_index(paragraphs, idx - 1)
            attached = False
            if prev_idx is not None:
                prev_p = paragraphs[prev_idx]
                prev_style = paragraph_style(prev_p)
                if prev_p in list(body) and prev_style in {'Heading1', 'Heading2', 'Heading3', 'TableCaption', 'ImageCaption'}:
                    next_bookmark_id = attach_bookmarks_to_paragraph(prev_p, generic_labels, bookmark_map, next_bookmark_id)
                    attached = True
            if not attached:
                pending_generic_labels.extend(generic_labels)
            if p in list(body):
                body.remove(p)
            continue
        elif generic_labels:
            strip_label_markers_in_paragraph(p)
            text = paragraph_text(p)
            next_bookmark_id = attach_bookmarks_to_paragraph(p, generic_labels, bookmark_map, next_bookmark_id)

        text = normalize_heading_separator(p, style, text)
        if first_abstract_idx is None and style == 'Heading1' and text in {'摘 要', '摘要', 'Abstract'}:
            first_abstract_idx = idx
        if style == 'Heading1':
            if text in {'摘 要', '摘要'}:
                set_paragraph_style(p, 'AbstractTitleCN')
            elif text == 'Abstract':
                set_paragraph_style(p, 'AbstractTitleEN')
            elif text in {'后 记', '后记', '参考文献'} or is_research_outputs_heading(text) or is_appendix_heading1(text) or is_body_heading1(text):
                pass
            else:
                set_paragraph_style(p, 'Normal')
        elif style == 'Heading2':
            if not is_body_heading2(text):
                set_paragraph_style(p, 'Normal')
        elif style == 'Heading3':
            if not is_body_heading3(text):
                set_paragraph_style(p, 'Normal')

        if pending_generic_labels and p in list(body):
            next_bookmark_id = attach_bookmarks_to_paragraph(p, pending_generic_labels, bookmark_map, next_bookmark_id)
            pending_generic_labels = []

        if paragraph_has_math(p):
            if paragraph_has_math_para(p):
                set_paragraph_style(p, 'EquationBlock')
            else:
                set_paragraph_style(p, 'Normal')

        chapter_label = chapter_label_from_heading(text)
        if chapter_label:
            current_chapter_label = chapter_label
            equation_no = 0
            table_no = 0
            figure_no = 0
            table_labels = {}
            figure_labels = {}
        elif current_chapter_label and paragraph_has_math(p):
            equation_no += 1
            if current_chapter_label.isdigit():
                eq_label = f'（{current_chapter_label}.{equation_no}）'
            else:
                eq_label = f'（{current_chapter_label}{equation_no}）'
            bookmark_name = None
            bookmark_id = None
            if pending_eq_label:
                bookmark_name = sanitize_bookmark_name(pending_eq_label)
                bookmark_map[pending_eq_label] = bookmark_name
                eq_display_map[pending_eq_label] = f'Equation{eq_label}'
                bookmark_id = next_bookmark_id
                next_bookmark_id += 1
                pending_eq_label = None
            append_equation_number(p, eq_label, bookmark_name, bookmark_id)
        elif current_chapter_label and re.match(r'^（[A-Z]\.\d+）$', text):
            set_paragraph_text(p, re.sub(r'^（([A-Z])\.(\d+)）$', r'（\1\2）', text))
        elif current_chapter_label and style == 'TableCaption' and text:
            explicit_continued, title = clean_caption_title(text, '表')
            if explicit_continued or title in table_labels:
                label = table_labels.get(title, make_caption_label(current_chapter_label, max(table_no, 1)))
                set_paragraph_text(p, format_caption_text('表', label, title, True))
            else:
                table_no += 1
                label = make_caption_label(current_chapter_label, table_no)
                table_labels[title] = label
                set_paragraph_text(p, format_caption_text('表', label, title, False))
        elif current_chapter_label and style == 'ImageCaption' and text:
            explicit_continued, title = clean_caption_title(text, '图')
            if explicit_continued or title in figure_labels:
                label = figure_labels.get(title, make_caption_label(current_chapter_label, max(figure_no, 1)))
                set_paragraph_text(p, format_caption_text('图', label, title, True))
            else:
                figure_no += 1
                label = make_caption_label(current_chapter_label, figure_no)
                figure_labels[title] = label
                set_paragraph_text(p, format_caption_text('图', label, title, False))

    children = list(body)
    content_start_idx = 0
    for probe_idx, probe_child in enumerate(children):
        if probe_child.tag != qn('w', 'p'):
            continue
        probe_style = paragraph_style(probe_child)
        if probe_style in {'AbstractTitleCN', 'AbstractTitleEN'}:
            content_start_idx = probe_idx
            break
    for idx, child in enumerate(children):
        if child.tag == qn('w', 'tbl') and is_stylable_body_table(children, idx, content_start_idx):
            apply_three_line_table_style(child)
        if child.tag != qn('w', 'p'):
            continue
        replace_xref_placeholders_in_paragraph(child, bookmark_map, eq_display_map)
        style = paragraph_style(child)
        if style == 'TableCaption':
            prev_idx = prev_nonblank_index(children, idx - 1)
            if prev_idx is not None and children[prev_idx].tag == qn('w', 'p') and is_unit_line(paragraph_text(children[prev_idx])):
                unit_para = children[prev_idx]
                set_paragraph_style(unit_para, 'TableMetaLine')
                body.remove(unit_para)
                insert_pos = list(body).index(child) + 1
                body.insert(insert_pos, unit_para)
                children = list(body)
                idx = children.index(child)

            next_idx = next_nonblank_index(children, idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'p'):
                text = paragraph_text(children[next_idx])
                if is_unit_line(text):
                    set_paragraph_style(children[next_idx], 'TableMetaLine')
                    next_idx = next_nonblank_index(children, next_idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'tbl'):
                apply_three_line_table_style(children[next_idx])
                source_idx = next_nonblank_index(children, next_idx + 1)
                if source_idx is not None and children[source_idx].tag == qn('w', 'p') and is_source_line(paragraph_text(children[source_idx])):
                    set_paragraph_style(children[source_idx], 'SourceNote')
        elif style == 'ImageCaption':
            next_idx = next_nonblank_index(children, idx + 1)
            if next_idx is not None and children[next_idx].tag == qn('w', 'p') and is_source_line(paragraph_text(children[next_idx])):
                set_paragraph_style(children[next_idx], 'SourceNote')
'''
text = text[:start] + new_normalize + text[end:]
p.write_text(text)
print('patched', p)
