"""公交验票终端换包 API（FastAPI + SQLite）。

流程：
1. POST /submissions            交包并核对（候选包 + 在用包号 + 批复新增/撤销清单）
2. POST /submissions/{id}/activate  换版生效（事务内复核在用包号仍一致）
3. GET  /packages/current       查在用包；GET /packages/{no} 按包号查包

核对规则：候选包与在用包（首次按空包）的有效搭配做差集，
实际增减须与批复逐项相符；报告只报多改/漏改项数并各举一例。
"""
from __future__ import annotations

import os
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .core import effective_combos, verify_against_approval
from .models import (
    ActivationOut,
    Approval,
    PackageContent,
    PackageOut,
    Report,
    SubmissionIn,
    SubmissionOut,
)
from .store import ActivationError, Store

DEFAULT_DB = os.environ.get("PACKAGE_DB", "packages.db")


def _approval_combos(approval: Approval) -> tuple[set, set]:
    adds = {(i.ticket_type, i.station, i.medium) for i in approval.additions}
    removes = {(i.ticket_type, i.station, i.medium) for i in approval.revocations}
    return adds, removes


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or DEFAULT_DB)

    app = FastAPI(
        title="公交验票终端换包 API",
        version="1.0.0",
        description=(
            "数据员交包：提交候选包、在用包号和批复的新增/撤销清单；"
            "系统按有效搭配（车站×介质−禁用）核对实际增减与批复是否逐项相符，"
            "通过后在换版时复核在用包号仍一致方可使候选包生效。"
        ),
    )

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse(url="/docs")

    @app.post(
        "/submissions",
        status_code=201,
        response_model=SubmissionOut,
        summary="交包并核对",
        description=(
            "提交候选包、在用包号和批复清单。首次换包 base_package_no 留空按空包核对。"
            "核对报告只给多改/漏改项数并各举一例，不枚举全部搭配。"
        ),
    )
    def submit(body: SubmissionIn):
        current = store.current_package_no()
        if body.base_package_no != current:
            raise HTTPException(
                status_code=409,
                detail=(
                    "在用包号不符：当前在用包为 "
                    f"{current or '（空）'}，提交基准为 {body.base_package_no or '（空）'}"
                ),
            )

        old_combos: set = set()
        if current is not None:
            pkg = store.get_package(current)
            old_combos = effective_combos(PackageContent(**pkg["content"]))

        new_combos = effective_combos(body.content)
        approved_add, approved_remove = _approval_combos(body.approval)

        report = verify_against_approval(old_combos, new_combos, approved_add, approved_remove)
        report["base_package_no"] = body.base_package_no
        report["candidate_package_no"] = body.package_no
        status = "verified" if report["result"] == "pass" else "rejected"

        try:
            sid = store.create_submission(
                package_no=body.package_no,
                base_package_no=body.base_package_no,
                content=body.content.model_dump(),
                approval=body.approval.model_dump(),
                report=report,
                status=status,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"包号 {body.package_no} 已存在")

        return {"submission_id": sid, "status": status, "report": report}

    @app.post(
        "/submissions/{sid}/activate",
        response_model=ActivationOut,
        summary="换版生效",
        description=(
            "仅核对通过（verified）的提交单可换版；替换时在用包号仍与提交基准一致才生效，"
            "否则换版失败，在用包照常可查。"
        ),
    )
    def activate(sid: int):
        try:
            return store.activate(sid)
        except ActivationError as exc:
            raise HTTPException(status_code=exc.code, detail=exc.detail)

    @app.get(
        "/packages/current",
        response_model=PackageOut,
        summary="查在用包",
        description="返回当前在用包完整内容；换版失败不影响本接口。",
    )
    def current_package():
        package_no = store.current_package_no()
        if package_no is None:
            raise HTTPException(status_code=404, detail="尚无在用包")
        return store.get_package(package_no)

    @app.get("/packages", summary="包列表", description="列出全部包（候选/在用/已退役）。")
    def list_packages():
        return {"items": store.list_packages()}

    @app.get(
        "/packages/{package_no}",
        response_model=PackageOut,
        summary="按包号查包",
    )
    def get_package(package_no: str):
        pkg = store.get_package(package_no)
        if pkg is None:
            raise HTTPException(status_code=404, detail=f"包 {package_no} 不存在")
        return pkg

    @app.get("/submissions", summary="提交单列表")
    def list_submissions():
        return {"items": store.list_submissions()}

    @app.get("/submissions/{sid}", summary="查提交单及核对报告")
    def get_submission(sid: int):
        sub = store.get_submission(sid)
        if sub is None:
            raise HTTPException(status_code=404, detail=f"提交单 {sid} 不存在")
        return sub

    return app


app = create_app()
