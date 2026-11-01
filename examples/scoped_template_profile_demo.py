"""Build a synthetic, editable two-profile fixture. Never overwrite inputs."""
from pathlib import Path
import hashlib
import json
import shlex
import sys

from docx import Document


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def build(destination):
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=False)
    doc = Document()
    doc.add_paragraph('Primary research title')
    p = doc.add_paragraph(); p.add_run('——').font.bold = False; p.add_run('合成副题')
    doc.add_paragraph('Synthetic subtitle')
    p = doc.add_paragraph(); p.add_run('Key Words: value').bold = True
    doc.add_paragraph('Table 1 Synthetic data')
    table = doc.add_table(rows=2, cols=2)
    for row, cells in zip(table.rows, [('Method', 'N'), ('Signature algorithm', '12')]):
        for cell, value in zip(row.cells, cells):
            cell.text = value
    doc.add_paragraph('[1] Synthetic example reference。')
    doc.add_paragraph('Synthetic appendix body')
    doc.add_paragraph('Achievements heading')
    doc.add_paragraph('Synthetic content, no real achievements claimed.')
    doc.add_paragraph('Chapter One', style='Heading 1')
    doc.sections[0].header.paragraphs[0].text = 'Old header'
    source = destination/'synthetic.docx'; doc.save(source)
    for name, size, line, width, blank in [('A', 12, 20, .75, 2), ('B', 14, 24, 1.25, 1)]:
        rules = []
        def add(value, role, **props):
            rules.append(dict(id=role, role=role, selector={'text_sha256':digest(value)},
                              evidence=['Fictional '+name+' configuration; caller declaration, not school authority'], **props))
        add('——合成副题', 'thesis_subtitle_zh', font={'size_pt':size+6})
        add('Synthetic subtitle', 'thesis_subtitle_en', **({'display_prefix':'——'} if name=='A' else {}))
        add('Key Words: value', 'keywords_en', keyword={'label':'Key Words:', 'label_font':{'bold':True}, 'value_font':{'bold':False}})
        add('Table 1 Synthetic data', 'table_caption', paragraph={'line_spacing':{'type':'exact','value':line}},
            table={'style':'three_line','top_border_pt':width,'header_border_pt':width,'bottom_border_pt':width,'color':'000000'})
        add('[1] Synthetic example reference。', 'bibliography_entry', terminal={'accepted':['。','.'],'value':'.'})
        add('Synthetic appendix body', 'appendix_body', font={'size_pt':size,'cjk':'SimSun' if name=='A' else 'FangSong'},
            paragraph={'alignment':'justify','first_line_indent_chars':2})
        add('Achievements heading', 'heading_publications', spacer={'count':blank,'paragraph':{'line_spacing':{'type':'exact','value':line}}})
        add('Chapter One','heading_1', header={'side':'default','expected_text_sha256':digest('Old header'),'synchronize_heading':True},font={'latin':'Times New Roman'})
        profile = dict(schema_version='1.0',profile_id='synthetic-'+name,authority='caller_declared',
                       input_sha256=digest(source.read_bytes()),rules=rules)
        path = destination/('profile-'+name+'.json'); path.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n')
        print(shlex.join(['python','scripts/apply_format_spec.py',str(source),str(path),
                          str(destination/('candidate-'+name+'.docx')),
                          '--out-dir',str(destination/('audit-'+name)),'--scoped-profile']))
    return destination


if __name__ == '__main__':
    build(sys.argv[1])
