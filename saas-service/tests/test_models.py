"""Tests for database models and their relationships."""

from app.models import (
    User, UserRole, Tenant, Pipeline, PipelineRun,
    Signal, Cluster, Experiment, ExperimentStatus, Member,
)


def test_user_role_enum():
    assert UserRole.ADMIN == "admin"
    assert UserRole.EDITOR == "editor"
    assert UserRole.VIEWER == "viewer"


def test_experiment_status_enum():
    assert ExperimentStatus.PROPOSED == "proposed"
    assert ExperimentStatus.RUNNING == "running"
    assert ExperimentStatus.PASS == "pass"
    assert ExperimentStatus.FAIL == "fail"
    assert ExperimentStatus.KILLED == "killed"


def test_tenant_table_name():
    assert Tenant.__tablename__ == "tenants"


def test_user_table_name():
    assert User.__tablename__ == "users"


def test_pipeline_table_name():
    assert Pipeline.__tablename__ == "pipelines"


def test_pipeline_run_table_name():
    assert PipelineRun.__tablename__ == "pipeline_runs"


def test_signal_table_name():
    assert Signal.__tablename__ == "signals"


def test_cluster_table_name():
    assert Cluster.__tablename__ == "clusters"


def test_experiment_table_name():
    assert Experiment.__tablename__ == "experiments"


def test_member_table_name():
    assert Member.__tablename__ == "members"


def test_tenant_relationships():
    rel_names = {key for key in Tenant.__mapper__.relationships.keys()}
    expected = {"users", "members", "pipelines", "signals", "clusters", "experiments"}
    assert expected == rel_names


def test_pipeline_relationships():
    rel_names = {key for key in Pipeline.__mapper__.relationships.keys()}
    assert "tenant" in rel_names
    assert "runs" in rel_names


def test_pipeline_run_relationships():
    rel_names = {key for key in PipelineRun.__mapper__.relationships.keys()}
    expected = {"pipeline", "signals", "clusters"}
    assert expected == rel_names


def test_signal_relationships():
    rel_names = {key for key in Signal.__mapper__.relationships.keys()}
    expected = {"tenant", "pipeline_run"}
    assert expected == rel_names


def test_cluster_relationships():
    rel_names = {key for key in Cluster.__mapper__.relationships.keys()}
    expected = {"tenant", "pipeline_run", "experiments"}
    assert expected == rel_names


def test_experiment_relationships():
    rel_names = {key for key in Experiment.__mapper__.relationships.keys()}
    expected = {"tenant", "cluster"}
    assert expected == rel_names
