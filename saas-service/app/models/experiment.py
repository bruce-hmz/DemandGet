from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class ExperimentStatus(str, Enum):
    PROPOSED = "proposed"
    RUNNING = "running"
    PASS = "pass"
    FAIL = "fail"
    KILLED = "killed"


class Experiment(Base):
    __tablename__ = "experiments"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id"), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="proposed")
    landing_url = Column(String(2048))
    spend_usd = Column(Numeric(10, 2), nullable=False, default=0)
    ctr = Column(Numeric(5, 4))
    conversion_rate = Column(Numeric(5, 4))
    notes = Column(Text)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="experiments")
    cluster = relationship("Cluster", back_populates="experiments")