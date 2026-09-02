import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import ExperimentCase
from app.services import experiment_runner
from app.ml.scorer import ModelNotTrainedError

router = APIRouter(prefix="/experiments", tags=["experiments"])


class RunExperimentRequest(BaseModel):
    count: int = 500
    seed: int | None = None


@router.post("")
def run_experiment(body: RunExperimentRequest, db: Session = Depends(get_db)):
    """
    Phase 7 exit criteria: one click, 500+ synthetic cases, reproducible
    (pass the same seed back to get the same run again), summarized
    immediately. Same baseline-vs-adaptive comparison as
    scripts/evaluate_policies.py, but persisted so it shows up in the
    dashboard and can be downloaded afterward.
    """
    if body.count < 1 or body.count > 5000:
        raise HTTPException(status_code=400, detail="count must be between 1 and 5000")

    try:
        run_id = experiment_runner.run_experiment(db, count=body.count, seed=body.seed)
    except ModelNotTrainedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return experiment_runner.summarize_run(db, run_id)


@router.get("")
def list_experiments(db: Session = Depends(get_db)):
    """Recent runs, most recent first — lets the dashboard offer a history, not just the latest."""
    rows = (
        db.query(ExperimentCase.run_id, ExperimentCase.created_at)
        .order_by(ExperimentCase.created_at.desc())
        .limit(500)
        .all()
    )
    seen = {}
    for run_id, created_at in rows:
        if run_id not in seen:
            seen[run_id] = created_at
    return [
        {"run_id": run_id, "created_at": created_at.isoformat() if created_at else None}
        for run_id, created_at in list(seen.items())[:20]
    ]


@router.get("/{run_id}")
def get_experiment(run_id: str, db: Session = Depends(get_db)):
    summary = experiment_runner.summarize_run(db, run_id)
    if not summary["found"]:
        raise HTTPException(status_code=404, detail="experiment run not found")
    return summary


@router.get("/{run_id}/export.csv")
def export_experiment_csv(run_id: str, db: Session = Depends(get_db)):
    """
    Phase 7 exit criteria: downloadable audit. One row per synthetic case
    per arm — exactly what a judge (or a skeptical teammate) would want to
    inspect to check the aggregate numbers aren't being fudged.
    """
    rows = (
        db.query(ExperimentCase)
        .filter(ExperimentCase.run_id == run_id)
        .order_by(ExperimentCase.created_at)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="experiment run not found")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "run_id", "arm", "revenue_case_id", "failure_category", "chosen_action",
        "amount_at_risk", "amount_recovered", "recovered", "contacts_made", "created_at",
    ])
    for r in rows:
        writer.writerow([
            r.run_id, r.arm, r.revenue_case_id, r.failure_category, r.chosen_action,
            r.amount_at_risk, r.amount_recovered, r.recovered, r.contacts_made,
            r.created_at.isoformat() if r.created_at else "",
        ])
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="recoveryos_experiment_{run_id}.csv"'},
    )
