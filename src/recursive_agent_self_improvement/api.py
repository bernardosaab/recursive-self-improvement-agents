from __future__ import annotations

import html
import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .service import make_candidate
from .store import Registry
from .study import Study

app = FastAPI(
    title="Recursive Agent Self-Improvement",
    description="Local illustrative API for bounded runtime versions and human review.",
    version="0.1.0",
)
registry: Registry | None = None


def _registry() -> Registry:
    global registry
    if registry is None:
        registry = Registry()
    return registry


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_version_id: str
    changes: dict[str, Any]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_version_id: str
    report_id: str
    reviewer: str = Field(min_length=1, max_length=120)
    accepted: bool
    reason: str = Field(min_length=1, max_length=1000)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)


class StudyReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cycle: int = Field(ge=1, le=3)
    report_hash: str = Field(min_length=64, max_length=64)
    candidate_hash: str = Field(min_length=64, max_length=64)
    reviewer: str = Field(min_length=1, max_length=120)
    accepted: bool
    reason: str = Field(min_length=1, max_length=1000)


class StudyRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_hash: str = Field(min_length=64, max_length=64)
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/versions")
def versions() -> dict[str, Any]:
    store = _registry()
    return {"active_version_id": store.active_version(), "versions": store.list_versions()}


@app.get("/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    try:
        report, digest = _registry().get_report(report_id)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"report": report.model_dump(mode="json"), "report_hash": digest}


@app.get("/decisions")
def decisions() -> dict[str, Any]:
    return {"decisions": _registry().list_decisions()}


@app.post("/versions")
def create_candidate(request: CandidateRequest) -> dict[str, Any]:
    try:
        return make_candidate(_registry(), request.parent_version_id, request.changes)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/reviews")
def review(request: ReviewRequest) -> dict[str, Any]:
    try:
        decision = _registry().decide(
            request.candidate_version_id,
            request.report_id,
            request.reviewer,
            request.accepted,
            request.reason,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return decision.model_dump(mode="json")


@app.post("/rollback")
def rollback(request: RollbackRequest) -> dict[str, str]:
    try:
        return _registry().rollback(request.version_id, request.reviewer, request.reason)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/study")
def study_history() -> dict[str, Any]:
    return Study(_registry().path).history()


@app.post("/study/reviews")
def study_review(request: StudyReviewRequest) -> dict[str, Any]:
    try:
        return Study(_registry().path).review(**request.model_dump())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/study/rollback")
def study_rollback(request: StudyRollbackRequest) -> dict[str, str]:
    try:
        return Study(_registry().path).rollback(**request.model_dump())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/study/review/{cycle}", response_class=HTMLResponse)
def study_review_page(cycle: int) -> HTMLResponse:
    try:
        report = Study(_registry().path).report(cycle)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    methods = report["selection"]["methods"]
    comparisons = report["selection"]["paired_vs_proposed"]
    regressions = {
        method: {
            "losses": comparison["losses"],
            "delta": comparison["accuracy_delta"],
            "per_family": methods[method]["per_family"],
        }
        for method, comparison in comparisons.items()
    }
    proposed = report["adversarial"]["results"]
    blocked = [
        {"task_id": item["trace"]["task_id"], "events": item["trace"]["events"]}
        for item in proposed
        if item["trace"]["blocked_attempts"]
    ]

    def show(value: Any) -> str:
        return html.escape(json.dumps(value, indent=2))

    history = Study(_registry().path).history()
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Study review {cycle}</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f4f6;padding:1rem}}
input,textarea,button{{font:inherit;margin:.3rem}} label{{display:block}} .warning{{color:#8a3000}}
</style></head><body>
<h1>Runtime strategy review: cycle {cycle}</h1>
<p class="warning">Local illustrative review; identity is self-reported, not authentication.
No live external actions are performed.</p>
<p>Report hash: <code>{html.escape(report["report_hash"])}</code><br>
Version hash: <code>{html.escape(report["candidate_hash"])}</code><br>
Promotion eligible: <strong>{report["promotion_eligible"]}</strong></p>
<h2>Strategy diff and proposal</h2><pre>{show(report["candidate_diff"])}</pre>
<pre>{show(report["proposal"])}</pre>
<h2>Evidence-linked development diagnostics</h2><pre>{show(report["diagnostics"])}</pre>
<h2>Selection metrics and paired regressions</h2><pre>{show(methods)}</pre>
<pre>{show(regressions)}</pre>
<h2>Blocked requests in proposed runs</h2><pre>{show(blocked)}</pre>
<h2>Budget and provider usage</h2><pre>{show(report["budget"])}</pre>
<pre>{show(report["provider_usage"])}</pre>
<h2>Decision history</h2><pre>{show(history["decisions"])}</pre>
<form id="review"><label>Reviewer <input id="reviewer" required maxlength="120"></label>
<label>Reason <textarea id="reason" required maxlength="1000"></textarea></label>
<button type="submit" name="accepted" value="true">Accept if eligible</button>
<button type="submit" name="accepted" value="false">Reject</button></form>
<pre id="result" aria-live="polite"></pre>
<script>
document.getElementById('review').addEventListener('submit', async (event) => {{
 event.preventDefault();
 const payload = {{cycle: {cycle}, report_hash: {json.dumps(report["report_hash"])},
 candidate_hash: {json.dumps(report["candidate_hash"])},
 reviewer: document.getElementById('reviewer').value,
 reason: document.getElementById('reason').value,
 accepted: event.submitter.value === 'true'}};
 try {{
   const response = await fetch('/study/reviews', {{method:'POST',
     headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
   document.getElementById('result').textContent = JSON.stringify(await response.json(), null, 2);
 }} catch (error) {{ document.getElementById('result').textContent = String(error); }}
}});
</script></body></html>"""
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})
