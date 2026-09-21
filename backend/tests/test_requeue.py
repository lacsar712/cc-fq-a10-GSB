"""Requeue tests: failed jobs re-enqueued from the original snapshot.

Covers the acceptance rule: 失败单再次入队后历史多一条新失败单,
and that a broken sample fails again with the same failed actor (归因同类).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api as api_module
from app.api import create_requeued_job
from app.database import Base, get_db
from app.main import app
from app.models import Job, JobStage
from app.pipeline.runner import create_job_stages, run_pipeline_sync


GOOD_FASTQ = """@SEQ1
ACGTACGT
+
IIIIHHHH
@SEQ2
NNNNACGT
+
IIIIIIII
"""

BROKEN_FASTQ = """@SEQ1
ACGT
NOTPLUS
IIII
"""


@pytest.fixture()
def env(monkeypatch):
    """TestClient + SQLite session factory wired into the API layer."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    # Background runner opens its own session via app.api.SessionLocal
    monkeypatch.setattr(api_module, "SessionLocal", TestingSession)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # No context manager: skip lifespan (it would create_all against postgres)
    client = TestClient(app)
    yield client, TestingSession
    app.dependency_overrides.clear()


def _login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _get(client, headers, job_id):
    res = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _submit(client, headers, fastq):
    # POST returns the pre-run (pending) serialization; fetch the final state via GET
    res = client.post("/api/jobs", json={"fastqText": fastq}, headers=headers)
    assert res.status_code == 201, res.text
    return _get(client, headers, res.json()["id"])


def _requeue(client, headers, job_id):
    res = client.post(f"/api/jobs/{job_id}/requeue", headers=headers)
    assert res.status_code == 201, res.text
    return _get(client, headers, res.json()["id"])


def _failed_stage_names(client, headers, job_id):
    res = client.get(f"/api/jobs/{job_id}/stages", headers=headers)
    assert res.status_code == 200, res.text
    return [s["actor_name"] for s in res.json() if s["status"] == "failed"]


def test_requeue_failed_job_adds_one_new_failed_job_to_history(env):
    """验收口令:失败单再次入队后历史多一条新失败单。"""
    client, _ = env
    headers = _login(client, "bioops", "fastq123456")

    old = _submit(client, headers, BROKEN_FASTQ)
    assert old["status"] == "failed"

    before = client.get("/api/jobs", headers=headers).json()
    assert len(before) == 1

    new = _requeue(client, headers, old["id"])
    assert new["id"] != old["id"]
    assert new["status"] == "failed"
    assert new["requeued_from_id"] == old["id"]

    after = client.get("/api/jobs", headers=headers).json()
    assert len(after) == len(before) + 1
    statuses = sorted(j["status"] for j in after)
    assert statuses == ["failed", "failed"]
    # 旧单保留,未被修改
    kept = {j["id"]: j for j in after}
    assert kept[old["id"]]["status"] == "failed"
    assert kept[new["id"]]["requeued_from_id"] == old["id"]


def test_requeue_uses_original_snapshot_and_fails_with_same_actor(env):
    """损坏样例再次入队仍应失败且归因同类(同一失败 Actor)。"""
    client, Session = env
    headers = _login(client, "bioops", "fastq123456")

    old = _submit(client, headers, BROKEN_FASTQ)
    new = _requeue(client, headers, old["id"])

    db = Session()
    try:
        new_row = db.query(Job).filter(Job.id == new["id"]).first()
        old_row = db.query(Job).filter(Job.id == old["id"]).first()
        # 创建接口会 strip 快照;新单须与旧单快照逐字一致
        assert new_row.fastq_snapshot == old_row.fastq_snapshot == BROKEN_FASTQ.strip()
        assert new_row.sample_name == old_row.sample_name
    finally:
        db.close()

    old_failed = _failed_stage_names(client, headers, old["id"])
    new_failed = _failed_stage_names(client, headers, new["id"])
    assert old_failed == ["ParseActor"]
    assert new_failed == old_failed  # 归因同类
    assert "必须以 +" in (new["error_message"] or "")


def test_requeue_old_job_detail_exposes_new_job_entry(env):
    """旧单详情应给出新单入口(requeued_to_ids)。"""
    client, _ = env
    headers = _login(client, "bioops", "fastq123456")

    old = _submit(client, headers, BROKEN_FASTQ)
    new = _requeue(client, headers, old["id"])

    detail = client.get(f"/api/jobs/{old['id']}", headers=headers).json()
    assert detail["requeued_to_ids"] == [new["id"]]
    new_detail = client.get(f"/api/jobs/{new['id']}", headers=headers).json()
    assert new_detail["requeued_from_id"] == old["id"]


def test_requeue_rejects_non_failed_job(env):
    client, _ = env
    headers = _login(client, "bioops", "fastq123456")

    ok_job = _submit(client, headers, GOOD_FASTQ)
    assert ok_job["status"] == "success"
    res = client.post(f"/api/jobs/{ok_job['id']}/requeue", headers=headers)
    assert res.status_code == 409

    res = client.post("/api/jobs/9999/requeue", headers=headers)
    assert res.status_code == 404


def test_auditor_has_no_requeue_entry(env):
    """审计员无再次入队入口:接口直接 403。"""
    client, _ = env
    ops = _login(client, "bioops", "fastq123456")
    auditor = _login(client, "auditor", "audit123456")

    old = _submit(client, ops, BROKEN_FASTQ)
    assert old["status"] == "failed"

    res = client.post(f"/api/jobs/{old['id']}/requeue", headers=auditor)
    assert res.status_code == 403
    # 审计员仍可读历史与详情
    assert client.get("/api/jobs", headers=auditor).status_code == 200
    assert client.get(f"/api/jobs/{old['id']}", headers=auditor).status_code == 200


def test_create_requeued_job_helper_preserves_original():
    """Unit level: helper keeps the old row and the re-run fails at the same actor."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        old = Job(
            sample_id=None,
            sample_name="自定义输入",
            status="pending",
            created_by="bioops",
            fastq_snapshot=BROKEN_FASTQ,
        )
        db.add(old)
        db.commit()
        create_job_stages(db, old.id)
        run_pipeline_sync(db, old)
        assert old.status == "failed"

        new = create_requeued_job(db, old, "bioops")
        assert new.requeued_from_id == old.id
        assert new.fastq_snapshot == old.fastq_snapshot
        assert db.query(Job).count() == 2  # 旧单保留

        run_pipeline_sync(db, new)
        assert new.status == "failed"

        def failed_actor(job_id):
            return [
                s.actor_name
                for s in db.query(JobStage)
                .filter(JobStage.job_id == job_id, JobStage.status == "failed")
                .all()
            ]

        assert failed_actor(new.id) == failed_actor(old.id) == ["ParseActor"]

        # 非失败单不可再次入队
        ok_job = Job(
            sample_id=None,
            sample_name="自定义输入",
            status="success",
            created_by="bioops",
            fastq_snapshot=GOOD_FASTQ,
        )
        db.add(ok_job)
        db.commit()
        with pytest.raises(ValueError):
            create_requeued_job(db, ok_job, "bioops")
    finally:
        db.close()
