#!/usr/bin/env python3
"""Inspect a retained Thesis startup checkpoint without creating or spending it."""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import json
import re
import sys
from uuid import UUID

# Startup inspection must also work on—and not write into—a read-only skill.
sys.dont_write_bytecode = True
if __name__ == '__main__':
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
try:
    from semantic_contract import strict_json_loads
    from skill_bundle import verify_bundle
    from skill_runtime import probe_capabilities
    from validation_budget import read_budget
except ImportError as exc:
    dependency_import_error = exc.name or 'checker companion module'
else:
    dependency_import_error = None


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _is_sha(value: str | None) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OSError('not a regular checkpoint file')
    return path.read_bytes()



def _bad_operation_evidence(operations: dict) -> list[str]:
    """Match authoritative write invariants without calling its write path."""
    invalid = []
    for op_id, op in operations.items():
        evidence = op['evidence']
        outcome = op['outcome']
        candidate_id = op['candidate_id']
        valid = (isinstance(op_id, str) and bool(op_id.strip()) and len(op_id) < 256
                 and isinstance(candidate_id, str) and bool(candidate_id.strip())
                 and len(candidate_id) < 256 and isinstance(evidence, dict))
        if valid and outcome != 'reserved':
            refs = evidence.get('evidence_refs')
            valid = (isinstance(refs, list) and bool(refs)
                     and all(isinstance(ref, str) and bool(ref.strip()) for ref in refs))
            if outcome == 'not_executed':
                valid = valid and evidence.get('execution_confirmed') is False
        if not valid:
            invalid.append(op_id)
    return invalid


def _official_session(work: Path, config: dict, *, inspect: bool) -> dict:
    from current_session import CONFIG_KEYS, inspect_session
    # The baseline contract includes code fingerprint, source identities,
    # packet hashes and the whole authoritative run manifest. A marker or
    # abbreviated reconstruction here cannot stand in for that provenance.
    if set(config) != CONFIG_KEYS:
        return {'next_action': 'blocked'}
    if not inspect:
        return {'next_action': 'not_inspected'}
    return inspect_session(work, include_checks=False)


def inspect_readiness(*, root: Path, manifest_sha256: str, campaign: Path,
                      campaign_id: str, ledger_sha256: str, phase: str = 'prepare',
                      installation_platform: str | None = None,
                      work_dir: Path | None = None, run_id: str | None = None,
                      operation_id: str | None = None) -> dict:
    """Return actions and a read-only observation, never authority to execute."""
    root, campaign = Path(root), Path(campaign)
    result = {'schema_version': 'thesis-startup-readiness/v1', 'status': 'blocked',
              'phase': phase, 'can_execute': False, 'checks': {}, 'blockers': []}
    checks = result['checks']

    def block(code: str, next_step: str) -> None:
        result['blockers'].append({'code': code, 'next_step': next_step})

    if phase not in {'prepare', 'resume', 'generate'}:
        block('invalid_phase', 'Choose prepare, resume, or generate.')
        return result

    if dependency_import_error:
        checks['startup_checker'] = {'missing_module': dependency_import_error}
        block('startup_checker_dependencies_missing',
              'Recover all companion modules from the same complete skill package; do not mix different installations.')

    mfile = root / 'bundle-manifest.json'
    trusted_pins = _is_sha(manifest_sha256) and _is_sha(ledger_sha256) and _is_uuid(campaign_id)
    if not trusted_pins:
        block('checkpoint_pin_invalid',
              'Supply the trusted bundle SHA, current campaign UUID and latest ledger SHA; '
              'do not derive the expected values from files you are attempting to verify.')
    try:
        mbytes = _regular_bytes(mfile)
    except OSError:
        block('bundle_manifest_missing',
              'Recover the selected complete verified skill bundle and its manifest. '
              'An engineering checkout or partial source recovery is not an installed runtime.')
    else:
        seen = _digest(mbytes)
        checks['bundle'] = {'manifest_sha256': seen}
        if seen != manifest_sha256:
            block('bundle_manifest_sha_mismatch',
                  'Recover the exact package matching the trusted manifest pin. Do not rebase the pin to this file.')
        elif not dependency_import_error:
            integrity = verify_bundle(root, installation_platform=installation_platform)
            checks['bundle'] = integrity
            try:
                unchanged = _regular_bytes(mfile) == mbytes
            except OSError:
                unchanged = False
            if not unchanged:
                block('bundle_manifest_changed_during_check',
                      'Obtain a stable immutable copy of the selected package and recheck.')
            elif integrity['status'] != 'pass' or integrity.get('name') != 'thesis-latex2docx':
                block('bundle_verification_failed',
                      'Recover missing or different payload files from the approved complete package. '
                      'Do not generate a new manifest to hide drift.')

    ledger = campaign / 'validation-budget.json'
    budget = None
    try:
        lbytes = _regular_bytes(ledger)
    except OSError:
        block('campaign_ledger_missing',
              'Restore the existing campaign ledger and its latest verified operation records. '
              'Do not initialize or substitute a campaign for this checkpoint.')
    else:
        seen = _digest(lbytes)
        checks['campaign'] = {'ledger_sha256': seen}
        if seen != ledger_sha256:
            block('campaign_sha_mismatch',
                  'Reconcile against the latest authoritative ledger and its receipts. Do not accept a stale or alternate ledger.')
        elif not dependency_import_error:
            try:
                raw = strict_json_loads(lbytes.decode('utf-8'))
                if not isinstance(raw, dict):
                    raise ValueError('campaign must be a JSON object')
            except (UnicodeError, ValueError):
                block('campaign_invalid', 'Recover intact ledger bytes from the current authoritative checkpoint.')
            else:
                checks['campaign'].update(schema_version=raw.get('schema_version'),
                                          campaign_id=raw.get('campaign_id'))
                if raw.get('campaign_id') != campaign_id:
                    block('campaign_id_mismatch',
                          'Resume the campaign named in the authorized checkpoint, not a replacement.')
                else:
                    # The actual installed validator—not this helper—owns the
                    # compatibility decision and any validation budget semantics.
                    try:
                        budget = read_budget(campaign)
                    except (ValueError, TypeError, KeyError, OSError, UnicodeError):
                        if raw.get('schema_version') != 'thesis-validation-budget/v1':
                            block('campaign_schema_unsupported',
                                  'Recover the reviewed runtime that supports this exact ledger version; '
                                  'do not convert, reset or drop previously charged operations.')
                        else:
                            block('campaign_invalid',
                                  'Run the authoritative checkpoint verifier and restore this campaign’s intact records.')
                    try:
                        stable = _regular_bytes(ledger) == lbytes
                    except OSError:
                        stable = False
                    if not stable:
                        block('campaign_changed_during_check',
                              'Reconcile the concurrent change and rerun with fresh authoritative checkpoint pins.')
                        budget = None
                    if budget is not None:
                        invalid = _bad_operation_evidence(budget['operations'])
                        if invalid:
                            # Keep the authority and its limits intact. Do not
                            # publish its discounted usage as verified when a
                            # supposedly refunded operation has no receipt.
                            checks['campaign'].update(
                                candidate_limit=budget['limits']['candidate'],
                                accounting_status='not_verified',
                                invalid_operation_evidence_count=len(invalid))
                            block('campaign_operation_evidence_invalid',
                                  'Recover authoritative outcome receipts. A not-executed outcome requires '
                                  'nonempty evidence references and affirmative proof of non-execution. '
                                  'Do not alter or refund any operation to pass this check.')
                            budget = None

    # The CLI performs no document extraction; runtime probing only reads pins
    # and executable versions. A missing optional render tool is not a DOCX blocker.
    if not dependency_import_error:
        runtime = probe_capabilities(root, ['docx'])
        checks['runtime'] = runtime['capabilities']['docx']
        if checks['runtime']['status'] != 'available':
            block('runtime_pins_unready',
                  'Use an isolated Python environment prepared with the exact runtime requirements; '
                  'do not continue with approximate package versions.')

    if budget is not None:
        checks['campaign'].update(used_candidates=budget['used']['candidate'],
                                  candidate_limit=budget['limits']['candidate'])
        if phase == 'generate':
            if not operation_id:
                block('operation_id_required', 'Supply the stable operation ID before checking candidate readiness.')
            elif operation_id in budget['operations']:
                checks['campaign']['operation_outcome'] = budget['operations'][operation_id]['outcome']
                block('operation_already_recorded',
                      'Inspect the retained operation and its evidence; do not replay an existing or uncertain operation.')
            elif (budget['limits']['candidate'] is not None and
                  budget['used']['candidate'] >= budget['limits']['candidate']):
                block('candidate_limit_reached',
                      'Keep the existing charged attempts. Reconcile the existing campaign under its authorized policy.')
    if phase in {'resume', 'generate'} and not dependency_import_error:
        if work_dir is None or not run_id:
            block('session_checkpoint_missing',
                  'Supply the retained run directory and pinned run ID; do not start a replacement automatically.')
        else:
            try:
                config = strict_json_loads(_regular_bytes(Path(work_dir) / 'current-session.json').decode('utf-8'))
                if not isinstance(config, dict) or config.get('schema_version') != 'thesis-current-session/v1':
                    raise ValueError('unsupported checkpoint')
            except (UnicodeError, ValueError, OSError):
                block('session_checkpoint_invalid',
                      'Restore the existing bound session; do not use old semantic responses in a new run.')
            else:
                if config.get('run_id') != run_id:
                    block('session_run_mismatch', 'Locate the exact retained run instead of reusing a different checkpoint.')
                if config.get('campaign_id') != campaign_id:
                    block('session_campaign_mismatch', 'Locate the session bound to the expected active campaign.')
                config_campaign = config.get('campaign')
                if (not isinstance(config_campaign, str) or not Path(config_campaign).is_absolute()
                        or Path(config_campaign).resolve() != campaign.resolve()):
                    block('session_campaign_path_mismatch',
                          'Restore this session’s authoritative campaign path or use an independently approved rebinding '
                          'workflow; do not silently reroute the retained session.')
                # Never let an invalid location redirect the official inspector
                # to another campaign. Check the full config independently, but
                # only inspect a run when upstream verification remains green.
                allowed = not result['blockers']
                code_changed = False
                try:
                    guard = _official_session(Path(work_dir), config, inspect=allowed)
                    step = guard.get('next_action', 'blocked')
                    code_changed = 'code_changed' in guard.get('blocked_reasons', [])
                    if step not in {'blocked', 'not_inspected'}:
                        binding = guard.get('binding')
                        if (step not in {'semantic_response', 'merge_response', 'generate_candidate', 'independent_review'}
                                or not isinstance(binding, dict) or binding.get('run_id') != run_id):
                            step = 'blocked'
                    if allowed and _regular_bytes(ledger) != lbytes:
                        block('campaign_changed_during_session_check',
                              'Reconcile the concurrent change against the current authoritative ledger before continuing.')
                        step = 'blocked'
                except (ValueError, OSError, TypeError, AttributeError, ImportError, KeyError, UnicodeError, RuntimeError):
                    step = 'blocked'
                checks['session'] = {'official_next_action': step}
                if code_changed:
                    checks['session']['code_binding'] = 'changed'
                    block('session_code_changed',
                          'Keep this checkpoint immutable and recover the exact previously bound runtime. '
                          'Use a fresh extraction only under the same approved campaign after explicit replan; '
                          'do not relabel or replay its old packets.')
                elif step == 'blocked' or (allowed and step == 'not_inspected'):
                    block('session_authoritative_validation_failed',
                          'Restore the complete run manifest, source and code bindings and exact review packets. '
                          'A partial session marker is not a restorable checkpoint.')
                elif phase == 'generate' and allowed and step != 'generate_candidate':
                    block('session_not_ready_to_generate',
                          'Follow the authoritative next action; do not generate before the bound review merge.')
    if not result['blockers']:
        result['status'] = 'ready'
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--installation-platform', choices=['chatgpt'])
    p.add_argument('--campaign', type=Path, required=True)
    p.add_argument('--campaign-id', required=True)
    p.add_argument('--ledger-sha256', required=True)
    p.add_argument('--phase', choices=['prepare', 'resume', 'generate'], default='prepare')
    p.add_argument('--work-dir', type=Path)
    p.add_argument('--run-id')
    p.add_argument('--operation-id')
    report = inspect_readiness(**vars(p.parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['status'] == 'ready' else 2


if __name__ == '__main__':
    raise SystemExit(main())
