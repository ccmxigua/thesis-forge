"""Apply an explicit host-reviewed, exact-source figure group pagination plan.

No group discovery, render call, geometry change, text edit or compliance promotion.
"""
from __future__ import annotations
import argparse,hashlib,json,re,os,tempfile,io
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from lxml import etree
from docx import Document
from docx.oxml.ns import qn
from semantic_contract import strict_json_read,sha256_json

PROTOCOL='explicit_figure_group_pagination_v1'
def xml_sha(node):return hashlib.sha256(etree.tostring(node,method='c14n')).hexdigest()
def media_hashes(doc,node):
 ids=node.xpath('.//a:blip/@r:embed')
 if node.xpath('.//a:blip/@r:link'):raise ValueError('external image is not a bound embedded object')
 result={}
 for rid in ids:
  part=doc.part.related_parts.get(rid)
  if part is None or not part.content_type.startswith('image/'):raise ValueError('missing bound image relationship')
  result[rid]=hashlib.sha256(part.blob).hexdigest()
 return result

def has_page_boundary(doc,node):
 # Conservative refusal even if a direct false overrides an inherited boundary.
 # Never guess which style Word/WPS will resolve, or modify a boundary implicitly.
 styles={s.style_id:s for s in doc.styles}
 pstyle=node.find('w:pPr/w:pStyle',node.nsmap)
 style_id=pstyle.get(qn('w:val')) if pstyle is not None else None
 if style_id is None:
  defaults=[s.style_id for s in doc.styles if s.element.get(qn('w:type'))=='paragraph' and s.element.get(qn('w:default')) in ('1','true','on')]
  if len(defaults)!=1:raise ValueError('missing or ambiguous default paragraph style requires review')
  style_id=defaults[0]
 chain=[node.find('w:pPr',node.nsmap)];seen=set()
 while style_id:
  if style_id in seen:raise ValueError('cyclic style inheritance requires review')
  seen.add(style_id)
  if style_id not in styles:raise ValueError('unresolved paragraph style requires review')
  elem=styles[style_id].element;chain.append(elem.find(qn('w:pPr')))
  base=elem.find(qn('w:basedOn'));style_id=base.get(qn('w:val')) if base is not None else None
 defaults=doc.styles.element.find('w:docDefaults/w:pPrDefault/w:pPr',doc.styles.element.nsmap)
 chain.append(defaults)
 for ppr in chain:
  if ppr is None:continue
  pb=ppr.find(qn('w:pageBreakBefore'))
  if pb is not None and pb.get(qn('w:val'),'1') not in ('0','false','off'):return True
 return False

def apply_bound_group(doc,plan):
 if not isinstance(plan,dict) or plan.get('protocol')!=PROTOCOL:raise ValueError('explicit group plan required')
 if plan.get('document_xml_sha256')!=xml_sha(doc._element):raise ValueError('source XML differs from reviewed plan')
 members=plan.get('members');evidence=plan.get('grouping_evidence')
 if not isinstance(members,list) or len(members)<5 or len(members)%2!=1:raise ValueError('two or more explicit image/subcaption pairs and a total caption required')
 if not isinstance(evidence,dict) or evidence.get('kind')!='host_reviewed_source_relation':raise ValueError('host-reviewed source relation required')
 children=list(doc._element.body);indices=[];nodes=[]
 for i,member in enumerate(members):
  if not isinstance(member,dict):raise ValueError('invalid member')
  index=member.get('body_child');kind=member.get('kind')
  if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(children):raise ValueError('invalid body-child locator')
  expected='caption' if i==len(members)-1 else ('image' if i%2==0 else 'subcaption')
  if kind!=expected:raise ValueError('explicit source member roles do not form the declared group')
  node=children[index]
  if node.tag!=qn('w:p') or member.get('xml_sha256')!=xml_sha(node):raise ValueError('source member differs or is not a paragraph')
  if node.xpath('.//w:sectPr|.//w:fldSimple|.//w:fldChar|.//w:instrText|.//w:object|.//w:br[@w:type="page" or @w:type="column"]'):
   raise ValueError('group crosses a section, explicit break or unsupported field/object')
  if has_page_boundary(doc,node):raise ValueError('group contains direct or inherited page boundary')
  text=''.join(node.xpath('.//w:t/text()')).strip()
  if kind=='image':
   hashes=media_hashes(doc,node)
   if text or not hashes or member.get('media_sha256')!=hashes:raise ValueError('image member lacks exact embedded-media evidence')
  else:
   if node.xpath('.//w:drawing|.//w:pict'):raise ValueError('caption contains unsupported mixed image content')
   if kind=='subcaption' and not re.match(r'^\([A-Za-z0-9]+\)\s*\S',text):raise ValueError('subcaption lacks an explicit source label')
   if kind=='caption' and (not text or evidence.get('source_quote')!=text or evidence.get('caption_body_child')!=index):raise ValueError('total caption does not match exact reviewed source quote')
  indices.append(index);nodes.append(node)
 if indices!=list(range(indices[0],indices[0]+len(indices))):raise ValueError('group is not complete contiguous explicit scope; never cross intervening prose')
 # All guards complete before mutation; only pagination properties are changed.
 for i,node in enumerate(nodes):
  ppr=node.get_or_add_pPr();ppr.get_or_add_keepNext().val=i<len(nodes)-1;ppr.get_or_add_keepLines().val=True
 return {'protocol':PROTOCOL,'status':'configured_not_render_verified','source_document_xml_sha256':plan['document_xml_sha256'],
         'output_document_xml_sha256':xml_sha(doc._element),'plan_sha256':sha256_json(plan),'body_children':indices,
         'visual_verified':False,'submission_ready':False}

def write_bound_copy(input_path,plan_path,output_path,report_path):
 paths=[Path(x) for x in (input_path,plan_path,output_path,report_path)]
 source,plan_file,output,report_file=paths
 if len({x.resolve() for x in paths})!=4 or output.exists() or report_file.exists():raise ValueError('distinct inputs and new outputs required')
 if not output.parent.is_dir() or not report_file.parent.is_dir():raise ValueError('output parent directories must already exist')
 source_bytes=source.read_bytes();plan=strict_json_read(plan_file)
 source_sha=hashlib.sha256(source_bytes).hexdigest()
 if not isinstance(plan,dict) or plan.get('input_docx_sha256')!=source_sha:raise ValueError('input package differs from reviewed plan')
 doc=Document(io.BytesIO(source_bytes));report=apply_bound_group(doc,plan)
 package=io.BytesIO()
 with ZipFile(io.BytesIO(source_bytes)) as src,ZipFile(package,'w',ZIP_DEFLATED) as dst:
  names=src.namelist()
  if len(names)!=len(set(names)):raise ValueError('duplicate ZIP members are unsupported')
  for info in src.infolist():dst.writestr(info,etree.tostring(doc._element,encoding='UTF-8',xml_declaration=True,standalone=True) if info.filename=='word/document.xml' else src.read(info.filename))
 data=package.getvalue()
 report.update(input_docx_sha256=source_sha,output_docx_sha256=hashlib.sha256(data).hexdigest())
 report_data=(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
 # Stage both complete files first. link() publishes each atomically without
 # overwriting a concurrent writer; rollback only our inode on an ordinary error.
 # A crash may leave one artifact: consumers must require the paired hash report.
 staged=[];published=[]
 try:
  for target,content in ((output,data),(report_file,report_data)):
   fd,name=tempfile.mkstemp(prefix='.'+target.name+'.',dir=target.parent)
   tmp=Path(name);staged.append(tmp)
   with os.fdopen(fd,'wb') as f:f.write(content);f.flush();os.fsync(f.fileno())
  for tmp,target in zip(staged,(output,report_file)):
   os.link(tmp,target);published.append((tmp,target))
 except BaseException:
  for tmp,target in reversed(published):
   try:
    if os.path.samestat(tmp.stat(),target.stat()):target.unlink()
   except FileNotFoundError:pass
  raise
 finally:
  for tmp in staged:
   try:tmp.unlink()
   except FileNotFoundError:pass
 return report

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);p.add_argument('plan',type=Path);p.add_argument('output',type=Path);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
 write_bound_copy(a.input,a.plan,a.output,a.report)
if __name__=='__main__':main()
