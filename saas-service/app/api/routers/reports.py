from datetime import datetime
from enum import Enum
from typing import Any, Optional
import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi_pagination import Params
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.pipeline import PipelineRun, Pipeline
from app.models.signal import Signal, Cluster
from app.models.user import User
from app.api.deps import get_current_active_user

router = APIRouter()


class ExportFormat(str, Enum):
    JSON = "json"
    CSV = "csv"
    MARKDOWN = "markdown"


@router.get("/")
async def list_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    pipeline_id: Optional[int] = None,
):
    """List reports (pipeline runs) with optional filtering by pipeline."""
    stmt = (
        select(PipelineRun)
        .join(PipelineRun.pipeline)
        .where(Pipeline.tenant_id == current_user.tenant_id)
    )
    if pipeline_id:
        stmt = stmt.where(PipelineRun.pipeline_id == pipeline_id)
    stmt = stmt.order_by(PipelineRun.started_at.desc())
    
    result = await db.execute(stmt)
    runs = result.scalars().all()
    return runs


@router.get("/{run_id}")
async def get_report(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get report (pipeline run) details."""
    stmt = (
        select(PipelineRun)
        .join(PipelineRun.pipeline)
        .where(PipelineRun.id == run_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Report not found")
    return run


@router.get("/{run_id}/signals")
async def get_report_signals(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get signals for a specific report."""
    run_stmt = (
        select(PipelineRun)
        .join(PipelineRun.pipeline)
        .where(PipelineRun.id == run_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    run_result = await db.execute(run_stmt)
    if not run_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Report not found")

    stmt = (
        select(Signal)
        .where(Signal.pipeline_run_id == run_id)
        .order_by(Signal.fetched_at.desc())
    )
    result = await db.execute(stmt)
    signals = result.scalars().all()

    return signals


@router.get("/{run_id}/clusters")
async def get_report_clusters(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get clusters for a specific report."""
    run_stmt = (
        select(PipelineRun)
        .join(PipelineRun.pipeline)
        .where(PipelineRun.id == run_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    run_result = await db.execute(run_stmt)
    if not run_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Report not found")

    stmt = (
        select(Cluster)
        .where(Cluster.pipeline_run_id == run_id)
        .order_by(Cluster.score.desc())
    )
    result = await db.execute(stmt)
    clusters = result.scalars().all()

    return clusters


@router.get("/{run_id}/export")
async def export_report(
    run_id: int,
    format: ExportFormat = ExportFormat.JSON,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Export report in JSON, CSV, or Markdown format."""
    from app.models.pipeline import Pipeline
    
    run_stmt = (
        select(PipelineRun)
        .join(PipelineRun.pipeline)
        .where(PipelineRun.id == run_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    run_result = await db.execute(run_stmt)
    pipeline_run = run_result.scalar_one_or_none()
    if not pipeline_run:
        raise HTTPException(status_code=404, detail="Report not found")

    signals_stmt = select(Signal).where(Signal.pipeline_run_id == run_id)
    signals_result = await db.execute(signals_stmt)
    signals = signals_result.scalars().all()

    clusters_stmt = select(Cluster).where(Cluster.pipeline_run_id == run_id)
    clusters_result = await db.execute(clusters_stmt)
    clusters = clusters_result.scalars().all()

    if format == ExportFormat.JSON:
        return {
            "run_id": pipeline_run.id,
            "pipeline_id": pipeline_run.pipeline_id,
            "status": pipeline_run.status,
            "started_at": pipeline_run.started_at.isoformat() if pipeline_run.started_at else None,
            "signals": [
                {
                    "id": s.id,
                    "source_channel": s.source_channel,
                    "source_url": s.source_url,
                    "raw_quote": s.raw_quote,
                }
                for s in signals
            ],
            "clusters": [
                {
                    "id": c.id,
                    "summary": c.summary,
                    "score": float(c.score) if c.score else None,
                }
                for c in clusters
            ],
        }

    elif format == ExportFormat.CSV:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "source_channel", "source_url", "raw_quote"])
        for signal in signals:
            writer.writerow([signal.id, signal.source_channel, signal.source_url, signal.raw_quote])
        
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=report_{run_id}.csv"},
        )

    elif format == ExportFormat.MARKDOWN:
        md_lines = [f"# Report {run_id}", "", "## Signals", ""]
        for signal in signals:
            md_lines.append(f"- {signal.source_channel}: {signal.raw_quote[:100]}...")
        
        return Response(
            content="\n".join(md_lines),
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=report_{run_id}.md"},
        )