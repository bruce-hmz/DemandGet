#!/usr/bin/env python3
"""
Seed demo data into the database.
Creates a Tenant, User, Pipeline, and PipelineRun for testing.
"""

import argparse
import asyncio
import sys
from passlib.hash import bcrypt

from app.database import AsyncSessionLocal, init_db
from app.models import Tenant, User, Pipeline, PipelineRun


async def seed_demo_data(reset: bool = False):
    """Seed demo data into the database."""
    async with AsyncSessionLocal() as session:
        try:
            if reset:
                print("Resetting database (deleting all demo data)...")
                # Delete in reverse order due to foreign keys
                await session.execute(
                    PipelineRun.__table__.delete().where(
                        PipelineRun.pipeline_id.in_(
                            Pipeline.__table__.select().where(
                                Pipeline.tenant_id.in_(
                                    Tenant.__table__.select().where(
                                        Tenant.slug == "demo"
                                    ).with_only_columns(Tenant.id)
                                )
                            ).with_only_columns(Pipeline.id)
                        )
                    )
                )
                await session.execute(
                    Pipeline.__table__.delete().where(
                        Pipeline.tenant_id.in_(
                            Tenant.__table__.select().where(
                                Tenant.slug == "demo"
                            ).with_only_columns(Tenant.id)
                        )
                    )
                )
                await session.execute(
                    User.__table__.delete().where(
                        User.tenant_id.in_(
                            Tenant.__table__.select().where(
                                Tenant.slug == "demo"
                            ).with_only_columns(Tenant.id)
                        )
                    )
                )
                await session.execute(
                    Tenant.__table__.delete().where(Tenant.slug == "demo")
                )
                await session.commit()
                print("Database reset complete.")
            
            # Check if demo tenant already exists
            from sqlalchemy import select
            result = await session.execute(select(Tenant).where(Tenant.slug == "demo"))
            existing_tenant = result.scalar_one_or_none()
            
            if existing_tenant:
                print("Demo data already exists. Use --reset to recreate.")
                return
            
            # Create Tenant
            tenant = Tenant(
                name="Demo",
                slug="demo",
                plan="free",
                monthly_budget=100.00,
                token_limit=100000,
            )
            session.add(tenant)
            await session.flush()
            print(f"Created Tenant: {tenant.name} (id={tenant.id}, slug={tenant.slug})")
            
            # Create User (password hashed with bcrypt)
            hashed_password = bcrypt.hash("demo123")
            user = User(
                email="demo@example.com",
                hashed_password=hashed_password,
                full_name="Demo User",
                tenant_id=tenant.id,
                role="admin",
                is_active=True,
            )
            session.add(user)
            await session.flush()
            print(f"Created User: {user.email} (id={user.id}, role={user.role})")
            
            # Create Pipeline
            pipeline = Pipeline(
                tenant_id=tenant.id,
                name="Demo Pipeline",
                description="Demo pipeline for testing",
                config_json={
                    "fetcher": "ddg",
                    "query": "AI startup ideas",
                    "limit": 10,
                    "extractors": ["signal_extractor"],
                    "scorers": ["ranker", "clusterer"],
                },
                schedule_cron="0 9 * * *",  # Daily at 9 AM
            )
            session.add(pipeline)
            await session.flush()
            print(f"Created Pipeline: {pipeline.name} (id={pipeline.id})")
            
            # Create PipelineRun
            pipeline_run = PipelineRun(
                pipeline_id=pipeline.id,
                status="pending",
                docs_fetched=0,
                signals_extracted=0,
                llm_tokens=0,
                cost=0,
            )
            session.add(pipeline_run)
            await session.flush()
            print(f"Created PipelineRun: id={pipeline_run.id}, status={pipeline_run.status}")
            
            await session.commit()
            print("\nDemo data seeded successfully!")
            
        except Exception as e:
            await session.rollback()
            print(f"Error seeding demo data: {e}", file=sys.stderr)
            raise


async def main():
    parser = argparse.ArgumentParser(description="Seed demo data into the database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing demo data before seeding",
    )
    args = parser.parse_args()
    
    print("Initializing database...")
    await init_db()
    
    print("Seeding demo data...")
    await seed_demo_data(reset=args.reset)


if __name__ == "__main__":
    asyncio.run(main())