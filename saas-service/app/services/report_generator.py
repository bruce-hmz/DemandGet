from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy import select

from app.models.signal import Signal, Cluster


async def generate_weekly_report(
    session,
    pipeline_run_id: int,
    cfg: dict,
    output_dir: str,
    signals: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    从 PostgreSQL 读取 signals + clusters，生成 Markdown 周报，返回文件路径。
    如果传入 signals list，则直接使用该列表（fallback 模式）。
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = Path(output_dir) / f"weekly-{today}.md"

    # 1. 获取 signals
    if signals is None:
        stmt = select(Signal).where(Signal.pipeline_run_id == pipeline_run_id)
        result = await session.execute(stmt)
        db_signals = result.scalars().all()
        signals = [
            {
                "source_channel": s.source_channel,
                "source_url": s.source_url,
                "raw_quote": s.raw_quote,
                "user_role": s.user_role,
                "pain_category": s.pain_category,
                "pain_intensity": s.pain_intensity,
                "implied_task": s.implied_task,
                "ai_solvable": s.ai_solvable,
                "monetization_signal": s.monetization_signal,
            }
            for s in db_signals
        ]

    # 2. 获取 clusters
    stmt = select(Cluster).where(Cluster.pipeline_run_id == pipeline_run_id)
    result = await session.execute(stmt)
    db_clusters = result.scalars().all()
    clusters = [
        {
            "summary": c.summary,
            "signal_count": c.signal_count,
            "avg_pain_intensity": float(c.avg_pain_intensity) if c.avg_pain_intensity else 0,
            "pay_signal_ratio": float(c.pay_signal_ratio) if c.pay_signal_ratio else 0,
            "ai_fit_ratio": float(c.ai_fit_ratio) if c.ai_fit_ratio else 0,
            "score": float(c.score) if c.score else 0,
            "verdict": c.verdict or "MAYBE",
        }
        for c in db_clusters
    ]

    # 3. 生成 Markdown
    lines = [f"# Web 出海周报 {today}\n\n"]
    
    # 总览
    lines.append(f"## 总览\n\n")
    lines.append(f"- 信号数量: {len(signals)}\n")
    lines.append(f"- 聚类数量: {len(clusters)}\n\n")

    # Clusters 部分
    if clusters:
        lines.append("## 聚类分析\n\n")
        for cluster in clusters:
            lines.append(f"### {cluster['summary']}\n\n")
            lines.append(f"- **得分**: {cluster['score']:.2f}\n")
            lines.append(f"- **verdict**: {cluster['verdict']}\n")
            lines.append(f"- **信号数**: {cluster['signal_count']}\n")
            lines.append(f"- **平均痛度**: {cluster['avg_pain_intensity']:.2f}\n")
            lines.append(f"- **付费信号比**: {cluster['pay_signal_ratio']:.2%}\n")
            lines.append(f"- **AI 适合度**: {cluster['ai_fit_ratio']:.2%}\n\n")

    # Signals 部分
    if signals:
        lines.append("## 信号详情\n\n")
        for sig in signals:
            lines.append(f"- **来源**: {sig.get('source_channel', '未知')}")
            lines.append(f"  - 链接: {sig.get('source_url', '')}")
            raw_quote = sig.get('raw_quote', '')[:200]
            if len(sig.get('raw_quote', '')) > 200:
                raw_quote += "..."
            lines.append(f"  - 原文摘要: {raw_quote}")
            lines.append(f"  - 用户角色: {sig.get('user_role', '未知')}")
            lines.append(f"  - 痛点分类: {sig.get('pain_category', '未分类')}")
            lines.append(f"  - 痛烈程度: {sig.get('pain_intensity', 0)}\n")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return str(report_path)