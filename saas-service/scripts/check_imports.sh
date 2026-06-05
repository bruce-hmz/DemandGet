#!/bin/bash
set -e

echo "Checking Python imports..."

# Change to the saas-service directory
cd "$(dirname "$0")/.."

# Check if we can import the main app
python -c "from app.main import app; print('Main app import successful')" 2>&1

# Check if we can import the database
python -c "from app.database import init_db, engine; print('Database import successful')" 2>&1

# Check if we can import the models
python -c "from app.models import User, Tenant, Pipeline, PipelineRun, Signal, Cluster, Experiment, Member; print('Models import successful')" 2>&1

# Check if we can import the reports router (which had the issue)
python -c "from app.api.routers import reports; print('Reports router import successful')" 2>&1

echo "All checks passed."