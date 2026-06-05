from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi_pagination import Params
from fastapi_pagination.limit_offset import LimitOffsetPage
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Experiment, Cluster, ExperimentStatus, User
from app.models.pipeline import PipelineRun
from app.schemas import ExperimentCreate, ExperimentOut, ExperimentUpdate
from app.api.deps import get_current_active_user, check_role

router = APIRouter()


class StatsOut(BaseModel):
    total_experiments: int
    proposed: int
    running: int
    passed: int
    failed: int
    killed: int
    avg_ctr: Optional[float] = None
    avg_conversion_rate: Optional[float] = None


@router.post("/", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    experiment_in: ExperimentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("editor")),
):
    cluster_stmt = (
        select(Cluster)
        .join(Cluster.pipeline_run)
        .where(Cluster.id == experiment_in.cluster_id, PipelineRun.pipeline.has(tenant_id=current_user.tenant_id))
    )
    cluster_result = await db.execute(cluster_stmt)
    cluster = cluster_result.scalar_one_or_none()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")

    experiment = Experiment(
        tenant_id=current_user.tenant_id,
        cluster_id=experiment_in.cluster_id,
        status=experiment_in.status or ExperimentStatus.PROPOSED,
        landing_url=experiment_in.landing_url,
        spend_usd=experiment_in.spend_usd or 0,
        notes=experiment_in.notes,
    )
    db.add(experiment)
    await db.flush()
    await db.refresh(experiment)
    return experiment


@router.get("/", response_model=LimitOffsetPage[ExperimentOut])
async def list_experiments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    params: Params = Depends(),
):
    stmt = (
        select(Experiment)
        .where(Experiment.tenant_id == current_user.tenant_id)
        .order_by(Experiment.created_at.desc())
        .limit(params.limit)
        .offset(params.offset)
    )
    result = await db.execute(stmt)
    experiments = result.scalars().all()

    count_stmt = select(func.count()).select_from(Experiment).where(Experiment.tenant_id == current_user.tenant_id)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar()

    return {
        "items": experiments,
        "total": total,
        "page": 1,
        "size": params.limit,
        "pages": (total + params.limit - 1) // params.limit if total else 0,
    }


@router.put("/{experiment_id}", response_model=ExperimentOut)
async def update_experiment(
    experiment_id: int,
    update_in: ExperimentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    stmt = (
        select(Experiment)
        .join(Cluster, Experiment.cluster_id == Cluster.id)
        .where(Experiment.id == experiment_id, Cluster.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    experiment = result.scalar_one_or_none()
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    update_data = update_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(experiment, field, value)

    await db.flush()
    await db.refresh(experiment)
    return experiment


@router.get("/stats", response_model=StatsOut)
async def get_experiment_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    stmt = select(
        func.count(Experiment.id).label("total"),
        func.sum(func.case((Experiment.status == ExperimentStatus.PROPOSED, 1), else_=0)).label("proposed"),
        func.sum(func.case((Experiment.status == ExperimentStatus.RUNNING, 1), else_=0)).label("running"),
        func.sum(func.case((Experiment.status == ExperimentStatus.PASS, 1), else_=0)).label("passed"),
        func.sum(func.case((Experiment.status == ExperimentStatus.FAIL, 1), else_=0)).label("failed"),
        func.sum(func.case((Experiment.status == ExperimentStatus.KILLED, 1), else_=0)).label("killed"),
        func.avg(Experiment.ctr).label("avg_ctr"),
        func.avg(Experiment.conversion_rate).label("avg_conversion"),
    ).where(Experiment.tenant_id == current_user.tenant_id)
    
    result = await db.execute(stmt)
    row = result.one()

    return StatsOut(
        total_experiments=row.total or 0,
        proposed=row.proposed or 0,
        running=row.running or 0,
        passed=row.passed or 0,
        failed=row.failed or 0,
        killed=row.killed or 0,
        avg_ctr=float(row.avg_ctr) if row.avg_ctr else None,
        avg_conversion_rate=float(row.avg_conversion) if row.avg_conversion else None,
    )