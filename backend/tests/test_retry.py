"""再次入队台：仅失败单可再次入队，原快照新建作业，旧单保留。

损坏样例再次入队仍应失败且归因同类（ParseActor）。
"""

from fastapi.testclient import TestClient

from app.main import app


BROKEN_FASTQ = """@SEQ1
ACGT
NOTPLUS
IIII
"""

GOOD_FASTQ = """@SEQ1
ACGTACGT
+
IIIIIIII
@SEQ2
NNNNACGT
+
IIIIIIII
"""


def _login(client: TestClient, username: str, password: str) -> dict:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _create_job(client: TestClient, headers: dict, fastq_text: str) -> dict:
    res = client.post("/api/jobs", json={"fastqText": fastq_text}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _get_job(client: TestClient, headers: dict, job_id: int) -> dict:
    res = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _failed_stage_names(job: dict) -> list[str]:
    return [s["actor_name"] for s in job["stages"] if s["status"] == "failed"]


def test_retry_failed_job_new_failure_same_attribution():
    """损坏样例再次入队：历史多一条新失败单，旧单保留，归因同类。"""
    with TestClient(app) as client:
        ops = _login(client, "bioops", "fastq123456")

        old = _create_job(client, ops, BROKEN_FASTQ)
        old = _get_job(client, ops, old["id"])  # 后台任务跑完后取终态
        assert old["status"] == "failed"
        assert _failed_stage_names(old) == ["ParseActor"]

        res = client.post(f"/api/jobs/{old['id']}/retry", headers=ops)
        assert res.status_code == 201, res.text
        new_id = res.json()["id"]
        assert new_id != old["id"]

        new = _get_job(client, ops, new_id)
        # 用原快照新建：同样例名、同样损坏内容 → 仍失败且归因同类
        assert new["retry_of_job_id"] == old["id"]
        assert new["sample_name"] == old["sample_name"]
        assert new["status"] == "failed"
        assert _failed_stage_names(new) == _failed_stage_names(old) == ["ParseActor"]
        assert new["error_message"] == old["error_message"]

        # 旧单保留：终态与失败阶段不变，详情带新单入口
        old_after = _get_job(client, ops, old["id"])
        assert old_after["status"] == "failed"
        assert _failed_stage_names(old_after) == ["ParseActor"]
        assert new_id in old_after["retry_job_ids"]

        # 验收口令：历史多一条新失败单
        jobs = client.get("/api/jobs", headers=ops).json()
        failed_ids = [j["id"] for j in jobs if j["status"] == "failed"]
        assert old["id"] in failed_ids
        assert new_id in failed_ids
        retry_row = next(j for j in jobs if j["id"] == new_id)
        assert retry_row["retry_of_job_id"] == old["id"]


def test_retry_rejects_non_failed_job():
    """只有失败单可再次入队：成功单返回 409。"""
    with TestClient(app) as client:
        ops = _login(client, "bioops", "fastq123456")
        good = _create_job(client, ops, GOOD_FASTQ)
        good = _get_job(client, ops, good["id"])
        assert good["status"] == "success"

        res = client.post(f"/api/jobs/{good['id']}/retry", headers=ops)
        assert res.status_code == 409
        assert "仅失败作业" in res.json()["detail"]


def test_retry_missing_job_404():
    with TestClient(app) as client:
        ops = _login(client, "bioops", "fastq123456")
        res = client.post("/api/jobs/999999/retry", headers=ops)
        assert res.status_code == 404


def test_retry_forbidden_for_auditor():
    """审计员无入口：接口同样 403。"""
    with TestClient(app) as client:
        ops = _login(client, "bioops", "fastq123456")
        auditor = _login(client, "auditor", "audit123456")

        broken = _create_job(client, ops, BROKEN_FASTQ)
        broken = _get_job(client, ops, broken["id"])
        assert broken["status"] == "failed"

        res = client.post(f"/api/jobs/{broken['id']}/retry", headers=auditor)
        assert res.status_code == 403

        # 审计员仍可只读查看
        res = client.get(f"/api/jobs/{broken['id']}", headers=auditor)
        assert res.status_code == 200


def test_retry_of_retry_keeps_history_growing():
    """新失败单可再次入队，历史持续追加且旧单都保留。"""
    with TestClient(app) as client:
        ops = _login(client, "bioops", "fastq123456")

        first = _create_job(client, ops, BROKEN_FASTQ)
        first = _get_job(client, ops, first["id"])
        second = client.post(f"/api/jobs/{first['id']}/retry", headers=ops).json()
        second = _get_job(client, ops, second["id"])
        assert second["status"] == "failed"

        third_res = client.post(f"/api/jobs/{second['id']}/retry", headers=ops)
        assert third_res.status_code == 201
        third = _get_job(client, ops, third_res.json()["id"])
        assert third["status"] == "failed"
        assert third["retry_of_job_id"] == second["id"]
        assert _failed_stage_names(third) == ["ParseActor"]

        jobs = client.get("/api/jobs", headers=ops).json()
        ids = [j["id"] for j in jobs]
        for jid in (first["id"], second["id"], third["id"]):
            assert jid in ids
