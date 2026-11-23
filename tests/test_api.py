"""端到端测试：入库、换版、重鉴、查询。"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"), initial_version="1")
    with TestClient(app) as c:
        yield c


def intake(client, items):
    return client.post("/api/specimens", json={"items": items})


def switch(client, current, mapping, new=None):
    payload = {"current_version": current, "mapping": mapping}
    if new is not None:
        payload["new_version"] = new
    return client.post("/api/version-switches", json=payload)


def reidentify(client, version, specimen_no, species_no):
    return client.post(
        "/api/reidentifications",
        json={"version": version, "specimen_no": specimen_no, "species_no": species_no},
    )


# ---------------------------------------------------------------------- 入库

def test_intake_and_counts(client):
    r = intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "A"},
        {"specimen_no": "S3", "species_no": "B"},
    ])
    assert r.status_code == 201
    assert r.json() == {"version": "1", "created": 3, "duplicates": 0}

    data = client.get("/api/lists").json()
    assert data["version"] == "1"
    assert data["counts"] == [
        {"species_no": "A", "count": 2},
        {"species_no": "B", "count": 1},
    ]
    assert data["pending"] == []


def test_intake_duplicate_specimen_counted_once(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    r = intake(client, [{"specimen_no": "S1", "species_no": "B"}])
    assert r.json()["duplicates"] == 1
    data = client.get("/api/lists").json()
    assert data["counts"] == [{"species_no": "A", "count": 1}]


def test_intake_single_object_and_array(client):
    r = client.post("/api/specimens", json={"specimen_no": "S1", "species_no": "A"})
    assert r.status_code == 201
    r = client.post("/api/specimens", json=[{"specimen_no": "S2", "species_no": "A"}])
    assert r.status_code == 201
    assert client.get("/api/counts").json()["counts"] == [
        {"species_no": "A", "count": 2}
    ]


# ---------------------------------------------------------------------- 换版

def test_switch_one_to_one_resolves(client):
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "A"},
        {"specimen_no": "S3", "species_no": "B"},
    ])
    r = switch(client, "1", {"A": ["X"], "B": ["Y"]}, new="2")
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == "2"
    assert body["previous_version"] == "1"
    assert body["resolved"] == 3
    assert body["pending"] == []
    assert body["counts"] == [
        {"species_no": "X", "count": 2},
        {"species_no": "Y", "count": 1},
    ]
    # 查询附版号，当前版已是新版
    assert client.get("/api/lists").json()["version"] == "2"
    # 旧版名单仍可查
    old = client.get("/api/lists", params={"version": "1"}).json()
    assert old["counts"] == [
        {"species_no": "A", "count": 2},
        {"species_no": "B", "count": 1},
    ]


def test_switch_one_to_many_goes_pending_with_candidates(client):
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "B"},
    ])
    r = switch(client, "1", {"A": ["X", "Y"], "B": ["Z"]}, new="2")
    assert r.status_code == 201
    data = client.get("/api/lists").json()
    assert data["version"] == "2"
    assert data["counts"] == [{"species_no": "Z", "count": 1}]
    assert data["pending"] == [{"specimen_no": "S1", "candidates": ["X", "Y"]}]


def test_switch_pending_candidates_merge_and_dedupe(client):
    # v1: S1 已定为 A，S2 已定为 B
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "B"},
    ])
    # v2: A 拆成 [X, Y]，B 拆成 [Y, Z] -> 两件都待重鉴
    switch(client, "1", {"A": ["X", "Y"], "B": ["Y", "Z"]}, new="2")
    # v3: X->P, Y->P（同号只计一次，并集只剩 P）, Z->[Q, R]
    r = switch(client, "2", {"X": ["P"], "Y": ["P"], "Z": ["Q", "R"]}, new="3")
    assert r.status_code == 201
    data = client.get("/api/lists").json()
    assert data["version"] == "3"
    # S1 候选 [X,Y] 并集去重后只剩 P -> 归入 P
    # S2 候选 [Y,Z] -> [P,Q,R] 仍待重鉴
    assert data["counts"] == [{"species_no": "P", "count": 1}]
    assert data["pending"] == [{"specimen_no": "S2", "candidates": ["P", "Q", "R"]}]


def test_switch_mapping_candidates_deduped(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    # 对照表里同号重复只计一次 -> 只剩一种，直接归入
    r = switch(client, "1", {"A": ["X", "X", "X"]}, new="2")
    assert r.status_code == 201
    data = client.get("/api/lists").json()
    assert data["counts"] == [{"species_no": "X", "count": 1}]
    assert data["pending"] == []


def test_switch_default_new_version_increments(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    r = switch(client, "1", {"A": ["B"]})
    assert r.status_code == 201
    assert r.json()["version"] == "2"


# ---------------------------------------------------------------------- 换版失败

def test_switch_wrong_current_version_fails_and_preserves(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    r = switch(client, "99", {"A": ["X"]}, new="2")
    assert r.status_code == 409
    # 原版仍在，名单不变
    data = client.get("/api/lists").json()
    assert data["version"] == "1"
    assert data["counts"] == [{"species_no": "A", "count": 1}]


def test_switch_missing_species_in_mapping_fails(client):
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "B"},
    ])
    r = switch(client, "1", {"A": ["X"]}, new="2")  # 缺 B
    assert r.status_code == 400
    assert "B" in r.json()["detail"]
    assert client.get("/api/version").json()["version"] == "1"


def test_switch_empty_candidate_list_fails(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    r = switch(client, "1", {"A": []}, new="2")
    assert r.status_code == 400
    assert client.get("/api/version").json()["version"] == "1"


def test_switch_existing_new_version_fails(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    switch(client, "1", {"A": ["X"]}, new="2")
    r = switch(client, "2", {"X": ["Y"]}, new="2")
    assert r.status_code == 409
    assert client.get("/api/version").json()["version"] == "2"


def test_failed_switch_preserves_received_reidentifications(client):
    # 造出待重鉴标本并完成一笔重鉴，再触发一次失败的换版
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    r = reidentify(client, "2", "S1", "X")
    assert r.status_code == 201
    # 失败换版：对照表缺 X
    r = switch(client, "2", {}, new="3")
    assert r.status_code == 400
    # 已收的重鉴保留：S1 仍归入 X，统计不变
    data = client.get("/api/lists").json()
    assert data["version"] == "2"
    assert data["counts"] == [{"species_no": "X", "count": 1}]
    assert data["pending"] == []


# ---------------------------------------------------------------------- 重鉴

def test_reidentify_pending_specimen(client):
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "A"},
    ])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    r = reidentify(client, "2", "S1", "Y")
    assert r.status_code == 201
    assert r.json() == {
        "version": "2", "specimen_no": "S1", "species_no": "Y", "status": "resolved",
    }
    data = client.get("/api/lists").json()
    assert data["counts"] == [{"species_no": "Y", "count": 1}]
    assert data["pending"] == [{"specimen_no": "S2", "candidates": ["X", "Y"]}]


def test_reidentify_requires_current_version(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    r = reidentify(client, "1", "S1", "X")
    assert r.status_code == 409


def test_reidentify_species_must_be_candidate(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    r = reidentify(client, "2", "S1", "ZZ")
    assert r.status_code == 400


def test_reidentify_non_pending_specimen_conflicts(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    r = reidentify(client, "1", "S1", "A")
    assert r.status_code == 409
    r = reidentify(client, "1", "NOPE", "A")
    assert r.status_code == 404


def test_reidentify_twice_conflicts(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    assert reidentify(client, "2", "S1", "X").status_code == 201
    assert reidentify(client, "2", "S1", "Y").status_code == 409


def test_reidentification_before_switch_counts_in_new_version(client):
    """切换前已收的重鉴须计入新版。"""
    intake(client, [
        {"specimen_no": "S1", "species_no": "A"},
        {"specimen_no": "S2", "species_no": "A"},
    ])
    switch(client, "1", {"A": ["X", "Y"]}, new="2")
    # 切换前：S1 被重鉴为 X
    reidentify(client, "2", "S1", "X")
    # 换到 v3：X->M，Y->[N1, N2]
    r = switch(client, "2", {"X": ["M"], "Y": ["N1", "N2"]}, new="3")
    assert r.status_code == 201
    data = client.get("/api/lists").json()
    assert data["version"] == "3"
    # S1 按已收的 X 计入新版（归入 M），而不是按旧候选 [X,Y]
    assert data["counts"] == [{"species_no": "M", "count": 1}]
    # S2 未重鉴，候选 [X,Y] 逐件替换：X->M，Y->[N1,N2]，并集为 [M,N1,N2]
    assert data["pending"] == [{"specimen_no": "S2", "candidates": ["M", "N1", "N2"]}]


# ---------------------------------------------------------------------- 核算期间并发

def test_reidentification_during_accounting_not_blocked_by_write_lock(tmp_path):
    """换版核算尚未发布新版时，重鉴按提交时的现行版处理，不被写锁挡住。"""
    app = create_app(str(tmp_path / "test.db"), initial_version="1")
    service = app.state.service
    with TestClient(app) as client:
        intake(client, [
            {"specimen_no": "S1", "species_no": "A"},
            {"specimen_no": "S2", "species_no": "A"},
        ])
        assert switch(client, "1", {"A": ["X", "Y"]}, new="2").status_code == 201

    # 让第一步核算（只读快照读取）变慢，模拟"核算尚未发布"的窗口
    orig_states = service._states
    calls = {"n": 0}

    def slow_states(conn, version):
        calls["n"] += 1
        if calls["n"] == 1:
            import time
            time.sleep(0.8)
        return orig_states(conn, version)

    service._states = slow_states

    results = {}

    def do_switch():
        results["switch"] = service.switch_version(
            "2", "3", {"X": ["M"], "Y": ["N1", "N2"]}
        )

    def do_reid():
        import time
        time.sleep(0.2)  # 在核算窗口内提交
        start = time.monotonic()
        try:
            results["reid"] = service.reidentify("2", "S1", "X")
        except Exception as exc:  # noqa: BLE001
            results["reid_error"] = exc
        results["reid_elapsed"] = time.monotonic() - start

    t_switch = threading.Thread(target=do_switch)
    t_reid = threading.Thread(target=do_reid)
    t_switch.start()
    t_reid.start()
    t_switch.join()
    t_reid.join()

    # 重鉴成功（按提交时的 v2 生效），且没有被写锁阻塞到核算结束
    assert "reid" in results, results.get("reid_error")
    assert results["reid"]["version"] == "2"
    assert results["reid_elapsed"] < 0.6

    # 换版发布把最新状态纳入新版：S1 已重鉴为 X -> 归入 M
    with TestClient(app) as client:
        v3 = client.get("/api/lists", params={"version": "3"}).json()
        assert v3["counts"] == [{"species_no": "M", "count": 1}]
        assert v3["pending"] == [
            {"specimen_no": "S2", "candidates": ["M", "N1", "N2"]}
        ]
        # 历史版照常可查
        v2 = client.get("/api/lists", params={"version": "2"}).json()
        assert v2["counts"] == [{"species_no": "X", "count": 1}]
        assert v2["pending"] == [{"specimen_no": "S2", "candidates": ["X", "Y"]}]
        assert client.get("/api/version").json()["version"] == "3"


def test_intake_during_accounting_included_in_new_version(tmp_path):
    """核算期间入库的标本按提交时的现行版生效，发布时纳入新版。"""
    app = create_app(str(tmp_path / "test.db"), initial_version="1")
    service = app.state.service
    with TestClient(app) as client:
        intake(client, [{"specimen_no": "S1", "species_no": "A"}])

    orig_states = service._states
    calls = {"n": 0}

    def slow_states(conn, version):
        calls["n"] += 1
        if calls["n"] == 1:
            import time
            time.sleep(0.8)
        return orig_states(conn, version)

    service._states = slow_states
    results = {}

    def do_switch():
        results["switch"] = service.switch_version("1", "2", {"A": ["X"]})

    def do_intake():
        import time
        time.sleep(0.2)
        from app.schemas import SpecimenIn
        results["intake"] = service.intake([SpecimenIn(specimen_no="S2", species_no="A")])

    t_switch = threading.Thread(target=do_switch)
    t_intake = threading.Thread(target=do_intake)
    t_switch.start()
    t_intake.start()
    t_switch.join()
    t_intake.join()

    assert results["intake"]["version"] == "1"
    with TestClient(app) as client:
        v2 = client.get("/api/lists", params={"version": "2"}).json()
        # 核算期间入库的 S2 已纳入新版，与 S1 一并归入 X
        assert v2["counts"] == [{"species_no": "X", "count": 2}]


def test_failed_switch_during_accounting_leaves_no_partial_update(tmp_path):
    """核算期间状态有变导致发布失败时，整体回滚，不产生部分更新。"""
    app = create_app(str(tmp_path / "test.db"), initial_version="1")
    service = app.state.service
    with TestClient(app) as client:
        intake(client, [{"specimen_no": "S1", "species_no": "A"}])

    orig_states = service._states
    calls = {"n": 0}

    def slow_then_inject(conn, version):
        calls["n"] += 1
        if calls["n"] == 1:
            import time
            time.sleep(0.6)
            return orig_states(conn, version)
        # 第二步校验时制造一个未覆盖的旧种号，触发发布失败
        return orig_states(conn, version) + [
            {"specimen_no": "GHOST", "status": "resolved",
             "species_no": "UNMAPPED", "candidates": None}
        ]

    service._states = slow_then_inject
    with pytest.raises(Exception):
        service.switch_version("1", "2", {"A": ["X"]})
    service._states = orig_states

    with TestClient(app) as client:
        # 版本未切换，新版名单不存在，原版完好
        assert client.get("/api/version").json()["version"] == "1"
        assert client.get("/api/lists", params={"version": "2"}).status_code == 404
        v1 = client.get("/api/lists").json()
        assert v1["version"] == "1"
        assert v1["counts"] == [{"species_no": "A", "count": 1}]



def test_queries_attach_version(client):
    intake(client, [{"specimen_no": "S1", "species_no": "A"}])
    assert client.get("/api/version").json() == {"version": "1"}
    assert client.get("/api/lists").json()["version"] == "1"
    assert client.get("/api/counts").json()["version"] == "1"
    assert client.get("/api/pending").json()["version"] == "1"
    specimen = client.get("/api/specimens/S1").json()
    assert specimen == {
        "version": "1", "specimen_no": "S1", "status": "resolved", "species_no": "A",
    }


def test_unknown_version_query_404(client):
    assert client.get("/api/lists", params={"version": "nope"}).status_code == 404


def test_numeric_identifiers_accepted(client):
    r = client.post("/api/specimens", json={"specimen_no": 101, "species_no": 7})
    assert r.status_code == 201
    r = client.post(
        "/api/version-switches",
        json={"current_version": 1, "new_version": 2, "mapping": {"7": [8, 9]}},
    )
    assert r.status_code == 201
    data = client.get("/api/lists").json()
    assert data["pending"] == [{"specimen_no": "101", "candidates": ["8", "9"]}]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
