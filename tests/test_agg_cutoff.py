
import pytest
from fastapi.testclient import TestClient

import app as lagapp
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def login(c, username="admin", password="123456"):
    tok = c.post("/api/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_disabled_source_excluded_from_waterline(client):
    """排除失败测：停用源不得再进水位聚合抬高绿灯，且史迹可读。"""
    h = login(client)
    before = client.get("/api/aggregate", headers=h).json()
    assert before["total"] == 3 and before["writable"] == 1  # 订单源可写绿灯
    # 停用仍抬水位夹具：停用可写的订单源与带史迹的支付源
    assert client.post("/api/sources/订单源/disable", headers=h, json={}).status_code == 200
    assert client.post("/api/sources/支付源/disable", headers=h, json={}).status_code == 200
    after = client.get("/api/aggregate", headers=h).json()
    assert after["total"] == 1       # 只剩物流源
    assert after["writable"] == 0    # 绿灯不得被停用源抬高
    assert after["degraded"] == 0
    assert after["frozen"] == 1
    assert after["disabled"] == 2
    # 源目录排除停用
    names = [s["name"] for s in client.get("/api/sources", headers=h).json()["items"]]
    assert names == ["物流源"]
    # 史迹可读：支付源的到达史迹仍在，禁止物理删除
    arr = client.get("/api/arrivals", headers=h).json()["items"]
    assert any(a["source"] == "支付源" for a in arr)


def test_dual_seat_refresh_contention(client):
    """双席抢刷失败测：停用与水位刷新同时进行须占聚合锁，后交只记停用聚合争用。"""
    ha = login(client)                               # 甲席 admin
    hv = login(client, "viewer", "view123")          # 乙席 viewer
    # 甲席在途占用聚合锁
    assert lagapp.AGG_LOCK.acquire(blocking=False)
    try:
        r1 = client.post("/api/arrivals", headers=hv, json={
            "source": "订单源", "source_clock": "2026-11-16T10:00:00",
            "ingest_clock": "2026-11-16T10:05:00"})
        assert r1.status_code == 409 and r1.json()["detail"] == "停用聚合争用"
        r2 = client.post("/api/sources/订单源/disable", headers=hv, json={})
        assert r2.status_code == 409 and r2.json()["detail"] == "停用聚合争用"
    finally:
        lagapp.AGG_LOCK.release()
    # 聚合争用痕迹：后交只记停用聚合争用
    kinds = [e["kind"] for e in client.get("/api/agg/events", headers=ha).json()["items"]]
    assert kinds.count("停用聚合争用") >= 2
    # 后交未落库：订单源未停用、到达未写入
    srcs = client.get("/api/sources", headers=ha, params={"include_disabled": 1}).json()["items"]
    assert all(s["disabled"] == 0 for s in srcs)
    assert all(a["source"] != "订单源" for a in client.get("/api/arrivals", headers=ha).json()["items"])


def test_disable_commit_rolls_back_on_failure(client):
    """甲路径：停用牌写入与水位剔除捆成同库提交，失败则整段撤销。"""
    h = login(client)
    before = client.get("/api/aggregate", headers=h).json()

    def boom(conn):
        raise RuntimeError("水位剔除写入失败")

    orig = lagapp.recompute_waterline
    lagapp.recompute_waterline = boom
    try:
        safe = TestClient(app, raise_server_exceptions=False)
        r = safe.post("/api/sources/订单源/disable", headers=h, json={})
        assert r.status_code == 500
    finally:
        lagapp.recompute_waterline = orig
    # 整段撤销：停用牌未落、水位聚合维持原值、无切断痕迹
    srcs = client.get("/api/sources", headers=h, params={"include_disabled": 1}).json()["items"]
    assert all(s["disabled"] == 0 for s in srcs)
    after = client.get("/api/aggregate", headers=h).json()
    assert after["total"] == before["total"] and after["writable"] == before["writable"]
    kinds = [e["kind"] for e in client.get("/api/agg/events", headers=h).json()["items"]]
    assert "停用切断" not in kinds


def test_method_deviation_rejected_and_logged(client):
    """偏离聚合切断卡则拒写并记聚合偏了；甲乙当日不得混用。"""
    h = login(client)
    card = client.get("/api/agg/card", headers=h).json()
    assert card["method"] == "甲" and card["sealed"] == 1
    r = client.post("/api/sources/订单源/disable", headers=h, json={"method": "乙"})
    assert r.status_code == 409 and r.json()["detail"] == "聚合偏了"
    # 拒写：订单源未停用
    names = [s["name"] for s in client.get("/api/sources", headers=h).json()["items"]]
    assert "订单源" in names
    # 切断册留痕，当日卡不改口
    kinds = [e["kind"] for e in client.get("/api/agg/events", headers=h).json()["items"]]
    assert "聚合偏了" in kinds
    again = client.get("/api/agg/card", headers=h).json()
    assert again["method"] == "甲" and again["day"] == card["day"]


def test_history_readable_and_disabled_writes_rejected(client):
    """史迹可读：停用不物理删除；停用源拒绝新到达与补齐；启用后回挂。"""
    h = login(client)
    assert client.post("/api/sources/支付源/disable", headers=h, json={}).status_code == 200
    # 史迹可读：支付源到达史迹仍在
    arr = client.get("/api/arrivals", headers=h).json()["items"]
    assert any(a["source"] == "支付源" for a in arr)
    # 停用源拒绝新到达与新补齐
    r1 = client.post("/api/arrivals", headers=h, json={
        "source": "支付源", "source_clock": "2026-11-17T08:00:00",
        "ingest_clock": "2026-11-17T08:05:00"})
    assert r1.status_code == 409 and r1.json()["detail"] == "源已停用，到达不入水位"
    r2 = client.post("/api/catchup/支付源", headers=h)
    assert r2.status_code == 409
    # 启用回挂：目录与水位恢复计入
    assert client.post("/api/sources/支付源/enable", headers=h, json={}).status_code == 200
    names = [s["name"] for s in client.get("/api/sources", headers=h).json()["items"]]
    assert "支付源" in names
    agg = client.get("/api/aggregate", headers=h).json()
    assert agg["total"] == 3 and agg["disabled"] == 0


def test_card_sealed_same_day_no_flip(client):
    """聚合切断卡当日封档：显式甲路径可写，卡不重复封、不改口。"""
    h = login(client)
    card = client.get("/api/agg/card", headers=h).json()
    r = client.post("/api/sources/物流源/disable", headers=h, json={"method": "甲"})
    assert r.status_code == 200 and r.json()["method"] == "甲"
    again = client.get("/api/agg/card", headers=h).json()
    assert again["id"] == card["id"] and again["method"] == "甲" and again["sealed"] == 1
    kinds = [e["kind"] for e in client.get("/api/agg/events", headers=h).json()["items"]]
    assert kinds.count("切断封档") == 1 and "停用切断" in kinds
