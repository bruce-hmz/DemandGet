from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi_pagination import Params
from fastapi_pagination.limit_offset import LimitOffsetPage
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.pipeline import Pipeline, PipelineRun
from app.models.user import User
from app.schemas import PipelineCreate, PipelineOut, PipelineUpdate
from app.api.deps import get_current_active_user, check_role
from app.workers.celery_app import run_pipeline_task

router = APIRouter()


class PipelineRunRequest(BaseModel):
    config_override: dict[str, Any] | None = None


class TaskResponse(BaseModel):
    task_id: str
    pipeline_run_id: int
    status: str = "pending"


@router.get("/", response_model=list[PipelineOut])
async def list_pipelines(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    stmt = (
        select(Pipeline)
        .where(Pipeline.tenant_id == current_user.tenant_id)
        .order_by(Pipeline.created_at.desc())
    )
    result = await db.execute(stmt)
    pipelines = result.scalars().all()
    return pipelines


@router.post("/", response_model=PipelineOut, status_code=status.HTTP_201_CREATED)
async def create_pipeline(
    pipeline_in: PipelineCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("editor")),
):
    pipeline = Pipeline(
        tenant_id=current_user.tenant_id,
        name=pipeline_in.name,
        description=pipeline_in.description,
        config_json=pipeline_in.config_json,
        schedule_cron=pipeline_in.schedule_cron,
    )
    db.add(pipeline)
    await db.flush()
    await db.refresh(pipeline)
    return pipeline


@router.get("/{pipeline_id}", response_model=PipelineOut)
async def get_pipeline(
    pipeline_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    stmt = (
        select(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    return pipeline


@router.put("/{pipeline_id}", response_model=PipelineOut)
async def update_pipeline(
    pipeline_id: int,
    update_in: PipelineUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("editor")),
):
    stmt = (
        select(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    update_data = update_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(pipeline, field, value)

    await db.flush()
    await db.refresh(pipeline)
    return pipeline


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(
    pipeline_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    stmt = (
        select(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    await db.delete(pipeline)
    return None


@router.post("/{pipeline_id}/run", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_pipeline(
    pipeline_id: int,
    run_request: PipelineRunRequest = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("editor")),
):
    stmt = (
        select(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    result = await db.execute(stmt)
    pipeline = result.scalar_one_or_none()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    pipeline_run = PipelineRun(
        pipeline_id=pipeline.id,
        status="pending",
        started_at=datetime.utcnow(),
    )
    db.add(pipeline_run)
    await db.flush()
    await db.refresh(pipeline_run)

    task = run_pipeline_task.delay(
        pipeline_id,
        run_request.config_override or pipeline.config_json,
        pipeline_run.id
    )
    
    pipeline_run.task_id = task.id
    pipeline.last_run_at = datetime.utcnow()
    
    await db.flush()
    
    return TaskResponse(
        task_id=task.id,
        pipeline_run_id=pipeline_run.id,
    )


@router.get("/{pipeline_id}/runs", response_model=list)
async def get_pipeline_runs(
    pipeline_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    pipeline_stmt = (
        select(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.tenant_id == current_user.tenant_id)
    )
    pipeline_result = await db.execute(pipeline_stmt)
    if not pipeline_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Pipeline not found")

    stmt = (
        select(PipelineRun)
        .where(PipelineRun.pipeline_id == pipeline_id)
        .order_by(PipelineRun.started_at.desc())
    )
    result = await db.execute(stmt)
    runs = result.scalars().all()
    return runs