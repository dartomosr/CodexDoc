"""Versioned contracts shared by UI, CLI, renderers and extensions."""

from string import Formatter
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PageStyle(Model):
    width_mm: float = Field(default=210, ge=100, le=500)
    height_mm: float = Field(default=297, ge=100, le=500)
    left_mm: float = Field(default=30, ge=5, le=70)
    right_mm: float = Field(default=15, ge=5, le=70)
    top_mm: float = Field(default=20, ge=5, le=70)
    bottom_mm: float = Field(default=20, ge=5, le=70)

    @model_validator(mode="after")
    def usable_area(self):
        if self.width_mm - self.left_mm - self.right_mm < 60:
            raise ValueError("Недостаточно места между левым и правым полями")
        if self.height_mm - self.top_mm - self.bottom_mm < 60:
            raise ValueError("Недостаточно места между верхним и нижним полями")
        return self


class TextStyle(Model):
    font: str = "Times New Roman"
    size_pt: float = Field(default=14, ge=8, le=24)
    line_spacing: float = Field(default=1.5, ge=1, le=3)
    indent_mm: float = Field(default=12.5, ge=0, le=30)
    paragraph_after_pt: float = Field(default=0, ge=0, le=30)


class Provenance(Model):
    title: str
    version: str = ""
    location: str = ""
    sha256: str = ""
    scope: str = ""


class ParagraphStyleSpec(Model):
    font: str = ""  # Empty means the template's body font.
    size_pt: float = Field(default=14, ge=8, le=36)
    line_spacing: float = Field(default=1.5, ge=1, le=3)
    first_line_mm: float = Field(default=0, ge=-30, le=50)
    left_mm: float = Field(default=0, ge=0, le=70)
    right_mm: float = Field(default=0, ge=0, le=70)
    before_pt: float = Field(default=0, ge=0, le=100)
    after_pt: float = Field(default=0, ge=0, le=100)
    alignment: Literal["left", "center", "right", "justify"] = "left"
    bold: bool = False


TITLE_FIELDS = {
    "organization",
    "department",
    "title",
    "kind",
    "author",
    "group",
    "supervisor",
    "city",
    "year",
}


def validate_title_text(text: str):
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None and (field not in TITLE_FIELDS or spec or conversion):
            raise ValueError(f"Недопустимая подстановка титула: {field}")
    return text


class TitleBlock(Model):
    kind: Literal["text", "authors", "reviewers"] = "text"
    text: str = ""
    y_mm: float = Field(default=0, ge=0, le=450)
    height_mm: float = Field(default=20, ge=4, le=250)
    style: ParagraphStyleSpec = Field(default_factory=lambda: ParagraphStyleSpec(alignment="center"))

    @model_validator(mode="after")
    def valid_text(self):
        validate_title_text(self.text)
        return self


class TitleLayout(Model):
    blocks: list[TitleBlock] = Field(
        default_factory=lambda: [
            TitleBlock(text="{organization}\n{department}", y_mm=0, height_mm=35),
            TitleBlock(text="{kind}", y_mm=60, height_mm=15),
            TitleBlock(
                text="{title}",
                y_mm=80,
                height_mm=35,
                style=ParagraphStyleSpec(size_pt=16, bold=True, alignment="center"),
            ),
            TitleBlock(kind="authors", y_mm=135, height_mm=35),
            TitleBlock(kind="reviewers", y_mm=175, height_mm=25),
        ]
    )
    bottom_text: str = "{city} {year}"
    bottom_height_mm: float = Field(default=12, ge=4, le=60)
    bottom_style: ParagraphStyleSpec = Field(
        default_factory=lambda: ParagraphStyleSpec(alignment="center", line_spacing=1)
    )

    @model_validator(mode="after")
    def nonoverlapping(self):
        validate_title_text(self.bottom_text)
        cursor = 0
        for block in self.blocks:
            if block.y_mm < cursor - 0.01:
                raise ValueError("Блоки титула должны идти сверху вниз без пересечений")
            cursor = block.y_mm + block.height_mm
        return self


class ContentsFormat(Model):
    title: str = "СОДЕРЖАНИЕ"
    heading: ParagraphStyleSpec = Field(default_factory=lambda: ParagraphStyleSpec(alignment="center"))
    entries: ParagraphStyleSpec = Field(default_factory=ParagraphStyleSpec)
    level_indent_mm: float = Field(default=5, ge=0, le=20)
    depth: int = Field(default=3, ge=1, le=3)


class BibliographyFormat(Model):
    title: str = "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"
    heading: ParagraphStyleSpec = Field(
        default_factory=lambda: ParagraphStyleSpec(alignment="center", bold=True)
    )
    entries: ParagraphStyleSpec = Field(default_factory=ParagraphStyleSpec)
    numbering: Literal["decimal", "brackets", "none"] = "decimal"
    heading_numbered: bool = False
    include_in_toc: bool = True
    new_page: bool = True


class Template(Model):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=140)
    engine_version: str = ""
    revision: int = Field(default=1, ge=1)
    status: Literal["ready", "draft"] = "ready"
    page: PageStyle = Field(default_factory=PageStyle)
    text: TextStyle = Field(default_factory=TextStyle)
    title_page: bool = True
    title_layout: TitleLayout | None = None
    contents_format: ContentsFormat | None = None
    bibliography_format: BibliographyFormat | None = None
    structural_heading_style: ParagraphStyleSpec | None = None
    structural_numbered: bool = False
    toc: bool = True
    numbered_headings: bool = True
    chapter_new_page: bool = True
    heading_bold: bool = True
    header: str = ""
    footer: str = ""
    required_sections: list[str] = Field(default_factory=list)
    suggested_sections: list[str] = Field(
        default_factory=lambda: ["Введение", "Основная часть", "Заключение"]
    )
    provenance: list[Provenance] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    agent_notes: str = ""
    review_note: str = ""

    @model_validator(mode="after")
    def layout_fits(self):
        if self.title_layout:
            available = self.page.height_mm - self.page.top_mm - self.page.bottom_mm - 3
            end = max((b.y_mm + b.height_mm for b in self.title_layout.blocks), default=0)
            if end > available - self.title_layout.bottom_height_mm:
                raise ValueError("Блоки титула пересекают нижний блок города/года или выходят за страницу")
        return self


class AgentProfile(Model):
    id: str = Field(default="balanced", pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = "Сбалансированный"
    autonomy: Literal["guided", "balanced", "autonomous"] = "balanced"
    verification: Literal["quick", "adaptive", "strict"] = "adaptive"
    internet: Literal["off", "ask", "allowed"] = "ask"
    errors: Literal["stop", "repair"] = "repair"
    confidential: bool = True


SourcePolicy = Literal["cite", "context", "format", "exclude"]
SourceRole = Literal["citable", "context_only", "requirements_style"]
LEGACY_POLICY_TO_ROLE = {
    "cite": "citable",
    "context": "context_only",
    "format": "requirements_style",
}
ROLE_TO_LEGACY_POLICY: dict[SourceRole, SourcePolicy] = {
    "citable": "cite",
    "context_only": "context",
    "requirements_style": "format",
}
LEGACY_FILE_ROLES = {"material", "standard", "sample", "code", "image"}


class Source(Model):
    _legacy_excluded: bool = PrivateAttr(default=False)

    id: str
    name: str
    path: str = ""
    role: SourceRole = "citable"
    sha256: str = ""
    analysis: str = ""
    reference: str = ""
    reference_verified: bool = False
    confidential: bool = False
    title_metadata: bool = False

    def __init__(self, **data):
        # Keep accepting the old constructor contract while storing only one
        # semantic role. Exclusions are routed to Workspace.excluded_sources.
        legacy_excluded = data.get("policy") == "exclude"
        if legacy_excluded:
            data = {**data, "policy": "context"}
        super().__init__(**data)
        object.__setattr__(self, "_legacy_excluded", legacy_excluded)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_role(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        policy = data.pop("policy", None)
        old_role = data.get("role")
        if old_role in LEGACY_FILE_ROLES:
            data.pop("role", None)
        if policy == "exclude":
            raise ValueError("Исключённый файл хранится отдельно от источников")
        if policy in LEGACY_POLICY_TO_ROLE:
            data["role"] = LEGACY_POLICY_TO_ROLE[policy]
        elif old_role in LEGACY_FILE_ROLES:
            data["role"] = "citable"
        return data

    @property
    def policy(self) -> SourcePolicy:
        """Deprecated API alias; the JSON contract stores only ``role``."""
        return ROLE_TO_LEGACY_POLICY[self.role]

    def __setattr__(self, name, value):
        if name == "policy":
            if value == "exclude":
                object.__setattr__(self, "_legacy_excluded", True)
                value = "context"
            if value not in LEGACY_POLICY_TO_ROLE:
                raise ValueError(f"Неизвестная политика источника: {value}")
            name, value = "role", LEGACY_POLICY_TO_ROLE[value]
        super().__setattr__(name, value)


class ExcludedSource(Model):
    id: str
    name: str
    kind: Literal["material", "standard", "sample", "code", "image"] = "material"
    confidential: bool = False


class Signatory(Model):
    name: str = Field(min_length=1)
    role: str = "Выполнил"
    group: str = ""


class Metadata(Model):
    title: str = Field(default="Новый документ", min_length=1)
    kind: str = "Отчёт"
    organization: str = ""
    department: str = ""
    author: str = ""
    group: str = ""
    supervisor: str = ""
    city: str = ""
    year: str = ""
    authors: list[Signatory] = Field(default_factory=list)
    reviewers: list[Signatory] = Field(default_factory=list)


class AuthorProfile(Model):
    organization: str = ""
    department: str = ""
    author: str = ""
    group: str = ""
    supervisor: str = ""
    city: str = ""
    year: str = ""


class DraftSource(Model):
    """A source selected in the editor; its absolute path stays in local app data."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = ""
    path: str
    name: str = ""
    role: SourceRole = "context_only"
    reference: str = ""
    reference_verified: bool = False
    confidential: bool = False
    title_metadata: bool = False


class DraftExcludedSource(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    source_id: str = ""
    name: str
    original_path: str = ""
    confidential: bool = False
    role: SourceRole = "context_only"
    reference: str = ""
    reference_verified: bool = False
    title_metadata: bool = False


class WorkspaceDraft(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    workspace_path: str = ""
    pending_changes: bool = False
    auto_title: bool = False
    disk_inputs: dict = Field(default_factory=dict)
    metadata: Metadata = Field(default_factory=Metadata)
    requirements: str = ""
    template_id: str = "report"
    template_snapshot: Template | None = None
    profile_id: str = "balanced"
    profile_snapshot: AgentProfile | None = None
    parent_path: str = ""
    mode: Literal["document", "template"] = "document"
    sources: list[DraftSource] = Field(default_factory=list)
    excluded_sources: list[DraftExcludedSource] = Field(default_factory=list)


class BlockBase(Model):
    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")


class Heading(BlockBase):
    type: Literal["heading"] = "heading"
    text: str = Field(min_length=1)
    level: int = Field(default=1, ge=1, le=3)
    numbered: bool = True


class Paragraph(BlockBase):
    type: Literal["paragraph"] = "paragraph"
    text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class Table(BlockBase):
    type: Literal["table"] = "table"
    caption: str = Field(min_length=1)
    rows: list[list[str]] = Field(min_length=1)
    header: bool = True

    @model_validator(mode="after")
    def rectangular(self):
        if not self.rows[0] or any(len(r) != len(self.rows[0]) for r in self.rows):
            raise ValueError("Таблица должна иметь одинаковое ненулевое количество ячеек в каждой строке")
        if len(self.rows[0]) > 15:
            raise ValueError("Таблица шире 15 столбцов: разбейте её или используйте расширение")
        return self


class Figure(BlockBase):
    type: Literal["image"] = "image"
    path: str
    caption: str = Field(min_length=1)
    width_mm: float = Field(default=130, ge=10, le=450)


class Equation(BlockBase):
    type: Literal["equation"] = "equation"
    text: str = Field(min_length=1)
    number: bool = True


class ListBlock(BlockBase):
    type: Literal["list"] = "list"
    items: list[str] = Field(min_length=1)
    ordered: bool = False


class PageBreak(BlockBase):
    type: Literal["page_break"] = "page_break"


class Requirement(BlockBase):
    type: Literal["requirement"] = "requirement"
    text: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    subsystem: str = ""
    priority: str = ""
    presentation: Literal["paragraph", "table"] = "paragraph"


class Appendix(BlockBase):
    type: Literal["appendix"] = "appendix"
    text: str = Field(min_length=1)


Block = Annotated[
    Heading | Paragraph | Table | Figure | Equation | ListBlock | PageBreak | Appendix | Requirement,
    Field(discriminator="type"),
]


class DocumentContent(Model):
    schema_version: Literal[1] = 1
    metadata: Metadata = Field(default_factory=Metadata)
    blocks: list[Block] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [b.id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("Идентификаторы блоков должны быть уникальными")
        return self


class Workspace(Model):
    schema_version: Literal[1] = 1
    engine_version: str
    name: str
    mode: Literal["document", "template"] = "document"
    sources: list[Source] = Field(default_factory=list)
    excluded_sources: list[ExcludedSource] = Field(default_factory=list)
    requirements: str = ""
    content_review_required: bool = False
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def move_excluded_sources(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        sources = []
        excluded = list(data.get("excluded_sources", []))
        for raw in data.get("sources", []):
            if isinstance(raw, Source):
                if raw._legacy_excluded:
                    excluded.append(
                        ExcludedSource(
                            id=raw.id,
                            name=raw.name,
                            kind="material",
                            confidential=raw.confidential,
                        )
                    )
                else:
                    sources.append(raw)
                continue
            if isinstance(raw, dict) and raw.get("policy") == "exclude":
                old_kind = raw.get("role", "material")
                excluded.append(
                    {
                        "id": raw.get("id", ""),
                        "name": raw.get("name", ""),
                        "kind": old_kind if old_kind in LEGACY_FILE_ROLES else "material",
                        "confidential": raw.get("confidential", False),
                    }
                )
                continue
            sources.append(raw)
        data["sources"] = sources
        data["excluded_sources"] = excluded
        return data


class Finding(Model):
    code: str
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    block_id: str = ""
    page: int | None = None


class CheckReport(Model):
    schema_version: Literal[1] = 1
    status: Literal["blocked", "review", "ready", "preview"] = "blocked"
    fingerprint: str = ""
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    page_count: int = 0
    review_pages: list[int] = Field(default_factory=list)
    reviewed_pages: list[int] = Field(default_factory=list)
    automatic_checks: list[str] = Field(default_factory=list)
    checked_at: str = ""
    technical_status: Literal["passed", "blocked", "not_checked"] = "not_checked"
    content_review: dict = Field(default_factory=dict)


class Basis(Model):
    kind: Literal["user", "source", "assumption", "proposal"]
    locator: str = Field(min_length=1)
    source_id: str = ""


class BaselineItem(Model):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["scope", "feature", "requirement", "decision", "unknown"]
    basis: Basis


class ContentBaseline(Model):
    goal: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    items: list[BaselineItem] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_items(self):
        if (
            not self.goal.strip()
            or not self.scope.strip()
            or any(
                not item.id.strip() or not item.text.strip() or not item.basis.locator.strip()
                for item in self.items
            )
        ):
            raise ValueError("Постановка и основания не могут быть пустыми")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("ID исходной постановки должны быть уникальными")
        return self


class Coverage(Model):
    item_id: str
    status: Literal["covered", "missing", "changed", "unresolved"]
    block_ids: list[str] = Field(default_factory=list)
    note: str = Field(min_length=1)


class ContentAssessment(Model):
    fingerprint: str
    stage: Literal["plan", "requirements", "final"]
    reviewer: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    criteria: dict[str, str]
    coverage: list[Coverage]
    findings: list[Finding] = Field(default_factory=list)
