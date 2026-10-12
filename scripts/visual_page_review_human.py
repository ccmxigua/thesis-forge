#!/usr/bin/env python3
"""Serve an offline, loopback-only visual review session for a prepared v2 run.

The browser session records which exact page images were served and loaded,
then stores the reviewer's submitted observations. It records a self-declared
reviewer label only; it does not authenticate identity or claim proof of human
attention, model vision, or submission readiness.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import visual_page_review as visual
from semantic_contract import strict_json_dumps, strict_json_loads, strict_json_read
from visual_review_protocol import renderer_status_projection, review_status, validate_checklist

ZERO_HASH = "0" * 64


def canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(strict_json_dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _append_event(session: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {
        "sequence": len(session["events"]) + 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "payload": payload,
        "previous_event_sha256": session["last_event_sha256"],
    }
    event["event_sha256"] = canonical_hash(event)
    session["last_event_sha256"] = event["event_sha256"]
    session["events"].append(event)
    with session["events_path"].open("a", encoding="utf-8") as handle:
        handle.write(strict_json_dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
    return event


def _page_images(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["image_id"]: row for row in page["image_map"]}


def _html_app() -> str:
    return r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual page review</title><style>
body{font:16px system-ui,sans-serif;margin:0;background:#f3f4f6;color:#111827}header{background:#111827;color:white;padding:14px 20px;position:sticky;top:0;z-index:2}main{max-width:1200px;margin:20px auto;padding:0 16px}.card{background:white;border-radius:10px;padding:18px;margin:14px 0;box-shadow:0 1px 5px #0002}.images{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.image-card{border:1px solid #ddd;padding:8px;overflow:auto}.image-card img{display:block;max-width:100%;height:auto}.check{display:grid;grid-template-columns:240px 150px 1fr;gap:10px;margin:12px 0;align-items:start}.check textarea{min-height:52px}.check input,.check select,.check textarea{width:100%;box-sizing:border-box}button{padding:10px 16px;margin:6px;cursor:pointer}textarea{font:14px ui-monospace,monospace}#status{font-weight:600;color:#075985}.warn{color:#9a3412}code{overflow-wrap:anywhere}
</style><header><strong>Visual page review</strong> — offline local session, exact prepared PDF images</header><main>
<div class="card"><div id="status">Loading session…</div><p class="warn">Reviewer label is self-declared and not identity-verified. Image delivery/load events do not prove that a person attended to or understood each image. Submission readiness remains false.</p><div id="pageinfo"></div></div>
<div class="card"><h2>Full page and evidence crops</h2><p>Wait for every image to load, then inspect the full page and each crop before recording observations.</p><div id="images" class="images"></div></div>
<div class="card"><h2>Page checklist</h2><div id="checklist"></div><label>Concise page summary<br><textarea id="summary" rows="3" style="width:100%"></textarea></label><label>Uncertainty notes, one per line<br><textarea id="uncertainty" rows="2" style="width:100%"></textarea></label>
<label>Issue records as JSON array; leave [] when no finding. Each issue must cite exact requirement/clause ids and image ids when applicable.<br><textarea id="issues" rows="8" style="width:100%">[]</textarea></label><button id="submit" disabled>Record this page</button></div>
<div class="card"><p id="receipt"></p><button id="previous">Previous page</button><button id="next">Next page</button></div></main>
<script>
const items=["headers_footers_and_parity","visible_folio_and_page_mapping","body_readability_and_glyphs","whitespace_and_page_breaks","figures_and_captions","tables_and_splits","equations_and_symbols","cross_page_continuity","source_rule_and_section_mapping","contents_entries_and_targets"];
let session=null,current=1,loaded=new Set(),completed=new Set();
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined});const x=await r.json();if(!r.ok)throw Error(x.error||r.statusText);return x;}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
async function loadPage(n){current=n;loaded=new Set();document.getElementById("submit").disabled=true;const p=session.pages[n-1];document.getElementById("pageinfo").innerHTML=`Physical PDF page <b>${n}</b> of ${session.pages.length}; page map: <code>${esc(JSON.stringify(p.page_map))}</code>`;document.getElementById("images").innerHTML="";
for(const im of p.images){const card=document.createElement("div");card.className="image-card";card.innerHTML=`<b>${esc(im.image_id)}</b> · ${esc(im.kind)} · ${im.width_px}×${im.height_px}<br><code>${im.sha256}</code><img alt="${esc(im.image_id)}" src="/image?page=${n}&id=${encodeURIComponent(im.image_id)}">`;const img=card.querySelector("img");img.onload=async()=>{try{await api("/image-loaded",{page_number:n,image_id:im.image_id,width_px:img.naturalWidth,height_px:img.naturalHeight});loaded.add(im.image_id);if(loaded.size===p.images.length)document.getElementById("submit").disabled=false;}catch(e){document.getElementById("status").textContent=e.message;}};document.getElementById("images").appendChild(card);}
document.getElementById("checklist").innerHTML=items.map(id=>`<div class="check"><b>${esc(id)}</b><select data-state="${id}"><option value="">Select…</option><option>pass</option><option>issue</option><option>not_applicable</option><option>unable_to_assess</option></select><div><textarea data-observation="${id}" placeholder="Visible observation"></textarea><textarea data-reason="${id}" placeholder="Reason for N/A or unable"></textarea></div></div>`).join("");document.getElementById("summary").value="";document.getElementById("uncertainty").value="";document.getElementById("issues").value="[]";document.getElementById("receipt").textContent=completed.has(n)?"Page already recorded for this session.":"";document.getElementById("status").textContent=`Loaded ${loaded.size}/${p.images.length} exact images; inspect each before submitting.`;
}
document.getElementById("submit").onclick=async()=>{try{const p=session.pages[current-1];if(loaded.size!==p.images.length)throw Error("Wait until every full-page/crop image reports loaded.");const checklist=items.map(item_id=>({item_id,status:document.querySelector(`[data-state="${item_id}"]`).value,observation:document.querySelector(`[data-observation="${item_id}"]`).value,reason:document.querySelector(`[data-reason="${item_id}"]`).value||null,evidence_image_ids:[`p${String(current).padStart(4,"0")}-full`]}));const uncertainty_notes=document.getElementById("uncertainty").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);const issues=JSON.parse(document.getElementById("issues").value);const visual_summary=document.getElementById("summary").value;const result=await api("/submit-page",{page_number:current,checklist,issues,uncertainty_notes,visual_summary,loaded_image_ids:[...loaded].sort()});completed.add(current);document.getElementById("receipt").textContent=JSON.stringify(result);document.getElementById("status").textContent=result.complete?"All pages recorded. The result is saved beside the prepared run.":`Recorded page ${current}; continue with every remaining physical page.`;}catch(e){document.getElementById("status").textContent=e.message;}};
document.getElementById("previous").onclick=()=>loadPage(Math.max(1,current-1));document.getElementById("next").onclick=()=>loadPage(Math.min(session.pages.length,current+1));
(async()=>{try{session=await api("/session");await loadPage(1);}catch(e){document.getElementById("status").textContent=e.message;}})();
</script></html>'''


def verify_human_receipt(receipt_path: Path, *, report_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify event hash chain, source binding, exact images and per-page payloads."""
    receipt_path = receipt_path.expanduser().resolve()
    receipt = visual._read_object(receipt_path, label="human visual review receipt")
    blockers: list[str] = []
    if receipt.get("schema_version") != "1.0" or receipt.get("protocol") != "visual_human_browser_session_v1":
        blockers.append("human_visual_receipt_protocol_invalid")
    if receipt.get("reviewer_identity_status") != "self_declared_not_verified":
        blockers.append("human_visual_reviewer_identity_claim_must_remain_unverified")
    base_record = receipt.get("base_manifest") if isinstance(receipt.get("base_manifest"), dict) else {}
    base_path = Path(str(base_record.get("path", ""))).expanduser().resolve()
    if not base_path.is_file() or visual.file_record(base_path) != base_record:
        blockers.append("human_visual_base_manifest_binding_invalid")
    events_record = receipt.get("events")
    if not isinstance(events_record, dict):
        blockers.append("human_visual_event_log_missing")
        events = []
    else:
        event_path = Path(str(events_record.get("path", ""))).resolve()
        if not event_path.is_file() or visual.file_record(event_path) != events_record:
            blockers.append("human_visual_event_log_hash_mismatch")
            events = []
        else:
            try:
                events = [strict_json_loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, ValueError):
                blockers.append("human_visual_event_log_invalid")
                events = []
    previous = ZERO_HASH
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("sequence") != index or event.get("previous_event_sha256") != previous:
            blockers.append("human_visual_event_chain_sequence_invalid")
            break
        copied = dict(event)
        digest = copied.pop("event_sha256", None)
        if digest != canonical_hash(copied):
            blockers.append("human_visual_event_chain_digest_invalid")
            break
        previous = digest
    if receipt.get("final_event_sha256") != previous or receipt.get("event_count") != len(events):
        blockers.append("human_visual_event_chain_terminal_mismatch")
    if base_path.is_file() and receipt.get("base_manifest_sha256") != visual.sha256_file(base_path):
        blockers.append("human_visual_base_manifest_sha256_mismatch")
    if base_record.get("sha256") != receipt.get("base_manifest_sha256"):
        blockers.append("human_visual_base_manifest_record_mismatch")
    if receipt.get("renderer_scope") != manifest.get("renderer_scope") or receipt.get("pdf_sha256") != manifest.get("pdf", {}).get("sha256"):
        blockers.append("human_visual_renderer_or_pdf_binding_mismatch")
    if receipt.get("run_id") != manifest.get("run_id"):
        blockers.append("human_visual_run_identity_mismatch")
    if base_path != (report_path.parent / "visual-review-manifest.json").resolve():
        blockers.append("human_visual_base_report_path_mismatch")
    expected_pages = manifest.get("pages", [])
    submitted_events = [event.get("payload", {}) for event in events if event.get("event_type") == "page_submitted"]
    if [row.get("page_number") for row in submitted_events] != list(range(1, len(expected_pages) + 1)):
        blockers.append("human_visual_page_submission_coverage_invalid")
    if receipt.get("pages") != submitted_events:
        blockers.append("human_visual_page_projection_mismatch")
    for number, (saved_page, submitted) in enumerate(zip(expected_pages, submitted_events), start=1):
        expected_ids = [row["image_id"] for row in saved_page.get("image_map", [])]
        if submitted.get("image_ids_loaded") != expected_ids:
            blockers.append(f"human_visual_loaded_images_mismatch:{number}")
        if submitted.get("image_hashes") != [row["image"]["sha256"] for row in saved_page.get("image_map", [])]:
            blockers.append(f"human_visual_image_hash_projection_mismatch:{number}")
        served_events = [event.get("payload", {}) for event in events
                         if event.get("event_type") == "image_served"
                         and event.get("payload", {}).get("page_number") == number]
        loaded_events = [event.get("payload", {}) for event in events
                         if event.get("event_type") == "image_loaded"
                         and event.get("payload", {}).get("page_number") == number]
        served_ids = {row.get("image_id") for row in served_events}
        loaded_ids = {row.get("image_id") for row in loaded_events}
        if served_ids != set(expected_ids) or loaded_ids != set(expected_ids):
            blockers.append(f"human_visual_viewer_image_events_incomplete:{number}")
        expected_hashes = {row["image_id"]: row["image"]["sha256"] for row in saved_page.get("image_map", [])}
        if any(row.get("sha256") != expected_hashes.get(row.get("image_id")) for row in served_events + loaded_events):
            blockers.append(f"human_visual_image_event_hash_mismatch:{number}")
        expected_dimensions = {row["image_id"]: (row["width_px"], row["height_px"])
                               for row in saved_page.get("image_map", [])}
        if any((row.get("width_px"), row.get("height_px")) != expected_dimensions.get(row.get("image_id"))
               for row in loaded_events):
            blockers.append(f"human_visual_image_event_dimensions_mismatch:{number}")
        try:
            validate_checklist(submitted.get("checklist"), allowed_image_ids=set(expected_ids))
        except ValueError:
            blockers.append(f"human_visual_checklist_invalid:{number}")
    return {"valid": not blockers, "status": "passed" if not blockers else "blocked", "blockers": sorted(set(blockers)), "receipt_sha256": visual.sha256_file(receipt_path)}


class HumanVisualReviewSession:
    def __init__(self, report_path: Path, *, reviewer_claim: str, output_dir: Path):
        self.report_path = report_path.expanduser().resolve()
        self.base = visual._read_object(self.report_path, label="prepared visual review")
        if self.base.get("protocol") != visual.PROTOCOL or self.base.get("status") != "prepared_not_reviewed":
            raise ValueError("human review requires a fresh prepared v2 report with no prior review attempt")
        if self.base.get("model_request_attempted") is True:
            raise ValueError("human browser session cannot be added after a model request attempt")
        self.work_dir = self.report_path.parent
        visual._validate_prepared_inputs(self.base, self.work_dir)
        if not reviewer_claim.strip():
            raise ValueError("reviewer label must be explicitly provided")
        self.reviewer_claim = reviewer_claim.strip()
        self.session_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.output_dir = output_dir.expanduser().resolve()
        if self.work_dir not in self.output_dir.parents:
            raise ValueError("human session evidence must be written inside the prepared run directory")
        if self.output_dir.exists():
            raise ValueError(f"human review output directory exists; refusing reuse: {self.output_dir}")
        self.output_dir.mkdir(parents=True)
        self.events_path = self.output_dir / "events.jsonl"
        self.events_path.write_text("", encoding="utf-8")
        self.session = {
            "events": [], "last_event_sha256": ZERO_HASH, "events_path": self.events_path,
            "served": {}, "loaded": {}, "submissions": {},
        }
        self.token_hash = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        self._append("session_created", {"session_id": self.session_id, "run_id": self.base["run_id"],
                                          "reviewer_claim": self.reviewer_claim,
                                          "reviewer_identity_status": "self_declared_not_verified"})
        self._lock = threading.RLock()

    def _append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _append_event(self.session, event_type, payload)

    def _session_payload(self) -> dict[str, Any]:
        rows = []
        for page in self.base["pages"]:
            rows.append({
                "page_number": page["page_number"], "page_map": page["page_map"],
                "images": [{
                    "image_id": row["image_id"], "kind": row["kind"],
                    "sha256": row["image"]["sha256"], "width_px": row["width_px"],
                    "height_px": row["height_px"],
                } for row in page["image_map"]],
            })
        return {"session_id": self.session_id, "run_id": self.base["run_id"], "pages": rows}

    def _finalize(self) -> dict[str, Any]:
        page_payloads = [self.session["submissions"][n] for n in range(1, self.base["page_count"] + 1)]
        receipt_path = self.output_dir / "human-review-receipt.json"
        receipt = {
            "schema_version": "1.0", "protocol": "visual_human_browser_session_v1",
            "session_id": self.session_id, "run_id": self.base["run_id"],
            "reviewer_claim": self.reviewer_claim,
            "reviewer_identity_status": "self_declared_not_verified",
            "base_manifest": visual.file_record(self.report_path),
            "base_manifest_sha256": visual.sha256_file(self.report_path),
            "renderer_scope": self.base["renderer_scope"],
            "pdf_sha256": self.base["pdf"]["sha256"],
            "pages": page_payloads,
            "events": visual.file_record(self.events_path),
            "event_count": len(self.session["events"]),
            "final_event_sha256": self.session["last_event_sha256"],
            "submission_ready": False,
        }
        visual._write_json(receipt_path, receipt)
        receipt_binding = visual.file_record(receipt_path)
        output = copy.deepcopy(self.base)
        output_path = self.work_dir / "visual-review-human-manifest.json"
        output.update({
            "status": "human_review_complete",
            "review_method": "human_browser_session",
            "human_review_receipt": receipt_binding,
            "model_request_attempted": False,
            "model_request_made": False,
            "model_request_outcome": "not_started_human_browser_session",
            "completed_model_turn_count": 0,
            "completed_model_response_count": 0,
            "unreviewed_page_numbers": [],
            "coverage_complete": True,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        all_pages = []
        for base_page, submitted in zip(output["pages"], page_payloads):
            checklist = submitted["checklist"]
            raw_issues = submitted["issues"]
            issues = []
            by_id = _page_images(base_page)
            for index, issue in enumerate(raw_issues, start=1):
                digest = canonical_hash(issue)
                issues.append({
                    **issue,
                    "finding_id": f"VRF-{output['run_id'][:8]}-{base_page['page_number']:04d}-{index:02d}-{digest[:10]}",
                    "status": "open",
                    "evidence_images": [{"image_id": image_id, "sha256": by_id[image_id]["image"]["sha256"]}
                                        for image_id in issue["evidence_image_ids"]],
                })
            base_page.update({
                "status": "completed",
                "review_method": "human_browser_session",
                "page_status": submitted["review_state"],
                "visual_summary": submitted["visual_summary"],
                "human_observation": {
                    "schema_version": "2.0", "page_number": submitted["page_number"],
                    "review_state": submitted["review_state"],
                    "visual_summary": submitted["visual_summary"],
                    "checklist": checklist, "issues": raw_issues,
                    "uncertainty_notes": submitted["uncertainty_notes"],
                },
                "checklist": checklist,
                "uncertainty_notes": submitted["uncertainty_notes"],
                "issues": issues,
                "unresolved_checklist_items": [item["item_id"] for item in checklist
                                               if item["status"] in {"issue", "unable_to_assess"}],
                "request_image_receipt": {
                    "status": "human_session_confirmed_images",
                    "session_id": self.session_id,
                    "reviewer_claim": self.reviewer_claim,
                    "reviewer_identity_status": "self_declared_not_verified",
                    "loaded_image_ids": submitted["image_ids_loaded"],
                    "meaning": "The loopback browser served and reported loading these exact images; this does not authenticate identity or prove attention.",
                },
            })
            all_pages.append(base_page)
        status, gate, blockers = review_status(
            page_count=output["page_count"], pages=all_pages, scope=output["renderer_scope"],
        )
        output["pages"] = all_pages
        output["visual_review_status"] = status
        output["review_gate_status"] = gate
        output["renderer_scoped_status"] = renderer_status_projection(output["renderer_scope"], status)
        output["visual_review_blockers"] = blockers
        output["status"] = "passed" if status == "passed" else status
        visual._write_issue_ledger(self.work_dir, output)
        from format_spec_validation import load_and_validate
        schema_errors = load_and_validate(output, visual.RECEIPT_SCHEMA)
        if schema_errors:
            raise ValueError("human visual report failed receipt schema: " + "; ".join(schema_errors[:12]))
        visual._write_json(output_path, output)
        return {"complete": True, "report": str(output_path), "receipt": str(receipt_path), "status": status,
                "submission_ready": False}

    def handler(self):
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "ThesisForgeVisualReview/1"

            def _authorized(self) -> bool:
                cookie = self.headers.get("Cookie", "")
                return f"vr_token={owner.token}" in cookie

            def _json(self, code: int, value: dict[str, Any]) -> None:
                raw = strict_json_dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/" and not self._authorized():
                    token = parse_qs(parsed.query).get("t", [""])[0]
                    if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), owner.token_hash):
                        self._json(403, {"error": "invalid or missing local session token"})
                        return
                    with owner._lock:
                        owner._append("browser_session_opened", {"session_id": owner.session_id,
                                                                  "reviewer_claim": owner.reviewer_claim})
                    self.send_response(302)
                    self.send_header("Set-Cookie", f"vr_token={owner.token}; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                if not self._authorized():
                    self._json(403, {"error": "local review session authorization required"})
                    return
                if parsed.path == "/":
                    raw = _html_app().encode("utf-8")
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store")
                    self.end_headers(); self.wfile.write(raw); return
                if parsed.path == "/session":
                    self._json(200, owner._session_payload()); return
                if parsed.path == "/image":
                    query = parse_qs(parsed.query)
                    try:
                        number = int(query.get("page", [""])[0]); image_id = query.get("id", [""])[0]
                        page = owner.base["pages"][number - 1]
                        image_row = _page_images(page)[image_id]
                    except (ValueError, IndexError, KeyError):
                        self._json(404, {"error": "image id or page is not in the prepared manifest"}); return
                    image_path = Path(image_row["image"]["path"]).resolve()
                    if not image_path.is_file() or visual.file_record(image_path) != image_row["image"]:
                        self._json(409, {"error": "prepared image changed after review preparation"}); return
                    data = image_path.read_bytes()
                    with owner._lock:
                        owner.session["served"].setdefault(number, set()).add(image_id)
                        owner._append("image_served", {"page_number": number, "image_id": image_id,
                                                        "sha256": image_row["image"]["sha256"]})
                    self.send_response(200); self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
                    self.end_headers(); self.wfile.write(data); return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    self._json(403, {"error": "local review session authorization required"}); return
                if int(self.headers.get("Content-Length", "0")) > 1_000_000:
                    self._json(413, {"error": "request too large"}); return
                try:
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    payload = strict_json_loads(body.decode("utf-8"))
                except (ValueError, UnicodeError):
                    self._json(400, {"error": "invalid JSON"}); return
                parsed = urlparse(self.path)
                if parsed.path == "/image-loaded":
                    try:
                        number = int(payload["page_number"]); image_id = payload["image_id"]
                        row = _page_images(owner.base["pages"][number - 1])[image_id]
                    except (ValueError, IndexError, KeyError, TypeError):
                        self._json(400, {"error": "unknown page image"}); return
                    with owner._lock:
                        if image_id not in owner.session["served"].get(number, set()):
                            self._json(409, {"error": "image-loaded event has no matching image-served event"}); return
                        if payload.get("width_px") != row["width_px"] or payload.get("height_px") != row["height_px"]:
                            self._json(409, {"error": "browser image dimensions do not match the render receipt"}); return
                        owner.session["loaded"].setdefault(number, set()).add(image_id)
                        owner._append("image_loaded", {"page_number": number, "image_id": image_id,
                                                        "sha256": row["image"]["sha256"],
                                                        "width_px": row["width_px"], "height_px": row["height_px"]})
                    self._json(200, {"loaded": True}); return
                if parsed.path == "/submit-page":
                    try:
                        number = int(payload["page_number"])
                        base_page = owner.base["pages"][number - 1]
                    except (ValueError, IndexError, KeyError, TypeError):
                        self._json(400, {"error": "unknown physical page"}); return
                    expected_rows = base_page["image_map"]
                    expected_ids = [row["image_id"] for row in expected_rows]
                    with owner._lock:
                        loaded = sorted(owner.session["loaded"].get(number, set()))
                        if loaded != sorted(expected_ids) or payload.get("loaded_image_ids") != sorted(expected_ids):
                            self._json(409, {"error": "every exact full-page/crop image must be served and loaded before recording"}); return
                        try:
                            validate_checklist(payload.get("checklist"), allowed_image_ids=set(expected_ids))
                            uncertainty = payload.get("uncertainty_notes")
                            if not isinstance(uncertainty, list) or any(not isinstance(x, str) or not x.strip() for x in uncertainty):
                                raise ValueError("uncertainty_notes must be nonempty text values")
                            issues = payload.get("issues")
                            if not isinstance(issues, list):
                                raise ValueError("issues must be a JSON array")
                            catalog = visual.build_source_catalog(
                                visual._load_validated_format_spec(Path(owner.base["format_spec"]["path"])),
                                strict_json_read(Path(owner.base["source_clauses"]["path"])),
                            )
                            state = "needs_human_review" if uncertainty or any(
                                row["status"] == "unable_to_assess" for row in payload["checklist"]
                            ) else "observations_recorded"
                            response = {
                                "schema_version": "2.0", "page_number": number,
                                "review_state": state,
                                "visual_summary": payload.get("visual_summary"),
                                "checklist": payload["checklist"], "issues": issues,
                                "uncertainty_notes": uncertainty,
                            }
                            visual.validate_page_response(
                                response, page_number=number, catalog=catalog,
                                allowed_image_ids=set(expected_ids), page_map=base_page["page_map"],
                            )
                        except (ValueError, KeyError, TypeError) as exc:
                            self._json(400, {"error": f"page observations invalid: {exc}"}); return
                        result = {
                            "page_number": number,
                            "review_state": state,
                            "visual_summary": payload["visual_summary"],
                            "checklist": payload["checklist"],
                            "issues": payload["issues"],
                            "uncertainty_notes": payload["uncertainty_notes"],
                            "image_ids_loaded": expected_ids,
                            "image_hashes": [row["image"]["sha256"] for row in expected_rows],
                            "reviewer_claim": owner.reviewer_claim,
                            "reviewer_identity_status": "self_declared_not_verified",
                        }
                        if number in owner.session["submissions"]:
                            self._json(409, {"error": "this page already has a submission in this session"}); return
                        owner.session["submissions"][number] = result
                        owner._append("page_submitted", result)
                        complete = len(owner.session["submissions"]) == owner.base["page_count"]
                        finalized = owner._finalize() if complete else {"complete": False}
                        if complete and getattr(owner, "server", None) is not None:
                            threading.Thread(target=owner.server.shutdown, daemon=True).start()
                    self._json(200, finalized); return
                self._json(404, {"error": "not found"})

            def log_message(self, *_args):
                return
        return Handler

    def serve(self, *, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> dict[str, Any]:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("human visual review server may bind only to loopback")
        server = http.server.ThreadingHTTPServer((host, port), self.handler())
        self.server = server
        server.daemon_threads = True
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        start_url = f"{base_url}/?t={self.token}"
        self._append("session_listening", {"url_host": base_url, "token_sha256": self.token_hash,
                                           "reviewer_identity_status": "self_declared_not_verified"})
        if open_browser:
            webbrowser.open(start_url)
        print(json.dumps({"status": "waiting_for_human_review", "url": start_url,
                          "run_id": self.base["run_id"], "page_count": self.base["page_count"],
                          "reviewer_claim": self.reviewer_claim,
                          "reviewer_identity_status": "self_declared_not_verified",
                          "model_request_made": False, "submission_ready": False}, ensure_ascii=False))
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            self._append("session_interrupted", {"session_id": self.session_id,
                                                  "pages_submitted": sorted(self.session["submissions"])})
            raise
        finally:
            server.server_close()
        return {"session_id": self.session_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="prepared v2 visual-review-manifest.json")
    parser.add_argument("--reviewer", required=True, help="self-declared reviewer label; not authenticated")
    parser.add_argument("--output-dir", type=Path, help="new session directory; default is sibling of prepared report")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    base = visual._read_object(args.report, label="prepared report")
    session_id = str(uuid.uuid4())
    out = args.output_dir or args.report.expanduser().resolve().parent / f"human-review-{session_id}"
    try:
        session = HumanVisualReviewSession(args.report, reviewer_claim=args.reviewer, output_dir=out)
        session.serve(port=args.port, open_browser=not args.no_browser)
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "output_dir": str(out),
                          "pages_submitted": sorted(session.session["submissions"]),
                          "model_request_made": False, "submission_ready": False}, ensure_ascii=False))
        return 130
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": f"{type(exc).__name__}: {exc}",
                          "model_request_made": False, "submission_ready": False}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
