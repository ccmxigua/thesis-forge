#!/usr/bin/env python3
"""
mapper-v2-to-tjufe.py
读取 V2 schema overlay YAML → 生成天财 Lua filter 兼容的 Pandoc metadata YAML
"""
import sys, yaml, json

def load(path):
    with open(path) as f:
        return yaml.safe_load(f)

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <config-tjufe-overlay.yaml> [output.yaml]", file=sys.stderr)
        sys.exit(2)

    overlay = load(sys.argv[1])
    out = {}

    # --- metadata block ---
    m = overlay.get('metadata', {})
    out['author'] = m.get('author', '')
    out['advisor'] = m.get('advisor', '')
    out['student_id'] = m.get('student_id', '')
    out['cn_title'] = m.get('title', '')
    out['en_title'] = m.get('title_en', '')
    out['cn_subtitle'] = m.get('subtitle', '')
    out['en_subtitle'] = m.get('subtitle_en', '')
    out['submit_date_cn'] = m.get('submit_date', '')
    out['class_no'] = m.get('classification_number', '')
    out['confidentiality'] = m.get('confidentiality_level', '')
    out['discipline'] = m.get('major', '')
    out['college'] = m.get('school', '')

    # doctoral fields
    out['doctoral_subject'] = m.get('major', '')
    out['doctoral_research_direction'] = m.get('research_direction', '')
    out['doctoral_student_name'] = m.get('author', '')
    out['doctoral_defense_date'] = m.get('defense_date', '')
    out['doctoral_degree_date'] = m.get('degree_conferral_date', '')
    out['doctoral_apply_degree'] = m.get('degree_display', '')
    out['doctoral_college'] = m.get('school', '')
    out['doctoral_major'] = m.get('major', '')
    # 从 title_page.info_fields 中按 label 提取，不再硬编码
    tp = overlay.get('title_page', {})
    info_map = {f['label']: f['value'] for f in tp.get('info_fields', [])}
    out['doctoral_committee_chair'] = info_map.get('答辩委员会主席', '待定')
    out['doctoral_reviewers'] = info_map.get('论文评阅人', '匿名评审')

    # cover
    cover = overlay.get('cover', {})
    out['insert_cover'] = cover.get('enabled', False)

    # title page (tp already loaded above)
    out['insert_title_page'] = tp.get('enabled', False)
    out['degree_type'] = tp.get('degree_line', m.get('degree_display', ''))
    out['cover_degree_line'] = tp.get('degree_line', '')
    out['title_page_info_rows'] = tp.get('info_fields', [])

    # toc
    toc = overlay.get('toc', {})
    out['insert_toc'] = toc.get('enabled', False)

    # acknowledgements / signature
    ack = overlay.get('acknowledgements', {})
    sig = ack.get('signature', {})
    if sig.get('enabled', False):
        out['ack_signature_name'] = m.get('author', '')
        out['ack_signature_date'] = m.get('submit_date', '')

    # page margins (for reference, though the Lua filter reads some from metadata)
    page = overlay.get('page', {}).get('margins', {})
    out['page_left_margin'] = page.get('left', 1134)
    out['page_right_margin'] = page.get('right', 1134)
    out['page_top_margin'] = page.get('top', 1134)
    out['page_bottom_margin'] = page.get('bottom', 850)

    # --- Font sizes for reference ---
    fs = overlay.get('font_sizes', {})
    out['font_size_body'] = fs.get('body', 12)

    # --- Schema version marker ---
    out['tjufe_config_source'] = 'config-schema-v2 + config-tjufe-overlay.yaml'

    # Write output
    out_path = sys.argv[2] if len(sys.argv) > 2 else '/dev/stdout'
    with open(out_path, 'w') if out_path != '/dev/stdout' else sys.stdout as f:
        yaml.dump(out, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=120)

if __name__ == '__main__':
    main()
