"""请求/响应数据模型（OpenAPI 文档中文字段说明）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------- 包内容 ----------

class ForbiddenPair(BaseModel):
    """禁用搭配：某票种下不允许的 车站×介质 组合。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    station: str = Field(min_length=1, description="车站代码", examples=["S101"])
    medium: str = Field(min_length=1, description="介质代码", examples=["QR"])


class TicketTypeRule(BaseModel):
    """单个票种的规则：车站清单、介质清单、禁用搭配。

    有效搭配 = 车站清单 × 介质清单 − 禁用搭配。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    ticket_type: str = Field(min_length=1, description="票种代码", examples=["ADULT"])
    stations: list[str] = Field(default_factory=list, description="车站清单")
    media: list[str] = Field(default_factory=list, description="介质清单")
    forbidden: list[ForbiddenPair] = Field(default_factory=list, description="禁用搭配")

    @model_validator(mode="after")
    def _forbidden_must_reference_lists(self) -> "TicketTypeRule":
        stations, media = set(self.stations), set(self.media)
        for pair in self.forbidden:
            if pair.station not in stations or pair.medium not in media:
                raise ValueError(
                    f"票种 {self.ticket_type} 的禁用搭配 "
                    f"({pair.station}, {pair.medium}) 引用了未列入清单的车站或介质"
                )
        return self


class PackageContent(BaseModel):
    """候选包内容：按票种列规则。"""

    ticket_types: list[TicketTypeRule] = Field(default_factory=list, description="票种规则清单")

    @model_validator(mode="after")
    def _unique_ticket_types(self) -> "PackageContent":
        seen: set[str] = set()
        for rule in self.ticket_types:
            if rule.ticket_type in seen:
                raise ValueError(f"票种重复定义: {rule.ticket_type}")
            seen.add(rule.ticket_type)
        return self


# ---------- 批复 ----------

class ApprovalItem(BaseModel):
    """一条批复：票种 + 车站 + 介质。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    ticket_type: str = Field(min_length=1, description="票种代码", examples=["ADULT"])
    station: str = Field(min_length=1, description="车站代码", examples=["S101"])
    medium: str = Field(min_length=1, description="介质代码", examples=["QR"])


class Approval(BaseModel):
    """批复的新增、撤销清单。"""

    additions: list[ApprovalItem] = Field(default_factory=list, description="批复新增清单")
    revocations: list[ApprovalItem] = Field(default_factory=list, description="批复撤销清单")


# ---------- 交包 ----------

class SubmissionIn(BaseModel):
    """交包请求：候选包 + 在用包号 + 批复清单。"""

    package_no: str = Field(min_length=1, description="候选包号", examples=["PKG-2026-001"])
    base_package_no: str | None = Field(
        default=None,
        description="在用包号；首次换包留空（null），按空包核对",
        examples=["PKG-2026-000"],
    )
    content: PackageContent = Field(description="候选包内容")
    approval: Approval = Field(description="批复清单")


# ---------- 核对报告 ----------

class ComboExample(BaseModel):
    """差异示例（仅一例，不枚举全部搭配）。"""

    direction: Literal["add", "remove"] = Field(description="差异方向：add=新增侧，remove=撤销侧")
    ticket_type: str = Field(description="票种代码")
    station: str = Field(description="车站代码")
    medium: str = Field(description="介质代码")


class DiffSide(BaseModel):
    """某一类差异的汇总：项数 + 一个例子。"""

    count: int = Field(description="差异项数")
    example: ComboExample | None = Field(default=None, description="差异示例（有差异时举一例）")


class Report(BaseModel):
    """核对报告：只汇总项数并举例，不逐一枚举车站与介质的搭配。"""

    result: Literal["pass", "fail"] = Field(description="pass=实际增减与批复逐项相符")
    base_package_no: str | None = Field(description="核对基准（在用包号，首次为 null=空包）")
    candidate_package_no: str = Field(description="候选包号")
    actual_additions: int = Field(description="实际新增搭配数")
    actual_removals: int = Field(description="实际撤销搭配数")
    approved_additions: int = Field(description="批复新增条数（去重后）")
    approved_removals: int = Field(description="批复撤销条数（去重后）")
    over_changed: DiffSide = Field(description="多改：实际改动超出批复")
    under_changed: DiffSide = Field(description="漏改：批复要求但未落实")


class SubmissionOut(BaseModel):
    submission_id: int = Field(description="提交单号")
    status: Literal["verified", "rejected"] = Field(description="verified=核对通过，可换版")
    report: Report


class ActivationOut(BaseModel):
    submission_id: int
    status: Literal["activated"]
    current_package_no: str = Field(description="换版后的在用包号")
    superseded_package_no: str | None = Field(description="被替换的包号（首次为 null）")


# ---------- 查询 ----------

class PackageOut(BaseModel):
    package_no: str
    status: Literal["candidate", "in_use", "superseded"] = Field(
        description="candidate=候选，in_use=在用，superseded=已退役"
    )
    content: PackageContent
    created_at: str


class PackageBrief(BaseModel):
    package_no: str
    status: str
    created_at: str


class SubmissionBrief(BaseModel):
    id: int
    package_no: str
    base_package_no: str | None
    status: str
    created_at: str
    activated_at: str | None


class SubmissionDetail(SubmissionBrief):
    approval: Approval
    report: Report
