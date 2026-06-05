"""批量保存 Signal 记录，按 tenant_id + source_url + raw_quote 去重。"""
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.signal import Signal

async def save_signals(
    session: AsyncSession,
    signals: list,
    tenant_id: int,
    pipeline_run_id: int,
) -> int:
    count = 0
    for sig in signals:
        # 先去重检查
        stmt = select(Signal).where(
            Signal.tenant_id == tenant_id,
            Signal.source_url == sig.source_url,
            Signal.raw_quote == sig.raw_quote,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            continue
        obj = Signal(
            tenant_id=tenant_id,
            pipeline_run_id=pipeline_run_id,
            source_channel=sig.source_channel,
            source_url=sig.source_url,
            raw_quote=sig.raw_quote,
            user_role=sig.user_role,
            pain_category=sig.pain_category,
            pain_intensity=sig.pain_intensity,
            implied_task=sig.implied_task,
            ai_solvable=sig.ai_solvable,
            monetization_signal=sig.monetization_signal,
            fetched_at=sig.fetched_at,
        )
        session.add(obj)
        count += 1
    await session.flush()
    return count
