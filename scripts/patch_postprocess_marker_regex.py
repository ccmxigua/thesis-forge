from pathlib import Path

p = Path('/Users/cheng/clawd/deliverables/天津财经大学博士毕业论文规范tjufe-pandoc-docx-kit-20260326/scripts/postprocess_tjufe_docx.py')
text = p.read_text()
text = text.replace("LABEL_RE = re.compile(r'\\[\\[\\[TJUFE_LABEL:([^\\]]+)\\]\\]\\]')", "LABEL_RE = re.compile(r'TJUFE_LABEL__([^_]+(?:_[^_]+)*)__')")
p.write_text(text)
print('patched', p)
