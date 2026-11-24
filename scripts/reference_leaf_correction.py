"""Opt-in causal experiment on a bounded DOCX. Not Word/WPS or release authority."""
from __future__ import annotations

import argparse
from copy import copy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import os
import posixpath
from math import ceil, floor
import re
import shutil
import sys
import tempfile
from zipfile import BadZipFile, ZipFile

import fitz
from lxml import etree

from guarded_reference_leaf import (DOCX_LIMIT, PDF_LIMIT, digest, exact_empty,
                                   exclusive_xml, parse_docx, require, scope)
from process_runner import run_process
from reference_leaf_advisory import W, inspect, q, regions
from semantic_contract import sha256_file, strict_json_dumps

# Approved by review of the exact observed Linux runtime, not caller input.
# No executable is run until its complete local asset tree matches this pin.
WRAPPER_SHA = '35c5a2736f68e6370a7b9fe5082e66bc438c1a8de11261d6c4bc847073ced45b'
OFFICE_INVENTORY = (3187, 535037790, 'f295e6c2999037c26f6092e79271c70dfe4fd72bc5cb2294defcc189d525a4dc')
FOOTER_START = .908


def limited_read(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), 'expected regular input')
    with path.open('rb') as f:
        data = f.read(limit+1)
    require(len(data) <= limit, 'artifact exceeds bounded read')
    return data


def runtime_location_trusted(path: Path, *, ancestors: bool=False) -> None:
    for part in (path,*path.parents) if ancestors else (path,):
        require(not part.is_symlink() and not os.access(part,os.W_OK)
                and not(part.stat().st_mode & 0o022),
                'pinned renderer found in a writable location or symlink')


def office_inventory(root: Path) -> tuple[int, int, str]:
    """Tree pin covers path and contents, not platform/fonts or authenticity."""
    require(root.is_dir() and not root.is_symlink(), 'pinned renderer installation is unavailable')
    runtime_location_trusted(root,ancestors=True)
    entries = sorted(root.rglob('*'),key=lambda p:p.relative_to(root).as_posix())
    require(len(entries) <= 5_000,'renderer inventory exceeds the expected bounds')
    d=sha256(); files=size=0
    for p in entries:
        require(not p.is_symlink() and (p.is_dir() or p.is_file()), 'unsupported renderer tree entry')
        runtime_location_trusted(p)
        if p.is_file():
            files += 1; size += p.stat().st_size
            require(files <= OFFICE_INVENTORY[0] and size <= OFFICE_INVENTORY[1],
                    'renderer inventory exceeds the exact pin')
            d.update(p.relative_to(root).as_posix().encode()+b'\0'+sha256_file(p).encode()+b'\n')
    return files,size,d.hexdigest()


def select_office() -> tuple[Path, Path]:
    # Discovery alone is never trust. This exact supported launcher is read as
    # data, not executed; a different build or installer falls back to diagnose.
    located = shutil.which('soffice')
    require(located is not None, 'not the pinned renderer: no known LibreOffice launcher')
    wrapper=Path(located).absolute()
    require(not wrapper.is_symlink() and digest(limited_read(wrapper,8192))==WRAPPER_SHA,
            'not the pinned renderer: launcher bytes differ')
    runtime_location_trusted(wrapper,ancestors=True)
    root=(wrapper.parent/'../../native/libreoffice-headless/libreoffice').resolve(strict=True)
    require(office_inventory(root)==OFFICE_INVENTORY, 'not the pinned renderer: Office runtime tree differs')
    executable=root/'program/soffice'
    require(executable.is_file() and os.access(executable,os.X_OK),'pinned renderer is not executable')
    return root,executable


OPC_CT='http://schemas.openxmlformats.org/package/2006/content-types'
OPC_REL='http://schemas.openxmlformats.org/package/2006/relationships'
OD_REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
WORD_MIME='application/vnd.openxmlformats-officedocument.wordprocessingml.'
DEFAULT_TYPES={'jpeg':'image/jpeg','rels':'application/vnd.openxmlformats-package.relationships+xml',
               'xml':'application/xml'}
WORD_TYPES={name:WORD_MIME+kind+'+xml' for name,kind in (
    ('word/document.xml','document.main'),('word/fontTable.xml','fontTable'),
    ('word/numbering.xml','numbering'),('word/settings.xml','settings'),
    ('word/styles.xml','styles'),('word/webSettings.xml','webSettings'))}
EXTRA_OVERRIDES={
 'docProps/app.xml':'application/vnd.openxmlformats-officedocument.extended-properties+xml',
 'docProps/core.xml':'application/vnd.openxmlformats-package.core-properties+xml',
 'customXml/itemProps1.xml':'application/vnd.openxmlformats-officedocument.customXmlProperties+xml',
 'word/stylesWithEffects.xml':'application/vnd.ms-word.stylesWithEffects+xml',
 'word/theme/theme1.xml':'application/vnd.openxmlformats-officedocument.theme+xml'}
REL_PACK={
  OPC_REL+'/metadata/core-properties':r'docProps/core\.xml',
  OPC_REL+'/metadata/thumbnail':r'docProps/thumbnail\.jpeg',
  OD_REL+'/extended-properties':r'docProps/app\.xml',
  OD_REL+'/officeDocument':r'word/document\.xml',
}
REL_DOCUMENT={OD_REL+'/'+key:pattern for key,pattern in (
 ('styles',r'word/styles\.xml'),('settings',r'word/settings\.xml'),
 ('fontTable',r'word/fontTable\.xml'),('webSettings',r'word/webSettings\.xml'),
 ('theme',r'word/theme/theme1\.xml'),('numbering',r'word/numbering\.xml'),
 ('customXml',r'customXml/item1\.xml'),('header',r'word/header[1-9][0-9]*\.xml'),
 ('footer',r'word/footer[1-9][0-9]*\.xml'))}
REL_DOCUMENT['http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects']=r'word/stylesWithEffects\.xml'
REL_FILES={'_rels/.rels':('',REL_PACK),'word/_rels/document.xml.rels':('word',REL_DOCUMENT),
   'customXml/_rels/item1.xml.rels':('customXml',{OD_REL+'/customXmlProps':r'customXml/itemProps1\.xml'})}


def xml_part(z: ZipFile,name: str):
    root=etree.fromstring(z.read(name),parser=etree.XMLParser(resolve_entities=False,no_network=True))
    require(not root.getroottree().docinfo.doctype,'DTD is outside canonical supported DOCX')
    return root


def inspect_canonical_opc(z: ZipFile) -> None:
    names=set(z.namelist())
    messages='not a canonical supported DOCX content/relationship set'
    require({'[Content_Types].xml','_rels/.rels','word/document.xml'}<=names,messages)
    declared={k:v for k,v in {**WORD_TYPES,**EXTRA_OVERRIDES}.items() if k in names}
    for name in names:
        m=re.fullmatch(r'word/(header|footer)[1-9][0-9]*\.xml',name)
        if m:declared[name]=WORD_MIME+m[1]+'+xml'
    require(names <= set(declared)|set(REL_FILES)|{
             '[Content_Types].xml','customXml/item1.xml','docProps/thumbnail.jpeg'},messages)
    types=xml_part(z,'[Content_Types].xml')
    require(types.tag==f'{{{OPC_CT}}}Types',messages)
    overrides={}; defaults={}
    for item in types:
        require(isinstance(item.tag,str) and not len(item),messages)
        if item.tag==f'{{{OPC_CT}}}Override' and set(item.attrib)=={'PartName','ContentType'}:
            raw=item.get('PartName'); name=raw[1:]
            require(raw.startswith('/') and name not in overrides, messages)
            overrides[name]=item.get('ContentType')
        elif item.tag==f'{{{OPC_CT}}}Default' and set(item.attrib)=={'Extension','ContentType'}:
            ext=item.get('Extension')
            require(ext in DEFAULT_TYPES and ext not in defaults and DEFAULT_TYPES[ext]==item.get('ContentType'),messages)
            defaults[ext]=item.get('ContentType')
        else:raise ValueError(messages)
    require(overrides==declared and overrides.get('word/document.xml')==WORD_TYPES['word/document.xml'],messages)
    for name in names & set(REL_FILES):
        base,policy=REL_FILES[name]; relations=xml_part(z,name)
        require(relations.tag==f'{{{OPC_REL}}}Relationships',messages)
        used=set(); mains=0
        for node in relations:
            require(node.tag==f'{{{OPC_REL}}}Relationship' and set(node.attrib)=={'Id','Target','Type'}
                    and not len(node) and bool(re.fullmatch(r'rId[1-9][0-9]*',node.get('Id','')))
                    and node.get('Id') not in used,messages)
            used.add(node.get('Id'))
            typ=node.get('Type');raw=node.get('Target')
            require(typ in policy and bool(re.fullmatch(r'[a-zA-Z0-9/._-]+',raw))
                    and not raw.startswith('/'),messages)
            dest=posixpath.normpath(posixpath.join(base,raw))
            require(dest in names and bool(re.fullmatch(policy[typ],dest)),messages)
            if typ==OD_REL+'/officeDocument':mains+=1
        if name=='_rels/.rels':require(mains==1,messages)


def supported_docx(data: bytes) -> int:
    parity,terms=inspect(data)
    require(not parity and len(terms)==1,'not a unique supported reference-to-appendix boundary')
    with ZipFile(BytesIO(data)) as z:
        inspect_canonical_opc(z)
        for name in z.namelist():
            if not name.endswith(('.xml','.rels')):continue
            item=xml_part(z,name)
            forbidden={'del','ins','moveFrom','moveTo','altChunk','contentPart','object','drawing',
                       'pict','tbl','control','fldChar','bookmarkStart','bookmarkEnd','commentRangeStart',
                       'commentRangeEnd','commentReference','oMath','oMathPara','AlternateContent',
                       'hyperlink','dataBinding'}
            require(not any(isinstance(n.tag,str) and etree.QName(n).localname in forbidden for n in item.iter()),
                    'objects, revisions, bookmarks, complex fields or compatibility variants are out of scope')
            fields=q(item,'//w:fldSimple/@w:instr|//w:instrText/text()')
            require(all(re.fullmatch(r'PAGE',s.strip(),re.I) for s in fields),
                    'only a simple literal PAGE counter is supported')
    return terms[0]


def render(raw: bytes, scratch: Path, executable: Path) -> tuple[bytes,dict]:
    scratch.mkdir(mode=0o700)
    for ns in ('home','tmp'):
        (scratch/ns).mkdir(mode=0o700)
    source=scratch/'render-input.docx'
    source.write_bytes(raw)
    initial=sha256_file(source)
    result=run_process([str(executable),'--headless','--norestore','--nodefault','--nofirststartwizard',
                       '-env:UserInstallation='+(scratch/'profile').as_uri(),
                        '--convert-to','pdf:writer_pdf_Export','--outdir',str(scratch),str(source)],
                       cwd=scratch,timeout=55,
                       env={'PATH':'/usr/bin:/bin','HOME':str(scratch/'home'),'TMPDIR':str(scratch/'tmp'),
                            'LANG':'C.UTF-8','SAL_USE_VCLPLUGIN':'svp'})
    require(result.returncode==0, 'pinned local renderer failed (exit '+str(result.returncode)+')')
    require(sha256_file(source)==initial, 'renderer changed the exact temporary DOCX')
    pdf=limited_read(scratch/'render-input.pdf',PDF_LIMIT)
    return pdf,{'input_sha256':initial,'pdf_sha256':digest(pdf),'process_exit':result.returncode,
                'output_observed_during_execution':True}


def remove_one(raw: bytes, observed: dict) -> tuple[bytes,dict]:
    with ZipFile(BytesIO(raw)) as old:
        doc=parse_docx(old); body=doc.find('{'+W+'}body')
        nodes=list(body); idx=observed['body_child_index']
        require(0 < idx < len(nodes)-2 and exact_empty(nodes[idx])
                and digest(exclusive_xml(nodes[idx]))==observed['paragraph_c14n_sha256'],
                'empty non-section paragraph no longer matches the bound XML')
        keep=[exclusive_xml(n) for i,n in enumerate(nodes) if i!=idx]
        neighbours=[digest(exclusive_xml(nodes[i])) for i in (idx-1,idx+1,idx+2)]
        body.remove(nodes[idx]); buf=BytesIO()
        with ZipFile(buf,'w') as dest:
            for zi in old.infolist():
                data=etree.tostring(doc,encoding='UTF-8',xml_declaration=True,standalone=True) if zi.filename=='word/document.xml' else old.read(zi)
                dest.writestr(copy(zi),data)
        with ZipFile(BytesIO(buf.getvalue())) as new:
            require(new.namelist()==old.namelist() and new.testzip() is None, 'output ZIP inventory changed')
            changed=[n for n in old.namelist() if old.read(n)!=new.read(n)]
            reopened=parse_docx(new)
            require(changed==['word/document.xml'] and exclusive_xml(doc)==exclusive_xml(reopened)
                    and [exclusive_xml(n) for n in reopened.find('{'+W+'}body')]==keep,
                    'a different OPC or XML change occurred')
        return buf.getvalue(),{'changed_opc_members':changed,'unchanged_neighbour_sha256':neighbours}


def bounded_layout_pages(pdf: fitz.Document) -> int:
    require(2 <= len(pdf) <= 30, 'page population out of diagnostic bounds')
    for p in pdf:
        require(300 <= p.rect.width <= 1000 and 300 <= p.rect.height <= 1400
                and p.rotation == 0,'unsupported page geometry')
        require(not p.get_links() and p.first_annot is None and p.first_widget is None,
                'non-text PDF annotations or interactive objects are unsupported')
    return len(pdf)


def numeric_footer_box(page: fitz.Page, text: str) -> fitz.Rect:
    boxes=[]
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines",[]):
            box=fitz.Rect(line["bbox"])
            value="".join(span["text"] for span in line["spans"]).strip()
            if value==text and box.y0/page.rect.height>FOOTER_START:
                boxes.append(box)
    require(len(boxes)==1 and 0<boxes[0].width<80 and 0<boxes[0].height<40,
            'unable to localize one footer number')
    return boxes[0]


def match_except_finite_footer_digits(old: fitz.Page,new: fitz.Page,before: str,after: str) -> None:
    a=numeric_footer_box(old,before); b=numeric_footer_box(new,after)
    require(abs((a.x0+a.x1)-(b.x0+b.x1))<=6 and abs(a.y0-b.y0)<2 and abs(a.y1-b.y1)<2,
            'footer position changed, not just numerals')
    # Permit differences only in the tight glyph rectangle (small allowance
    # for anti-aliasing), not a blanket exception for the whole footer band.
    mask=a|b
    # For masked pages, conservatively refuse vector/image drawing at any
    # location. A stroke outside the glyph coordinates can still paint into
    # the pixel/anti-alias allowance; drawing bounds alone are insufficient.
    require(all(not page.get_drawings() and not page.get_image_info() for page in (old,new)),
            'unrelated subsequent page has graphics; footer number overlaps or pixel mask is unverifiable')
    x0=max(0,floor(mask.x0)-1);y0=max(0,floor(mask.y0)-1)
    x1=ceil(mask.x1)+1;y1=ceil(mask.y1)+1
    op=old.get_pixmap(); np=new.get_pixmap()
    require((op.width,op.height,op.n,op.stride)==(np.width,np.height,np.n,np.stride)
            and x1<=op.width and y1<=op.height,'inconsistent footer/page raster geometry')
    xa=op.n*x0; xb=op.n*x1; stride=op.stride
    ob=op.samples; nb=np.samples
    for k in range(op.height):
        line=k*stride
        if y0<=k<y1:
            same=ob[line:line+xa]==nb[line:line+xa] and ob[line+xb:line+stride]==nb[line+xb:line+stride]
        else:
            same=ob[line:line+stride]==nb[line:line+stride]
        require(same,'unrelated subsequent page changed outside the exact footer numerals')


def verify_layout_change(before: bytes, after: bytes, page: int) -> list[int]:
    """Every unaffected page must match; renumbered footer is the only exception."""
    with fitz.open(stream=before,filetype='pdf') as a,fitz.open(stream=after,filetype='pdf') as b:
        n=bounded_layout_pages(a); m=bounded_layout_pages(b)
        require(n==m+1 and 1<page<n, 'actual rerender did not remove exactly one interior page')
        require(regions(b[page-1])[1] and regions(b[page-1])[1][0]=='附录','next page is not exact appendix')
        for i in range(m):
            old=a[i if i<page-1 else i+1]; new=b[i]
            require(old.rect==new.rect,'unrelated page geometry changed')
            if i<page-1:
                require(old.get_text()==new.get_text()
                        and old.get_pixmap().samples==new.get_pixmap().samples,'unrelated preceding page changed')
            else:
                ro,rn=regions(old),regions(new)
                require(ro[:2]==rn[:2] and len(ro[2])==len(rn[2])==1
                        and ro[2][0].isdigit() and rn[2][0].isdigit()
                        and int(ro[2][0])==int(rn[2][0])+1,'unrelated content or expected numeric footer changed')
                match_except_finite_footer_digits(old,new,ro[2][0],rn[2][0])
        return [n,m]


def publish(stage: Path, target: Path, payloads: dict[str,bytes]) -> None:
    """Publish complete fresh output only; never replace or use existing results."""
    frozen=stage/'verified-artifacts'; frozen.mkdir()
    for name,data in payloads.items():
        with (frozen/name).open('xb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
    target.mkdir(mode=0o700)
    created=[]
    try:
        for name in payloads:
            os.link(frozen/name,target/name)
            created.append(target/name)
        # The last linked artifact is the complete verification receipt.
        fd=os.open(target,os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        parent_fd=os.open(target.parent,os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(parent_fd)
        finally:os.close(parent_fd)
    except OSError:
        for p in created:
            if p.exists() and os.path.samefile(p,frozen/p.name):p.unlink()
        target.rmdir()
        raise


def correct(source: Path, expected_sha: str, page: int, terminal: int, out: Path) -> dict:
    require(re.fullmatch('[a-f0-9]{64}',expected_sha) and type(page) is int and 1<page<30
            and type(terminal) is int and 0<terminal<10000,'explicit exact input and expected physical/page association required')
    raw=limited_read(source,DOCX_LIMIT)
    require(digest(raw)==expected_sha,'input SHA-256 differs from explicit opt-in')
    out=out.absolute()
    require(out.name and not out.exists() and not out.is_symlink() and out.parent.is_dir()
            and out.parent.resolve()==out.parent,'new separate output directory under a real resolved parent required')
    protected_markers=('current-session.json','validation-budget.json')
    require(not any((parent/name).exists() or (parent/name).is_symlink()
                    for parent in out.parents for name in protected_markers),
            'output must remain outside existing formal run or campaign directories')
    require(supported_docx(raw)==terminal,'literal terminal reference does not match explicit opt-in')
    root,exe=select_office()
    with tempfile.TemporaryDirectory(prefix='.reference-leaf-causal-',dir=out.parent) as name:
        stage=Path(name)
        before,r0=render(raw,stage/'before',exe)
        with fitz.open(stream=before,filetype='pdf') as pdf:n=bounded_layout_pages(pdf)
        proposal=scope(raw,before,n,{})
        require(proposal is not None and proposal['physical_page']==page
                and proposal['terminal_reference']==terminal,'no unique exact leaf in the independently generated before PDF')
        candidate,changes=remove_one(raw,proposal)
        after,r1=render(candidate,stage/'after',exe)
        pages=verify_layout_change(before,after,page)
        require(office_inventory(root)==OFFICE_INVENTORY,'pinned renderer changed during the causal test')
        require(digest(limited_read(source,DOCX_LIMIT))==expected_sha,'original input changed')
        receipt={'status':'separate_candidate_after_local_causal_render','input_docx_sha256':expected_sha,
                 'output_docx_sha256':digest(candidate),'local_renderer_scope':'pinned_managed_libreoffice_same_invocation',
                 'renderer_inventory_sha256':OFFICE_INVENTORY[2], 'page_counts':pages,
                 'native_word_wps_verified':False,'submission_ready':False,'formal_review_imported':False,
                 'causal_proof':{**proposal,**changes,'same_layout_except_removed_page_and_footer_renumbering':True},
                 'renders':[r0,r1]}
        publish(stage,out,{'reference-leaf-candidate.docx':candidate,'before.pdf':before,'after.pdf':after,
                           'correction-receipt.json':(strict_json_dumps(receipt,indent=2)+'\n').encode()})
        return receipt


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--expect-input-sha256',required=True)
    p.add_argument('--expect-page',type=int,required=True)
    p.add_argument('--expect-terminal-reference',type=int,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    args=p.parse_args(argv)
    try:
        result=correct(args.input,args.expect_input_sha256,args.expect_page,args.expect_terminal_reference,args.output_dir)
    except (ValueError,OSError,TypeError,IndexError,etree.XMLSyntaxError,KeyError,BadZipFile,RuntimeError) as exc:
        print('HOLD: '+str(exc),file=sys.stderr)
        return 2
    print(strict_json_dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
