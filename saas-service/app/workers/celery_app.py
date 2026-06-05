import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from celery import Celery
from celery.schedules import crontab
from celery.exceptions import Ignore
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal


celery_app = Celery(
    "saas-service",
    broker=settings.celery_broker_url or settings.redis_url,
    backend=settings.celery_result_backend or settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

celery_app.conf.beat_schedule = {
    "check-quota-daily": {
        "task": "app.workers.celery_app.check_quota_task",
        "schedule": crontab(hour=0, minute=0),
    },
}


@celery_app.task(bind=True, name="app.workers.celery_app.run_pipeline_task")
def run_pipeline_task(self, pipeline_id: int, config: dict, run_id: int | None = None):
    """完整 pipeline：fetch -> extract -> cluster -> score -> report"""
    async def _execute():
        from app.database import AsyncSessionLocal
        from app.models.pipeline import Pipeline, PipelineRun
        from app.models.signal import Signal, Cluster
        from app.services.fetchers.hn import fetch_hn
        from app.services.fetchers.stackexchange import fetch_stackexchange
        from app.services.fetchers.ddg import fetch_ddg
        from app.services.extractors.signal_extractor import extract_signals
        from app.services.scorers.clusterer import cluster_signals_llm
        from app.services.scorers.ranker import rank_clusters

        # 1. 更新 run 状态为 running
        current_tenant_id = 0
        if run_id:
            async with AsyncSessionLocal() as session:
                stmt = select(PipelineRun).where(PipelineRun.id == run_id)
                result = await session.execute(stmt)
                run = result.scalar_one_or_none()
                if run:
                    run.status = "running"
                    await session.flush()
                # 获取 pipeline 的 tenant_id
                pipeline_stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
                pipeline_result = await session.execute(pipeline_stmt)
                pipeline = pipeline_result.scalar_one_or_none()
                current_tenant_id = pipeline.tenant_id if pipeline else 0

        # 2. 根据 config.channels 选择 fetcher
        fetcher_map = {
            "hn": fetch_hn,
            "stackex": fetch_stackexchange,
            "ddg": fetch_ddg,
        }
        channels = config.get("channels", [])
        all_docs = []
        for channel in channels:
            fetcher = fetcher_map.get(channel)
            if fetcher:
                docs = fetcher(config)
                all_docs.extend(docs)

        # 3. extract_signals
        signals = extract_signals(all_docs)

        # 4. cluster + score
        clusters = cluster_signals_llm(signals, config)
        ranked_clusters = rank_clusters(clusters)

        # 5. 入库 signals + clusters
        if run_id:
            from app.services.storers.signal_store import save_signals as _save_signals
            from app.services.storers.cluster_store import save_clusters as _save_clusters
            async with AsyncSessionLocal() as session:
                # 保存 signals（去重）
                signal_count = await _save_signals(session, signals, current_tenant_id, run_id)

                # 构建 implied_task -> signal.id 映射（用于关联 cluster）
                implied_task_map = {}
                for sig in signals:
                    if sig.implied_task:
                        # 需要重新查询获取刚保存的 signal id
                        stmt = select(Signal).where(
                            Signal.tenant_id == current_tenant_id,
                            Signal.pipeline_run_id == run_id,
                            Signal.implied_task == sig.implied_task,
                            Signal.raw_quote == sig.raw_quote,
                        )
                        res = await session.execute(stmt)
                        db_sig = res.scalar_one_or_none()
                        if db_sig:
                            implied_task_map[sig.implied_task] = db_sig.id

                # 保存 clusters 并回写 signal.cluster_id
                await _save_clusters(session, ranked_clusters, current_tenant_id, run_id, implied_task_map)

        # 6. 更新 run 状态为 completed
        if run_id:
            async with AsyncSessionLocal() as session:
                stmt = select(PipelineRun).where(PipelineRun.id == run_id)
                result = await session.execute(stmt)
                run = result.scalar_one_or_none()
                if run:
                    run.status = "completed"
                    run.ended_at = datetime.utcnow()
                    await session.flush()

        return {
            "status": "completed",
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "signals_count": len(signals),
            "clusters_count": len(ranked_clusters),
        }

    try:
        return asyncio.run(_execute())
    except Exception as exc:
        # 更新 run 状态为 failed
        if run_id:
            try:
                async def _update_error():
                    from app.models.pipeline import PipelineRun
                    async with AsyncSessionLocal() as session:
                        stmt = select(PipelineRun).where(PipelineRun.id == run_id)
                        result = await session.execute(stmt)
                        run = result.scalar_one_or_none()
                        if run:
                            run.status = "failed"
                            run.error = str(exc)
                            run.ended_at = datetime.utcnow()
                            await session.flush()
                asyncio.run(_update_error())
            except Exception:
                pass
        raise Ignore()


@celery_app.task(bind=True)
def extract_signals_task(
    self,
    docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signals = []

    for doc in docs:
        signal = {
            "source_channel": doc.get("channel", "unknown"),
            "source_url": doc.get("url", ""),
            "raw_quote": doc.get("content", "")[:500],
            "user_role": doc.get("user_role"),
            "pain_category": doc.get("pain_category"),
            "pain_intensity": doc.get("pain_intensity", 0),
            "implied_task": doc.get("implied_task"),
            "ai_solvable": doc.get("ai_solvable", True),
            "monetization_signal": doc.get("monetization_signal", False),
        }
        signals.append(signal)

    return signals


@celery_app.task(bind=True)
def generate_report_task(
    self,
    run_id: int,
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    async def save_signals_and_clusters():
        from app.models.signal import Signal, Cluster
        from app.models.pipeline import Pipeline
        from app.services.scorers.clusterer import cluster_signals_llm
        from app.services.scorers.ranker import rank_clusters

        async with AsyncSessionLocal() as session:
            stmt = select(PipelineRun).where(PipelineRun.id == run_id)
            result = await session.execute(stmt)
            pipeline_run = result.scalar_one_or_none()

            if not pipeline_run:
                return None

            pipeline_stmt = select(Pipeline).where(Pipeline.id == pipeline_run.pipeline_id)
            pipeline_result = await session.execute(pipeline_stmt)
            pipeline = pipeline_result.scalar_one_or_none()

            tenant_id = pipeline.tenant_id if pipeline else None

            for sig in signals:
                signal = Signal(
                    tenant_id=tenant_id,
                    pipeline_run_id=run_id,
                    source_channel=sig.get("source_channel", "unknown"),
                    source_url=sig.get("source_url", ""),
                    raw_quote=sig.get("raw_quote", ""),
                    user_role=sig.get("user_role"),
                    pain_category=sig.get("pain_category"),
                    pain_intensity=sig.get("pain_intensity"),
                    implied_task=sig.get("implied_task"),
                    ai_solvable=sig.get("ai_solvable", True),
                    monetization_signal=sig.get("monetization_signal", False),
                )
                session.add(signal)

            await session.flush()

            clusters = cluster_signals_llm(signals, {})
            ranked_clusters = rank_clusters(clusters)

            for cluster_data in ranked_clusters:
                cluster = Cluster(
                    tenant_id=tenant_id,
                    pipeline_run_id=run_id,
                    summary=cluster_data.get("summary", ""),
                    signal_count=cluster_data.get("signal_count", 0),
                    avg_pain_intensity=cluster_data.get("avg_pain_intensity"),
                    pay_signal_ratio=cluster_data.get("pay_signal_ratio"),
                    ai_fit_ratio=cluster_data.get("ai_fit_ratio"),
                    score=cluster_data.get("score"),
                    verdict=cluster_data.get("verdict"),
                )
                session.add(cluster)

            await session.flush()

            return {
                "run_id": run_id,
                "signals_count": len(signals),
                "clusters_count": len(ranked_clusters),
            }

    return asyncio.run(save_signals_and_clusters())


@celery_app.task(bind=True)
def scan_competitors_task(
    self,
    cluster_id: int,
) -> dict[str, Any]:
    async def get_cluster():
        from app.models.signal import Cluster
        async with AsyncSessionLocal() as session:
            stmt = select(Cluster).where(Cluster.id == cluster_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    cluster = asyncio.run(get_cluster())

    if not cluster:
        raise Ignore()

    competitors = []

    self.update_state(state="PROGRESS", meta={"current": 50, "total": 100, "status": "Scanning competitors"})

    self.update_state(state="PROGRESS", meta={"current": 100, "total": 100, "status": "Complete"})

    return {
        "cluster_id": cluster_id,
        "competitor_count": len(competitors),
        "competitors": competitors,
    }


@celery_app.task
def check_quota_task() -> None:
    async def check_quotas():
        from app.models.tenant import Tenant
        async with AsyncSessionLocal() as session:
            stmt = select(Tenant).where(Tenant.plan != "enterprise")
            result = await session.execute(stmt)
            tenants = result.scalars().all()

            for tenant in tenants:
                pass

    asyncio.run(check_quotas())


@celery_app.task
def health_check_task() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}
