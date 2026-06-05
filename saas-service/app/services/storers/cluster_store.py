"""批量保存 Cluster 记录，并回写信号的 cluster_id。"""
from typing import List, Dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.signal import Signal
from app.models.signal import Cluster

async def save_clusters(
    session: AsyncSession,
    clusters: list,
    tenant_id: int,
    pipeline_run_id: int,
    implied_task_map: Dict[str, int],
) -> list:
    """clusters 是 dict list，每个含 summary, members(list of implied_task), score, verdict 等
    implied_task_map: implied_task -> signal_id 映射
    """
    saved = []
    for c in clusters:
        obj = Cluster(
            tenant_id=tenant_id,
            pipeline_run_id=pipeline_run_id,
            summary=c.get("summary", ""),
            signal_count=c.get("signal_count", len(c.get("members", []))),
            avg_pain_intensity=c.get("avg_pain_intensity"),
            pay_signal_ratio=c.get("pay_signal_ratio"),
            ai_fit_ratio=c.get("ai_fit_ratio"),
            competitor_count=c.get("competitor_count", 0),
            score=c.get("score"),
            verdict=c.get("verdict"),
        )
        session.add(obj)
        await session.flush()
        # 回写 signal.cluster_id
        for task_text in c.get("members", []):
            sig_id = implied_task_map.get(task_text)
            if sig_id:
                stmt = select(Signal).where(Signal.id == sig_id)
                res = await session.execute(stmt)
                sig = res.scalar_one_or_none()
                if sig:
                    sig.cluster_id = obj.id
        saved.append(obj)
    await session.flush()
    return saved
