"""换包 API 自测：空包首核、多改/漏改报告、禁用项变更、交错换包。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

TT = "ADULT"


# ---------- 工具 ----------

def make_content(stations=("S1", "S2"), media=("M1", "M2"), forbidden=(), ticket_type=TT):
    return {
        "ticket_types": [
            {
                "ticket_type": ticket_type,
                "stations": list(stations),
                "media": list(media),
                "forbidden": [{"station": s, "medium": m} for s, m in forbidden],
            }
        ]
    }


def combos_of(content: dict) -> set[tuple[str, str, str]]:
    out = set()
    for rule in content["ticket_types"]:
        forb = {(f["station"], f["medium"]) for f in rule["forbidden"]}
        for s in rule["stations"]:
            for m in rule["media"]:
                if (s, m) not in forb:
                    out.add((rule["ticket_type"], s, m))
    return out


def as_items(combos) -> list[dict]:
    return [
        {"ticket_type": t, "station": s, "medium": m} for t, s, m in sorted(combos)
    ]


def submit(client: TestClient, package_no, content, base=None, additions=(), revocations=()):
    return client.post(
        "/submissions",
        json={
            "package_no": package_no,
            "base_package_no": base,
            "content": content,
            "approval": {"additions": as_items(additions), "revocations": as_items(revocations)},
        },
    )


def submit_and_activate(client: TestClient, package_no, content, base=None):
    """按精确批复交包并直接换版，返回新在用包号。"""
    eff = combos_of(content)
    old = set()
    if base is not None:
        old = combos_of(client.get(f"/packages/{base}").json()["content"])
    r = submit(client, package_no, content, base=base,
               additions=eff - old, revocations=old - eff)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "verified", r.json()
    r = client.post(f"/submissions/{r.json()['submission_id']}/activate")
    assert r.status_code == 200, r.text
    return package_no


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


# ---------- 首次按空包核对 ----------

def test_first_submit_empty_base_pass_and_activate(client):
    content = make_content(forbidden=[("S1", "M1")])  # 有效搭配 3 条
    eff = combos_of(content)
    assert len(eff) == 3

    r = submit(client, "PKG-001", content, base=None, additions=eff)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "verified"
    report = body["report"]
    assert report["result"] == "pass"
    assert report["base_package_no"] is None  # 空包核对
    assert report["actual_additions"] == 3
    assert report["actual_removals"] == 0

    r = client.post(f"/submissions/{body['submission_id']}/activate")
    assert r.status_code == 200
    assert r.json()["current_package_no"] == "PKG-001"
    assert r.json()["superseded_package_no"] is None

    r = client.get("/packages/current")
    assert r.status_code == 200
    assert r.json()["package_no"] == "PKG-001"
    assert r.json()["status"] == "in_use"
    assert r.json()["content"] == content


def test_first_submit_must_use_empty_base(client):
    r = submit(client, "PKG-001", make_content(), base="PKG-000")
    assert r.status_code == 409
    assert "在用包号不符" in r.json()["detail"]


def test_first_submit_revocations_impossible(client):
    """空包无可撤销项，批复撤销必然漏改。"""
    r = submit(client, "PKG-001", make_content(), base=None,
               additions=combos_of(make_content()),
               revocations={(TT, "S1", "M1")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "rejected"
    assert body["report"]["under_changed"]["count"] == 1
    assert body["report"]["under_changed"]["example"]["direction"] == "remove"


# ---------- 多改 / 漏改报告 ----------

def test_under_changed_report_with_one_example(client):
    """批复漏了一条新增 → 多改 1 项并举例（实际改了但批复没有）。"""
    content = make_content()  # 4 条有效搭配
    eff = combos_of(content)
    missing = sorted(eff)[0]
    r = submit(client, "PKG-001", content, base=None, additions=eff - {missing})
    body = r.json()
    assert body["status"] == "rejected"
    report = body["report"]
    assert report["over_changed"]["count"] == 1
    example = report["over_changed"]["example"]
    assert example["direction"] == "add"
    assert (example["ticket_type"], example["station"], example["medium"]) == missing
    assert report["under_changed"]["count"] == 0
    # 核对未通过不能换版
    r = client.post(f"/submissions/{body['submission_id']}/activate")
    assert r.status_code == 409
    assert client.get("/packages/current").status_code == 404


def test_over_and_under_changed_together(client):
    """候选包多带了车站（多改），批复又写了不存在的搭配（漏改）。"""
    content = make_content(stations=("S1", "S2", "S3"))  # 6 条有效搭配
    approved = combos_of(make_content())  # 只批了 S1/S2 的 4 条
    approved.add((TT, "S9", "M1"))        # 批复多写一条不存在的
    r = submit(client, "PKG-001", content, base=None, additions=approved)
    report = r.json()["report"]
    assert report["result"] == "fail"
    assert report["over_changed"]["count"] == 2   # S3×{M1,M2} 多改
    assert report["over_changed"]["example"]["station"] == "S3"
    assert report["under_changed"]["count"] == 1  # (S9,M1) 漏改
    assert report["under_changed"]["example"] == {
        "direction": "add", "ticket_type": TT, "station": "S9", "medium": "M1",
    }


def test_report_never_enumerates_combos(client):
    """报告只含项数与单个例子，不出现搭配清单。"""
    content = make_content(stations=("S1", "S2", "S3", "S4"), media=("M1", "M2", "M3"))
    r = submit(client, "PKG-001", content, base=None, additions=set())
    report = r.json()["report"]
    assert set(report) == {
        "result", "base_package_no", "candidate_package_no",
        "actual_additions", "actual_removals",
        "approved_additions", "approved_removals",
        "over_changed", "under_changed",
    }
    for side in ("over_changed", "under_changed"):
        assert set(report[side]) == {"count", "example"}
        example = report[side]["example"]
        assert example is None or isinstance(example, dict)  # 单例而非列表
    assert report["over_changed"]["count"] == 12  # 只报项数


# ---------- 禁用搭配 ----------

def test_forbidden_pairs_excluded_from_effective_set(client):
    """禁用搭配从有效集合中扣除，批复只需覆盖剩余搭配。"""
    content = make_content(forbidden=[("S1", "M1"), ("S2", "M2")])
    eff = combos_of(content)
    assert eff == {(TT, "S1", "M2"), (TT, "S2", "M1")}
    r = submit(client, "PKG-001", content, base=None, additions=eff)
    assert r.json()["status"] == "verified"


def test_forbidden_reference_must_exist(client):
    r = client.post("/submissions", json={
        "package_no": "PKG-001", "base_package_no": None,
        "content": make_content(forbidden=[("S9", "M1")]),
        "approval": {"additions": [], "revocations": []},
    })
    assert r.status_code == 422


def test_forbidden_change_between_versions(client):
    """禁用项变更：放开 (S1,M1)、禁用 (S2,M2) → 一增一撤，批复相符即生效。"""
    v1 = make_content(forbidden=[("S1", "M1")])
    submit_and_activate(client, "PKG-001", v1, base=None)

    v2 = make_content(forbidden=[("S2", "M2")])
    r = submit(client, "PKG-002", v2, base="PKG-001",
               additions={(TT, "S1", "M1")}, revocations={(TT, "S2", "M2")})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "verified"
    assert body["report"]["actual_additions"] == 1
    assert body["report"]["actual_removals"] == 1

    r = client.post(f"/submissions/{body['submission_id']}/activate")
    assert r.status_code == 200
    assert r.json()["superseded_package_no"] == "PKG-001"

    current = client.get("/packages/current").json()
    assert current["package_no"] == "PKG-002"
    assert client.get("/packages/PKG-001").json()["status"] == "superseded"


def test_forbidden_change_without_approval_fails(client):
    """禁用项变更未获批复 → 多改、漏改各自计数并举例。"""
    v1 = make_content(forbidden=[("S1", "M1")])
    submit_and_activate(client, "PKG-001", v1, base=None)

    v2 = make_content(forbidden=[("S2", "M2")])
    # 批复只给了新增，没给撤销
    r = submit(client, "PKG-002", v2, base="PKG-001",
               additions={(TT, "S1", "M1")}, revocations=set())
    report = r.json()["report"]
    assert report["result"] == "fail"
    assert report["over_changed"]["count"] == 1
    assert report["over_changed"]["example"] == {
        "direction": "remove", "ticket_type": TT, "station": "S2", "medium": "M2",
    }
    assert report["under_changed"]["count"] == 0
    # 换版失败后在用包照常可查
    assert client.get("/packages/current").json()["package_no"] == "PKG-001"


# ---------- 交错换包 ----------

def _change_content(base_content: dict, extra_station: str) -> dict:
    content = {**base_content, "ticket_types": [dict(base_content["ticket_types"][0])]}
    content["ticket_types"][0]["stations"] = list(content["ticket_types"][0]["stations"]) + [extra_station]
    return content


def test_interleaved_activation_only_first_wins(client):
    """A、B 都按 PKG-001 核对通过；A 先换版后，B 替换时在用包号已不一致 → 失败。"""
    v1 = make_content()
    submit_and_activate(client, "PKG-001", v1, base=None)

    content_a = _change_content(v1, "SA")
    content_b = _change_content(v1, "SB")
    eff_a, eff_b = combos_of(content_a), combos_of(content_b)
    eff_v1 = combos_of(v1)

    ra = submit(client, "PKG-002", content_a, base="PKG-001",
                additions=eff_a - eff_v1, revocations=eff_v1 - eff_a)
    rb = submit(client, "PKG-003", content_b, base="PKG-001",
                additions=eff_b - eff_v1, revocations=eff_v1 - eff_b)
    assert ra.json()["status"] == rb.json()["status"] == "verified"
    sid_a = ra.json()["submission_id"]
    sid_b = rb.json()["submission_id"]

    # A 先换版成功
    r = client.post(f"/submissions/{sid_a}/activate")
    assert r.status_code == 200
    assert r.json()["current_package_no"] == "PKG-002"

    # B 替换时在用包号已变 → 409，提交单失效
    r = client.post(f"/submissions/{sid_b}/activate")
    assert r.status_code == 409
    assert "在用包号已变更" in r.json()["detail"]
    assert client.get(f"/submissions/{sid_b}").json()["status"] == "stale"

    # 换版失败，在用包照常可查
    current = client.get("/packages/current")
    assert current.status_code == 200
    assert current.json()["package_no"] == "PKG-002"

    # 失效提交单不能再换版
    r = client.post(f"/submissions/{sid_b}/activate")
    assert r.status_code == 409


def test_concurrent_activation_exactly_one_wins(client):
    """多线程同时换版：恰好一个成功，其余 409，在用包始终可查。"""
    v1 = make_content()
    submit_and_activate(client, "PKG-001", v1, base=None)
    eff_v1 = combos_of(v1)

    sids = []
    for i in range(8):
        content = _change_content(v1, f"SX{i}")
        eff = combos_of(content)
        r = submit(client, f"PKG-C{i}", content, base="PKG-001",
                   additions=eff - eff_v1, revocations=eff_v1 - eff)
        assert r.json()["status"] == "verified"
        sids.append(r.json()["submission_id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda sid: client.post(f"/submissions/{sid}/activate"), sids))

    ok = [r for r in results if r.status_code == 200]
    conflict = [r for r in results if r.status_code == 409]
    assert len(ok) == 1
    assert len(conflict) == 7
    winner = ok[0].json()["current_package_no"]
    assert client.get("/packages/current").json()["package_no"] == winner


# ---------- 其它约束 ----------

def test_base_mismatch_against_current(client):
    submit_and_activate(client, "PKG-001", make_content(), base=None)
    r = submit(client, "PKG-002", make_content(), base="PKG-999")
    assert r.status_code == 409
    r = submit(client, "PKG-002", make_content(), base=None)
    assert r.status_code == 409  # 已有在用包，不能再按空包交


def test_duplicate_package_no_rejected(client):
    content = make_content()
    eff = combos_of(content)
    assert submit(client, "PKG-001", content, additions=eff).status_code == 201
    r = submit(client, "PKG-001", content, additions=eff)
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]


def test_activate_unknown_submission(client):
    assert client.post("/submissions/999/activate").status_code == 404


def test_query_packages_and_submissions(client):
    submit_and_activate(client, "PKG-001", make_content(), base=None)
    assert client.get("/packages/PKG-001").json()["status"] == "in_use"
    assert client.get("/packages/NOPE").status_code == 404
    packages = client.get("/packages").json()["items"]
    assert [p["package_no"] for p in packages] == ["PKG-001"]
    subs = client.get("/submissions").json()["items"]
    assert len(subs) == 1 and subs[0]["status"] == "activated"
    detail = client.get(f"/submissions/{subs[0]['id']}").json()
    assert detail["report"]["result"] == "pass"
    assert detail["activated_at"] is not None
