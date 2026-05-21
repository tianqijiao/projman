from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.domain import (
    AttachmentKind,
    BudgetDelta,
    ProjectDraft,
    ProjectStatus,
    attachment_storage_name,
    compute_project_status,
    contract_budget_delta,
    is_renewal_due,
)
from app.models import AppSetting, Attachment, Project


@dataclass(frozen=True)
class ProjectOverview:
    project: Project
    status: ProjectStatus
    budget_delta: BudgetDelta
    attachments: list[Attachment]


@dataclass(frozen=True)
class DashboardSummary:
    year: int
    total_projects: int
    established_count: int
    unsigned_contract_count: int
    unaccepted_count: int
    unpaid_count: int
    renewal_due: list[ProjectOverview]
    projects: list[ProjectOverview]


@dataclass(frozen=True)
class ProjectLedgerSummary:
    total_count: int
    budget_total: float
    contract_total: float
    incomplete_count: int


STATUS_FILTER_OPTIONS = [
    ("", "全部状态"),
    ("unsigned_contract", "未签合同"),
    ("unaccepted", "未验收"),
    ("unpaid", "未付款"),
    ("incomplete", "资料不完整"),
    ("completed", "已完成"),
]


def create_project(
    session: Session,
    *,
    year: int,
    name: str,
    budget_amount: float | None = None,
    notes: str = "",
) -> Project:
    project = Project(
        year=year,
        name=name.strip(),
        budget_amount=budget_amount,
        notes=notes.strip(),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def update_project(session: Session, project_id: int, **changes: Any) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("项目不存在")
    for key, value in changes.items():
        if hasattr(project, key):
            setattr(project, key, value)
    project.updated_at = datetime.now()
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def save_attachment_bytes(
    session: Session,
    *,
    project: Project,
    data_dir: Path,
    kind: AttachmentKind,
    original_filename: str,
    content: bytes,
) -> Attachment:
    if not original_filename.lower().endswith(".pdf") or not content.startswith(b"%PDF"):
        raise ValueError("只支持上传 PDF 文件")
    if project.id is None:
        raise ValueError("项目尚未保存，不能上传附件")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stored_name = attachment_storage_name(original_filename, kind, timestamp)
    relative_dir = Path("attachments") / str(project.year) / str(project.id)
    absolute_dir = data_dir / relative_dir
    absolute_dir.mkdir(parents=True, exist_ok=True)
    relative_path = relative_dir / stored_name
    (data_dir / relative_path).write_bytes(content)

    attachment = Attachment(
        project_id=project.id,
        kind=kind,
        original_filename=original_filename,
        stored_path=relative_path.as_posix(),
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def project_overview(session: Session, project: Project) -> ProjectOverview:
    attachments = list(
        session.exec(
            select(Attachment)
            .where(Attachment.project_id == project.id)
            .order_by(Attachment.uploaded_at.desc())
        )
    )
    status = compute_project_status(
        _draft_from_project(project),
        [attachment.kind for attachment in attachments],
    )
    delta = contract_budget_delta(project.budget_amount, project.contract_amount)
    return ProjectOverview(
        project=project,
        status=status,
        budget_delta=delta,
        attachments=attachments,
    )


def list_project_overviews(session: Session, year: int | None = None) -> list[ProjectOverview]:
    statement = select(Project).order_by(Project.year.desc(), Project.id.desc())
    if year is not None:
        statement = select(Project).where(Project.year == year).order_by(Project.id.desc())
    return [project_overview(session, project) for project in session.exec(statement)]


def filter_project_overviews(
    overviews: list[ProjectOverview],
    *,
    query: str | None = None,
    status_filter: str | None = None,
) -> list[ProjectOverview]:
    keyword = (query or "").strip().lower()
    normalized_status = normalize_status_filter(status_filter)

    return [
        item
        for item in overviews
        if _matches_keyword(item, keyword)
        and _matches_status_filter(item, normalized_status)
    ]


def summarize_project_overviews(overviews: list[ProjectOverview]) -> ProjectLedgerSummary:
    return ProjectLedgerSummary(
        total_count=len(overviews),
        budget_total=sum(item.project.budget_amount or 0 for item in overviews),
        contract_total=sum(item.project.contract_amount or 0 for item in overviews),
        incomplete_count=sum(1 for item in overviews if item.status.missing_evidence),
    )


def normalize_status_filter(value: str | None) -> str:
    allowed = {key for key, _label in STATUS_FILTER_OPTIONS}
    status_filter = (value or "").strip()
    return status_filter if status_filter in allowed else ""


def format_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def dashboard_summary(
    session: Session,
    *,
    year: int,
    today: date,
    renewal_lead_days: int,
) -> DashboardSummary:
    projects = list_project_overviews(session, year=year)
    renewal_due = [
        item
        for item in projects
        if is_renewal_due(_draft_from_project(item.project), today, renewal_lead_days)
    ]
    return DashboardSummary(
        year=year,
        total_projects=len(projects),
        established_count=sum(1 for item in projects if item.status.established),
        unsigned_contract_count=sum(1 for item in projects if not item.status.contract_signed),
        unaccepted_count=sum(1 for item in projects if not item.status.accepted),
        unpaid_count=sum(1 for item in projects if not item.status.paid),
        renewal_due=renewal_due,
        projects=projects,
    )


def get_int_setting(session: Session, key: str, default: int) -> int:
    setting = session.get(AppSetting, key)
    if setting is None:
        return default
    try:
        return int(setting.value)
    except ValueError:
        return default


def set_int_setting(session: Session, key: str, value: int) -> AppSetting:
    setting = session.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=str(value))
    else:
        setting.value = str(value)
    session.add(setting)
    session.commit()
    session.refresh(setting)
    return setting


def _draft_from_project(project: Project) -> ProjectDraft:
    return ProjectDraft(
        year=project.year,
        name=project.name,
        budget_amount=project.budget_amount,
        contract_amount=project.contract_amount,
        contract_start=project.contract_start,
        contract_end=project.contract_end,
        acceptance_date=project.acceptance_date,
        payment_date=project.payment_date,
    )


def _matches_keyword(item: ProjectOverview, keyword: str) -> bool:
    if not keyword:
        return True
    project = item.project
    return keyword in project.name.lower() or keyword in project.notes.lower()


def _matches_status_filter(item: ProjectOverview, status_filter: str) -> bool:
    if status_filter == "unsigned_contract":
        return not item.status.contract_signed
    if status_filter == "unaccepted":
        return not item.status.accepted
    if status_filter == "unpaid":
        return not item.status.paid
    if status_filter == "incomplete":
        return bool(item.status.missing_evidence)
    if status_filter == "completed":
        return (
            item.status.established
            and item.status.contract_signed
            and item.status.accepted
            and item.status.paid
            and not item.status.missing_evidence
        )
    return True
