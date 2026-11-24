"""Exact generated-layer removal. Caller must bind all marker text to its ledger."""
from lxml import etree
import re
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W='{'+NS['w']+'}'
TITLE='待定位人工处理（审查草稿，不可提交）'
EMPTY='本次没有待定位项目；请在正文中按 MR 编号逐项处理。'
MARKER=re.compile(r'^【(MR-\d{4})｜人工待审】')
def introduction(count):
 return (f'本次共有 {count} 项人工审查记录。明确位置的标记置于对应正文旁；'
         '下列项目尚无可靠位置，请按编号核对并自行处理。红字不替代原文，也不代表问题已经解决。')
def text(node):return ''.join(node.xpath('.//w:t/text()',namespaces=NS))
def plain(node):
 if node.tag!=W+'p' or node.xpath('.//w:sectPr|.//w:drawing|.//w:object|.//w:fldChar|.//w:instrText|.//w:bookmarkStart|.//w:bookmarkEnd',namespaces=NS):
  raise ValueError('generated manual layer contains non-text structure; retain it')
 if any(c.tag not in {W+'pPr',W+'r'} for c in node):
  raise ValueError('generated manual layer contains unsupported children')
 if any(c.tag not in {W+'rPr',W+'t'} for run in node.findall(W+'r') for c in run):
  raise ValueError('generated manual layer contains unsupported run content')
def remove_manual_layer(root, expected):
 """Validate the entire removal before mutating; leave all other XML intact."""
 if not expected:return {'removed_marker_ids':[],'removed_paragraph_texts':[]}
 body=root.find('w:body',NS)
 if body is None:raise ValueError('missing document body')
 children=list(body)
 if len(children)<4 or text(children[0])!=TITLE or text(children[1])!=introduction(len(expected)):
  raise ValueError('manual front wrapper does not match exact generated text')
 for n in children[:2]:plain(n)
 remove=list(children[:2]);front_ids=[];index=2
 while index<len(children):
  n=children[index];match=MARKER.match(text(n))
  if not match:break
  front_ids.append(match.group(1));index+=1
 if index<len(children) and text(children[index])==EMPTY:
  if front_ids:raise ValueError('empty-front marker conflicts with actual markers')
  plain(children[index]);remove.append(children[index]);index+=1
 if index>=len(children):raise ValueError('missing generated front page break')
 br=children[index]
 if br.tag!=W+'p' or len(br)!=1 or br[0].tag!=W+'r' or len(br[0])!=1 or br[0][0].tag!=W+'br' or dict(br[0][0].attrib)!={W+'type':'page'}:
  raise ValueError('manual front page break is not the exact generated structure')
 remove.append(br)
 found=[]
 for node in root.xpath('.//w:p',namespaces=NS):
  match=MARKER.match(text(node))
  if not match:continue
  key=match.group(1)
  if key not in expected or text(node)!=expected[key]:raise ValueError('manual marker text differs from complete ledger')
  if node.getparent() is not body:raise ValueError("nested manual marker cannot be removed without changing container structure")
  plain(node);found.append(key);remove.append(node)
 if len(found)!=len(set(found)) or set(found)!=set(expected):
  raise ValueError('manual marker coverage is incomplete or duplicated')
 # Empty front statement is only valid when every marker is outside the front.
 if not front_ids and children[2] not in remove:
  raise ValueError('missing exact empty-front statement')
 removed_texts=[text(n) for n in remove if n.getparent() is body]
 for n in remove:n.getparent().remove(n)
 return {'removed_marker_ids':found,'removed_paragraph_texts':removed_texts}
