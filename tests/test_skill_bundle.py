from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def api():
    source = ROOT / "scripts/skill_bundle.py"
    assert source.is_file(), "Portable skill bundler is not implemented"
    spec = importlib.util.spec_from_file_location("skill_bundle_test_target", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    (root / "schema").mkdir()
    (root / "SKILL.md").write_text("---\nname: thesis-latex2docx\n---\n", encoding="utf-8")
    (root / "scripts/entry.py").write_text(
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "print((root / 'schema/input.json').read_text())\n", encoding="utf-8")
    (root / "schema/input.json").write_text('{"source":"fixture"}', encoding="utf-8")
    (root / "skill-package.json").write_text(json.dumps({
        "schema_version": "thesis-skill-package/v1", "name": "thesis-latex2docx",
        "version": "1.0.0", "include": ["SKILL.md", "scripts", "schema"],
        "entrypoints": {"prepare": "scripts/entry.py"},
    }), encoding="utf-8")
    return root


def test_relocated_bundle_does_not_depend_on_source_checkout(source, tmp_path):
    archive = tmp_path / "release.zip"
    api().build_bundle(source, archive)
    target = tmp_path / "different root"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    source.rename(tmp_path / "hidden checkout")
    report = api().verify_bundle(target)
    assert report["status"] == "pass"
    run = subprocess.run([sys.executable, str(target / "scripts/entry.py")],
                         cwd=tmp_path, capture_output=True, text=True, check=True)
    assert run.stdout.strip() == '{"source":"fixture"}'
    (target / "schema/input.json").unlink()
    report = api().verify_bundle(target)
    assert report["status"] == "blocked"
    assert "schema/input.json" in report["missing"]


def test_bundle_detects_changed_bytes_and_added_runtime_code(source, tmp_path):
    archive, target = tmp_path / "bundle.zip", tmp_path / "installed"
    api().build_bundle(source, archive)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    (target / "scripts/entry.py").write_text("print('tampered')")
    (target / "scripts/extra.py").write_text("print('extra')")
    report = api().verify_bundle(target)
    assert report["status"] == "blocked"
    assert "scripts/entry.py" in report["mismatched"]
    assert "scripts/extra.py" in report["unexpected"]


def test_bundle_is_reproducible_and_does_not_include_working_material(source, tmp_path):
    (source / ".env").write_text("SECRET=never ship")
    (source / "inputs").mkdir()
    (source / "inputs/private.docx").write_bytes(b"private test input")
    (source / "scripts/__pycache__").mkdir()
    (source / "scripts/__pycache__/entry.pyc").write_bytes(b"cache")
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    api().build_bundle(source, first)
    api().build_bundle(source, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as bundle:
        assert ".env" not in bundle.namelist()
        assert "inputs/private.docx" not in bundle.namelist()
        assert not any("__pycache__" in item for item in bundle.namelist())


@pytest.mark.parametrize("danger", ["../outside", "/tmp/outside", ".env", "inputs"])
def test_bundle_refuses_unsafe_or_private_include_policy(source, tmp_path, danger):
    policy_path = source / "skill-package.json"
    policy = json.loads(policy_path.read_text())
    policy["include"].append(danger)
    policy_path.write_text(json.dumps(policy))
    output = tmp_path / "existing.zip"
    output.write_bytes(b"preserve existing file")
    with pytest.raises(ValueError):
        api().build_bundle(source, output)
    assert output.read_bytes() == b"preserve existing file"


def test_bundle_refuses_symlink_resource(source, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("private outside data")
    (source / "schema/link.json").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        api().build_bundle(source, tmp_path / "bundle.zip")


def test_verifier_refuses_manifest_traversal_without_reading_outside(source, tmp_path):
    target = tmp_path / "installed"
    api().build_bundle(source, tmp_path / "bundle.zip")
    with zipfile.ZipFile(tmp_path / "bundle.zip") as bundle:
        bundle.extractall(target)
    manifest_path = target / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "../outside"
    manifest_path.write_text(json.dumps(manifest))
    assert api().verify_bundle(target)["status"] == "blocked"


def test_runtime_bundle_has_all_current_session_entry_resources(tmp_path):
    archive=tmp_path/'actual-runtime.zip'
    api().build_bundle(ROOT,archive)
    with zipfile.ZipFile(archive) as bundle:
        required={'scripts/current_session.py','scripts/session_review.py','scripts/native_acceptance.py',
                  'scripts/validation_budget.py','schema/native-acceptance-v1.schema.json',
                  'references/wps-native-acceptance.md'}
        assert required.issubset(bundle.namelist())


def test_relocated_tex_preparation_does_not_require_zip_executable_bits(tmp_path):
    import os
    from docx import Document
    archive=tmp_path/'runtime.zip';api().build_bundle(ROOT,archive)
    installed=tmp_path/'skill with spaces'
    with zipfile.ZipFile(archive) as bundle:bundle.extractall(installed)
    assert not os.access(installed/'convert.sh',os.X_OK)
    req=tmp_path/'requirements.docx';doc=Document();doc.add_paragraph('正文使用宋体小四号。');doc.save(req)
    source=tmp_path/'source.tex';source.write_text('\\documentclass{article}\n\\begin{document}\n\\section{Introduction}\nSynthetic plain text.\n\\end{document}\n')
    env=os.environ.copy();env['PYTHONPATH']=os.pathsep.join(p for p in env.get('PYTHONPATH','').split(os.pathsep) if p and Path(p).resolve()!=(ROOT/'scripts').resolve())
    env['PYTHON']='/nonexistent/unrelated-python'
    result=subprocess.run([sys.executable,str(installed/'scripts/thesis_format.py'),str(req),str(source),'--work-dir',str(tmp_path/'work')],cwd=tmp_path,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stderr+result.stdout[-3000:]
    assert (tmp_path/'work/review/requirements/llm-request-chunks.json').is_file()


@pytest.fixture
def platform_installed(source, tmp_path):
    (source/'agents').mkdir()
    (source/'agents/openai.yaml').write_bytes((ROOT/'agents/openai.yaml').read_bytes())
    policy_path=source/'skill-package.json';policy=json.loads(policy_path.read_text());policy['include'].append('agents');policy_path.write_text(json.dumps(policy))
    archive=tmp_path/'platform.zip';api().build_bundle(source,archive)
    installed=tmp_path/'platform-installed'
    with zipfile.ZipFile(archive) as bundle:bundle.extractall(installed)
    fixture=ROOT/'tests/fixtures/chatgpt_installation'
    (installed/'agents/openai.yaml').write_bytes((fixture/'openai.yaml').read_bytes())
    (installed/'assets').mkdir();(installed/'assets/icon.svg').write_bytes((fixture/'icon.svg').read_bytes())
    return installed


def test_platform_adapter_accepts_only_observed_metadata_pair(platform_installed):
    m=api()
    assert m.verify_bundle(platform_installed)['status']=='blocked'
    report=m.verify_bundle(platform_installed,installation_platform='chatgpt')
    assert report['status']=='pass'
    assert report['verification_mode']=='chatgpt_observed_ui_metadata/v1'
    assert report['byte_identical_to_bundle'] is False
    assert report['payload_integrity']=='exact'
    assert {change['path'] for change in report['accepted_platform_changes']}=={'agents/openai.yaml','assets/icon.svg'}
    assert report['platform_origin_authenticated'] is False


@pytest.mark.parametrize('change',['code','extra_code','extra_asset','extra_agent_file','yaml_field','icon_bytes','icon_missing','yaml_original','source_manifest','icon_symlink'])
def test_platform_adapter_never_ignores_other_changes(platform_installed,tmp_path,change):
    p=platform_installed
    if change=='code':(p/'scripts/entry.py').write_text('print("changed")')
    elif change=='extra_code':(p/'scripts/unknown.py').write_text('print("added")')
    elif change=='extra_asset':(p/'assets/unknown.svg').write_text('<svg/>')
    elif change=='extra_agent_file':(p/'agents/extra.yaml').write_text('policy: changed')
    elif change=='yaml_field':
        path=p/'agents/openai.yaml';path.write_text(path.read_text()+'unknown: permitted\n')
    elif change=='icon_bytes':(p/'assets/icon.svg').write_text('<svg/>')
    elif change=='icon_missing':(p/'assets/icon.svg').unlink()
    elif change=='yaml_original':(p/'agents/openai.yaml').write_bytes((ROOT/'agents/openai.yaml').read_bytes())
    elif change=='source_manifest':
        path=p/'bundle-manifest.json';manifest=json.loads(path.read_text())
        next(item for item in manifest['files'] if item['path']=='agents/openai.yaml')['sha256']='0'*64
        path.write_text(json.dumps(manifest))
    else:
        outside=tmp_path/'outside.svg';outside.write_bytes((p/'assets/icon.svg').read_bytes());(p/'assets/icon.svg').unlink();(p/'assets/icon.svg').symlink_to(outside)
    assert api().verify_bundle(p,installation_platform='chatgpt')['status']=='blocked'


def test_platform_adapter_rejects_unknown_platform(platform_installed):
    assert api().verify_bundle(platform_installed,installation_platform='unknown')['status']=='blocked'
