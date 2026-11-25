"""对运行中的换包 API 做端到端冒烟：空包首核 → 禁用项变更 → 交错换包 → 失败后在用包可查。

用法: python scripts/smoke.py [base_url]   （默认 http://127.0.0.1:8000）
"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
TT = "ADULT"


def content(stations, media, forbidden=()):
    return {
        "ticket_types": [
            {
                "ticket_type": TT,
                "stations": stations,
                "media": media,
                "forbidden": [{"station": s, "medium": m} for s, m in forbidden],
            }
        ]
    }


def items(combos):
    return [{"ticket_type": t, "station": s, "medium": m} for t, s, m in sorted(combos)]


def submit(client, package_no, body_content, base, additions, revocations):
    r = client.post("/submissions", json={
        "package_no": package_no,
        "base_package_no": base,
        "content": body_content,
        "approval": {"additions": items(additions), "revocations": items(revocations)},
    })
    r.raise_for_status()
    return r.json()


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as client:
        # 0) /docs 可用
        assert client.get("/docs").status_code == 200
        print("[ok] /docs 可访问")

        # 1) 首次按空包核对：S1/S2 × M1/M2 禁用 (S1,M1) → 3 条有效搭配
        v1 = content(["S1", "S2"], ["M1", "M2"], forbidden=[("S1", "M1")])
        eff1 = {(TT, "S1", "M2"), (TT, "S2", "M1"), (TT, "S2", "M2")}
        res = submit(client, "PKG-2026-001", v1, None, eff1, set())
        assert res["status"] == "verified", res
        sid1 = res["submission_id"]
        print(f"[ok] 首次交包核对通过（空包基准），提交单 {sid1}")

        # 批复漏一条 → 多改 1 项并举例，且不能换版
        bad = submit(client, "PKG-2026-001X", v1, None, eff1 - {(TT, "S2", "M1")}, set())
        assert bad["status"] == "rejected"
        over = bad["report"]["over_changed"]
        assert over["count"] == 1 and over["example"]["station"] == "S2"
        r = client.post(f"/submissions/{bad['submission_id']}/activate")
        assert r.status_code == 409
        print(f"[ok] 批复漏项被拒：多改 {over['count']} 项，示例 {over['example']}")

        r = client.post(f"/submissions/{sid1}/activate")
        r.raise_for_status()
        assert r.json()["current_package_no"] == "PKG-2026-001"
        print("[ok] PKG-2026-001 换版生效")

        # 2) 禁用项变更：放开 (S1,M1)，禁用 (S2,M2) → 增一撤一
        v2 = content(["S1", "S2"], ["M1", "M2"], forbidden=[("S2", "M2")])
        res = submit(client, "PKG-2026-002", v2, "PKG-2026-001",
                     {(TT, "S1", "M1")}, {(TT, "S2", "M2")})
        assert res["status"] == "verified", res
        sid2 = res["submission_id"]
        r = client.post(f"/submissions/{sid2}/activate")
        r.raise_for_status()
        assert client.get("/packages/current").json()["package_no"] == "PKG-2026-002"
        print("[ok] 禁用项变更按批复生效，在用包 = PKG-2026-002")

        # 3) 交错换包：003、004 都按 002 核对通过，003 先生效，004 必失败
        v3 = content(["S1", "S2", "S3"], ["M1", "M2"], forbidden=[("S2", "M2")])
        v4 = content(["S1", "S2"], ["M1", "M2", "M3"], forbidden=[("S2", "M2")])
        eff2 = {(TT, "S1", "M1"), (TT, "S1", "M2"), (TT, "S2", "M1")}
        eff3 = eff2 | {(TT, "S3", "M1"), (TT, "S3", "M2")}
        eff4 = eff2 | {(TT, "S1", "M3"), (TT, "S2", "M3")}
        sid3 = submit(client, "PKG-2026-003", v3, "PKG-2026-002",
                      eff3 - eff2, eff2 - eff3)["submission_id"]
        sid4 = submit(client, "PKG-2026-004", v4, "PKG-2026-002",
                      eff4 - eff2, eff2 - eff4)["submission_id"]
        r = client.post(f"/submissions/{sid3}/activate")
        assert r.status_code == 200
        r = client.post(f"/submissions/{sid4}/activate")
        assert r.status_code == 409, r.text
        print(f"[ok] 交错换包被拦截：{r.json()['detail']}")

        # 4) 换版失败后在用包照常可查
        current = client.get("/packages/current").json()
        assert current["package_no"] == "PKG-2026-003"
        assert current["status"] == "in_use"
        print(f"[ok] 在用包照常可查：{current['package_no']}（{current['status']}）")

        # 5) 查任意包与提交单报告
        assert client.get("/packages/PKG-2026-002").json()["status"] == "superseded"
        report = client.get(f"/submissions/{sid4}").json()
        assert report["status"] == "stale"
        print("[ok] 历史包与提交单报告可查")

    print("\n冒烟全部通过。")


if __name__ == "__main__":
    main()
