from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.services.fetchers.hn import fetch_hn
from app.services.fetchers.stackexchange import fetch_stackexchange
from app.services.fetchers.ddg import fetch_ddg
from app.services.extractors.signal_extractor import extract_signals
from app.services.scorers.clusterer import cluster_signals_llm
from app.services.scorers.ranker import rank_clusters
from app.models.signal import Signal, Cluster
from app.models.pipeline import PipelineRun


class PipelineRunner:
    def __init__(self, cfg: dict, tenant_id: int):
        self.cfg = cfg
        self.tenant_id = tenant_id
        self.fetchers = {
            "hn": fetch_hn,
            "stackex": fetch_stackexchange,
            "ddg": fetch_ddg,
        }

    async def run(self, session: AsyncSession, pipeline_run_id: int) -> Dict[str, Any]:
        """执行完整 pipeline，返回执行摘要"""
        # 1. 获取 pipeline run 并验证租户
        stmt = select(PipelineRun).where(PipelineRun.id == pipeline_run_id)
        result = await session.execute(stmt)
        pipeline_run = result.scalar_one_or_none()
        if not pipeline_run:
            raise ValueError(f"Pipeline run {pipeline_run_id} not found")

        # TODO: 验证 tenant_id 匹配（可选）

        # 2. 根据 config.channels 选择 fetcher
        fetcher_map = {
            "hn": fetch_hn,
            "stackex": fetch_stackexchange,
            "ddg": fetch_ddg,
        }
        channels = self.cfg.get("channels", [])
        all_docs = []
        for channel in channels:
            fetcher = fetcher_map.get(channel)
            if fetcher:
                docs = fetcher(self.cfg)
                all_docs.extend(docs)

        # 3. extract_signals
        signals = extract_signals(all_docs)

        # 4. cluster + score
        clusters = cluster_signals_llm(signals, self.cfg)
        ranked_clusters = rank_clusters(clusters)

        # 5. 持久化 signals 和 clusters
        # 保存 signals
        for sig in signals:
            signal_obj = Signal(
                tenant_id=self.tenant_id,
                pipeline_run_id=pipeline_run_id,
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
            session.add(signal_obj)
        await session.flush()

        # 保存 clusters
        for cluster_data in ranked_clusters:
            cluster_obj = Cluster(
                tenant_id=self.tenant_id,
                pipeline_run_id=pipeline_run_id,
                summary=cluster_data.get("summary", ""),
                signal_count=cluster_data.get("signal_count", 0),
                avg_pain_intensity=cluster_data.get("avg_pain_intensity"),
                pay_signal_ratio=cluster_data.get("pay_signal_ratio"),
                ai_fit_ratio=cluster_data.get("ai_fit_ratio"),
                score=cluster_data.get("score"),
                verdict=cluster_data.get("verdict"),
            )
            session.add(cluster_obj)
        await session.flush()

        # TODO: 关联 signal 和 cluster (需要在 cluster_data 中包含 signal_ids)
        # 为了简单，这里省略，但实际应用中需要根据聚类结果设置 signal.cluster_id

        return {
            "status": "completed",
            "pipeline_run_id": pipeline_run_id,
            "signals_count": len(signals),
            "clusters_count": len(ranked_clusters),
        }
