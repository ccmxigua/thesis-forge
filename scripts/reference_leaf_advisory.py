"""Reference-only leaf heuristic for a hash-bound frozen PDF, not a verdict."""
from io import BytesIO
from zipfile import ZipFile
from lxml import etree as E
import re

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def q(p,x):return p.xpath(x,namespaces={'w':W})
def inspect(raw):
 if not isinstance(raw,bytes) or len(raw)>10_000_000:raise ValueError()
 with ZipFile(BytesIO(raw)) as z:
  names=z.namelist()
  if len(names)>256 or len(names)!=len(set(names)) or sum(i.file_size for i in z.infolist())>30_000_000 or not {'word/document.xml','[Content_Types].xml'}<=set(names):raise ValueError()
  roots={}
  for n in names:
   if n.startswith('/') or '..' in n.split('/') or '\\' in n or n.lower().endswith(('.bin','.exe','.dll','.js')) or any(x in n.lower() for x in ('vbaproject','/embeddings/','/activex/')):raise ValueError()
   if n.endswith(('.xml','.rels')):
    r=E.fromstring(z.read(n),parser=E.XMLParser(resolve_entities=False,no_network=True))
    if r.getroottree().docinfo.doctype or any(x.get('TargetMode','').lower()=='external' or E.QName(x).localname in ('altChunk','object','control') for x in r.iter() if isinstance(x.tag,str)):raise ValueError()
    if re.search(r'\b(?:DDE(?:AUTO)?|INCLUDE(?:TEXT|PICTURE)|LINK)\b',' '.join(q(r,'//w:instrText/text()|//w:fldSimple/@w:instr')),re.I):raise ValueError()
    roots[n]=r
 d=roots['word/document.xml']; text=lambda p:''.join(q(p,'.//w:t/text()')).strip()
 body=d.find('{'+W+'}body')
 if body is None:raise ValueError()
 nodes=list(body); terminal=[]
 parity=bool(set(q(d,'//w:sectPr/w:type/@w:val')) & {'oddPage','evenPage'})
 styles=roots.get('word/styles.xml')
 outline_levels={}
 if styles is not None:
  for style in q(styles,'./w:style[@w:type="paragraph"]'):
   sid=style.get('{'+W+'}styleId')
   vals=q(style,'./w:pPr/w:outlineLvl/@w:val')
   if sid and len(vals)==1 and vals[0] in tuple(map(str,range(9))):
    outline_levels[sid]=vals[0]
 def bibliography_heading(node):
  if E.QName(node).localname!='p' or text(node)!='参考文献':return False
  sid=q(node,'./w:pPr/w:pStyle/@w:val'); override=q(node,'./w:pPr/w:outlineLvl/@w:val')
  return len(sid)==1 and sid[0] in outline_levels and (not override or override==[outline_levels[sid[0]]])
 def bound_bibliography(last_idx, last_ord):
  # Accept only an unbroken visible [1]…[N] range under an actual
  # bibliographic heading in body XML; the running header is not evidence.
  first=last_idx-last_ord+1
  if last_ord<1 or first<1 or not bibliography_heading(nodes[first-1]):return False
  for n,entry in enumerate(nodes[first:last_idx+1],1):
   if E.QName(entry).localname!='p' or q(entry,'./w:pPr/w:sectPr|./w:pPr/w:outlineLvl'):return False
   if any(t in outline_levels for t in q(entry,'./w:pPr/w:pStyle/@w:val')):return False
   if not re.match(r'^\['+str(n)+r'\]\s*\S',text(entry)):return False
  return True
 for idx,(a,b,c,h) in enumerate(zip(nodes,nodes[1:],nodes[2:],nodes[3:])):
  if [E.QName(x).localname for x in (a,b,c,h)]!=['p']*4:continue
  m=re.match(r'^\[(\d+)\]\s*\S',text(a))
  if not m or text(h)!='附录' or text(b) or text(c):continue
  if [E.QName(x).localname for x in b]!=['pPr'] or q(b,'./w:pPr/w:sectPr'):continue
  if q(b,'./w:pPr/w:pageBreakBefore') and q(b,'./w:pPr/w:pageBreakBefore/@w:val') not in (['0'],['off'],['false']):continue
  if not q(c,'./w:pPr/w:sectPr') or q(c,'./w:pPr/w:sectPr/w:type/@w:val') not in ([],['nextPage']):continue
  if not q(h,'./w:pPr/w:pageBreakBefore') or q(h,'./w:pPr/w:pageBreakBefore/@w:val') in (['0'],['off'],['false']):continue
  if any(q(n,'.//w:br[@w:type="page"]') for n in (a,b,c,h)):continue
  last_ord=int(m.group(1))
  if bound_bibliography(idx,last_ord):terminal.append(last_ord)
 return parity,terminal
def regions(p):
 zones=[[],[],[]]
 for block in p.get_text('dict')['blocks']:
  for line in block.get('lines',[]):
   s=''.join(x['text'] for x in line['spans']).strip()
   if s:
    x,y=line['bbox'][:2]; y=y/p.rect.height
    zones[0 if y<.082 else 2 if y>.908 else 1].append((round(y,4),x,s))
 return [[v[2] for v in sorted(z)] for z in zones]
