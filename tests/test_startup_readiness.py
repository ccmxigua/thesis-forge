from pathlib import Path
from hashlib import sha256
from types import SimpleNamespace
import json
import os
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from skill_bundle import build_bundle
from validation_budget import initialize_campaign, reserve_operation, record_operation
try:
    import startup_readiness as ready
except ModuleNotFoundError as exc:
    if exc.name != "startup_readiness": raise
    ready = None

class Fixture(SimpleNamespace):
    def request(self, **changes):
        data = vars(self).copy()
        data.update(changes)
        return ready.inspect_readiness(**data)


def sha(p):
    return sha256(p.read_bytes()).hexdigest()


def dump(path,value):
    path.write_text(json.dumps(value,sort_keys=True)+'\n')


def build_state(tmp_path, testcase):
    tmp_path.mkdir(); seed=tmp_path/'seed'; seed.mkdir(); (seed/'scripts').mkdir()
    (seed/'SKILL.md').write_text('startup fixture')
    (seed/'scripts'/'thesis_format.py').write_text('inert test-only file')
    dump(seed/'skill-package.json',{'schema_version':'thesis-skill-package/v1','name':'thesis-latex2docx',
      'version':'fixture','include':['SKILL.md','scripts/thesis_format.py'],'entrypoints':{}})
    package=tmp_path/'package.zip'
    from zipfile import ZipFile
    build_bundle(seed,package)
    root=tmp_path/'bundle'
    with ZipFile(package) as z:z.extractall(root)
    campaign=tmp_path/'existing-campaign'
    campaign_id=initialize_campaign(campaign)['campaign_id']
    ledger=campaign/'validation-budget.json'
    # Host environment in test is never treated as evidence that real pins match.
    # Use actual dependency discovery in the fixture, substituting only its injected result.
    original = ready.probe_capabilities
    testcase.addCleanup(setattr, ready, 'probe_capabilities', original)
    ready.probe_capabilities = lambda root, requested: {
        'status':'ready','capabilities':{'docx':{'status':'available','missing':[]}}}
    return Fixture(root=root,manifest_sha256=sha(root/'bundle-manifest.json'),
       campaign=campaign,campaign_id=campaign_id,ledger_sha256=sha(ledger))


def snapshot(path):
    return {str(x.relative_to(path)):x.read_bytes() for x in path.rglob('*') if x.is_file()}


def exercise(state,**kw):
    before=snapshot(state.campaign.parent)
    report=state.request(**kw)
    assert snapshot(state.campaign.parent)==before
    return report


def codes(report):
    return {x['code'] for x in report['blockers']}


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ready, 'The startup readiness gate is absent from the runtime.')
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.state = build_state(self.tmp_path/'fixture', self)

    def test_ready_only_is_not_permission_and_never_writes(self):
        state = self.state
        report=exercise(state)
        assert report['status']=='ready'
        assert report['can_execute'] is False
        assert report['checks']['campaign']['used_candidates']==0


    def test_missing_bundle_and_ledger_are_actionable_and_no_init(self):
        tmp_path = self.tmp_path
        result=ready.inspect_readiness(root=tmp_path/'missing-bundle',manifest_sha256='0'*64,
            campaign=tmp_path/'missing-campaign',campaign_id='c79efd81-5d2c-4e33-a319-48be100a77ca',
            ledger_sha256='0'*64)
        assert result['status']=='blocked'
        assert {'bundle_manifest_missing','campaign_ledger_missing'}<=codes(result)
        assert not (tmp_path/'missing-campaign').exists()
        assert all(x['next_step'] for x in result['blockers'])


    def test_modified_member_blocks_even_with_same_manifest_pin(self):
        state = self.state
        (state.root/'SKILL.md').write_text('altered')
        r=exercise(state)
        assert 'bundle_verification_failed' in codes(r)
        assert r['checks']['bundle']['mismatched']==['SKILL.md']


    def test_wrong_manifest_pin_is_distinct(self):
        state = self.state
        r=exercise(state,manifest_sha256='4'*64)
        assert 'bundle_manifest_sha_mismatch' in codes(r)


    def test_missing_exact_runtime_pins_returns_real_pin_names(self):
        state = self.state
        ready.probe_capabilities = lambda root, requested: {
          'status':'blocked','capabilities':{'docx':{'status':'unavailable','missing':['lxml==6.1.0']}}}
        r=exercise(state)
        assert 'runtime_pins_unready' in codes(r)
        assert r['checks']['runtime']['missing']==['lxml==6.1.0']


    def test_same_schema_other_ledger_is_not_a_replacement(self):
        state = self.state
        r=exercise(state,campaign_id='c79efd81-5d2c-4e33-a319-48be100a77ca')
        assert 'campaign_id_mismatch' in codes(r)
        assert 'campaign_sha_mismatch' in codes(exercise(state,ledger_sha256='8'*64))


    def test_v3_authorized_caps_not_reinterpreted_as_v1_or_reset(self):
        state = self.state
        ledger=state.campaign/'validation-budget.json'
        data=json.loads(ledger.read_text()); data['schema_version']='thesis-validation-budget/v3'
        data['limits']['candidate']=None;dump(ledger,data)
        r=exercise(state,ledger_sha256=sha(ledger))
        assert 'campaign_schema_unsupported' in codes(r)
        assert r['checks']['campaign']['schema_version']=='thesis-validation-budget/v3'


    def test_unsettled_operation_never_eligible_for_automatic_replay(self):
        state = self.state
        reserve_operation(state.campaign,'candidate-one','candidate','op-existing')
        record_operation(state.campaign,'op-existing','unknown',{'evidence_refs':['fixture transport disappearance']})
        r=exercise(state,ledger_sha256=sha(state.campaign/'validation-budget.json'),phase='generate',operation_id='op-existing')
        assert 'operation_already_recorded' in codes(r)
        assert r['checks']['campaign']['operation_outcome']=='unknown'


    def test_retained_budget_cap_is_not_guessed_away(self):
        state = self.state
        reserve_operation(state.campaign,'candidate-one','candidate','one')
        reserve_operation(state.campaign,'candidate-two','candidate','two')
        opts={'ledger_sha256':sha(state.campaign/'validation-budget.json')}
        assert exercise(state,**opts)['status']=='ready' # packet prep is distinct
        assert 'candidate_limit_reached' in codes(exercise(state,**opts,phase='generate',operation_id='new'))


    def test_resume_refuses_wrong_campaign_bound_to_checkpoint(self):
        state = self.state
        work=state.root.parent/'retained-run';work.mkdir()
        dump(work/'current-session.json',{'schema_version':'thesis-current-session/v1','run_id':'fixture-run','campaign_id':'wrong'})
        r=exercise(state,phase='resume',work_dir=work,run_id='fixture-run')
        assert 'session_campaign_mismatch' in codes(r)

    def test_generate_requires_official_checkpoint_validation(self):
        state = self.state
        work = self.tmp_path / 'work'
        work.mkdir()
        config = {'schema_version': 'thesis-current-session/v1', 'run_id': 'same-run',
                  'campaign_id': state.campaign_id, 'campaign': str(state.campaign)}
        checkpoint = work / 'current-session.json'
        dump(checkpoint, config)
        args = dict(phase='generate', work_dir=work, run_id='same-run', operation_id='new-operation')
        report = exercise(state, **args)
        self.assertIn('session_authoritative_validation_failed', codes(report))
        self.assertFalse(report['can_execute'])
        config['campaign'] = str(self.tmp_path / 'stale-campaign-location')
        dump(checkpoint, config)
        self.assertIn('session_campaign_path_mismatch', codes(exercise(state, **args)))

    def test_confirmed_refund_cannot_suppress_budget_without_proof(self):
        state = self.state
        ledger = state.campaign / 'validation-budget.json'
        reserve_operation(state.campaign, 'charged-one', 'candidate', 'bad-refund')
        raw = json.loads(ledger.read_text())
        raw['operations']['bad-refund'].update(outcome='not_executed', evidence=None)
        dump(ledger, raw)
        r = exercise(state, ledger_sha256=sha(ledger))
        self.assertIn('campaign_operation_evidence_invalid', codes(r))
        self.assertEqual(r['status'], 'blocked')

    def test_resume_rejects_three_field_stub_without_provenance(self):
        state = self.state
        work = self.tmp_path/'partial-work'; work.mkdir()
        dump(work/'current-session.json',{'schema_version':'thesis-current-session/v1',
             'run_id':'stub-run','campaign_id':state.campaign_id})
        report=exercise(state,phase='resume',work_dir=work,run_id='stub-run')
        self.assertIn('session_authoritative_validation_failed', codes(report))
        self.assertEqual(report['status'],'blocked')

    def test_report_code_changed_without_rebinding(self):
        from current_session import CONFIG_KEYS
        state = self.state
        work = self.tmp_path/'retained-work'; work.mkdir()
        values=dict.fromkeys(CONFIG_KEYS)
        values.update(schema_version='thesis-current-session/v1', run_id='retained',
                      campaign_id=state.campaign_id, campaign=str(state.campaign))
        dump(work/'current-session.json',values)
        original=ready._official_session
        self.addCleanup(setattr,ready,'_official_session',original)
        ready._official_session=lambda path, config, inspect: {
            'next_action':'blocked','blocked_reasons':['code_changed']} if inspect else {'next_action':'not_inspected'}
        r=exercise(state,phase='resume',work_dir=work,run_id='retained')
        self.assertIn('session_code_changed',codes(r))
        self.assertEqual(r['checks']['session']['code_binding'],'changed')

    def test_valid_refund_reuses_that_existing_ledger_without_spending(self):
        state=self.state
        reserve_operation(state.campaign,'candidate-used','candidate','observed-refund')
        record_operation(state.campaign,'observed-refund','not_executed',
            {'execution_confirmed':False,'evidence_refs':['test-only affirmative proof']})
        r=exercise(state, ledger_sha256=sha(state.campaign/'validation-budget.json'))
        self.assertEqual(r['status'],'ready')
        self.assertEqual(r['checks']['campaign']['used_candidates'],0)
        self.assertFalse(r['can_execute'])

    def test_incomplete_executable_tree_explains_missing_entrypoints_as_json(self):
        partial = self.tmp_path / 'partial'
        partial.mkdir()
        script = partial / 'startup_readiness.py'
        script.write_bytes(Path(ready.__file__).read_bytes())
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        result = subprocess.run([sys.executable, '-I', str(script),
            '--root', str(partial), '--manifest-sha256', '0'*64,
            '--campaign', str(partial/'campaign'), '--ledger-sha256', '0'*64,
            '--campaign-id', 'c79efd81-5d2c-4e33-a319-48be100a77ca'],
            env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        r = json.loads(result.stdout)
        self.assertIn('startup_checker_dependencies_missing', codes(r))
        self.assertIn('bundle_manifest_missing', codes(r))
        self.assertFalse((partial/'.budget.lock').exists())

if __name__ == '__main__': unittest.main()
