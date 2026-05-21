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
from app.models import AnnualAttachment, AnnualExecution, AppSetting, Attachment, Project


PROJECT_ATTACHMENT_KINDS = [
    AttachmentKind.PROCUREMENT_BASIS,
    AttachmentKind.CONTRACT_REVIEW,
    AttachmentKind.SIGNED_CONTRACT,
    AttachmentKind.OTHER,
]

ANNUAL_ATTACHMENT_KINDS = [
    AttachmentKind.ACCEPTANCE,
    AttachmentKind.INVOICE,
]


@dataclass(frozen=True)
class ProjectOverview:
    project: Project
    status: ProjectStatus
    budget_delta: BudgetDelta
    attachments: list[Attachment]


@dataclass(frozen=True)
class AnnualProjectOverview:
    project: Project
    execution: AnnualExecution
    status: ProjectStatus
    budget_delta: BudgetDelta
    project_attachments: list[Attachment]
    annual_attachments: list[AnnualAttachment]
    legacy_annual_attachments: list[Attachment]
    attachments: list[Attachment | AnnualAttachment]


@dataclass(frozen=True)
class DashboardSummary:
    year: int
    total_projects: int
    established_count: int
    unsigned_contract_count: int
    unaccepted_count: int
    unpaid_count: int
    renewal_due: list[AnnualProjectOverview]
    projects: list[AnnualProjectOverview]


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


def sync_annual_executions(session: Session, project: Project) -> list[AnnualExecution]:
    target_years = _execution_years(project)
    normal_years = _contract_service_years(project) or {project.year}
    existing = {
        execution.year: execution
        for execution in session.exec(
            select(AnnualExecution).where(AnnualExecution.project_id == project.id)
        )
    }
    stale_executions = [
        execution
        for year, execution in existing.items()
        if year not in target_years
        and execution.contract_only
        and not execution.manual_amounts
    ]
    for execution in stale_executions:
        session.delete(execution)

    normal_count = max(len(target_years & normal_years), 1)
    changed = bool(stale_executions)

    for year in sorted(target_years):
        contract_only = year not in normal_years
        budget_amount = None if contract_only else _split_amount(project.budget_amount, normal_count)
        contract_amount = None if contract_only else _split_amount(project.contract_amount, normal_count)
        execution = existing.get(year)
        if execution is None:
            execution = AnnualExecution(
                project_id=project.id,
                year=year,
                contract_only=contract_only,
                budget_amount=budget_amount,
                contract_amount=contract_amount,
                acceptance_date=(
                    project.acceptance_date
                    if year == project.year and not contract_only
                    else None
                ),
                payment_date=(
                    project.payment_date
                    if year == project.year and not contract_only
                    else None
                ),
            )
            session.add(execution)
            changed = True
            continue

        if execution.contract_only != contract_only:
            execution.contract_only = contract_only
            changed = True
        if not execution.manual_amounts:
            if execution.budget_amount != budget_amount:
                execution.budget_amount = budget_amount
                changed = True
            if execution.contract_amount != contract_amount:
                execution.contract_amount = contract_amount
                changed = True
        if year == project.year and not contract_only:
            if execution.acceptance_date is None and project.acceptance_date is not None:
                execution.acceptance_date = project.acceptance_date
                changed = True
            if execution.payment_date is None and project.payment_date is not None:
                execution.payment_date = project.payment_date
                changed = True
        if changed:
            execution.updated_at = datetime.now()
            session.add(execution)

    if changed:
        session.commit()
    return list(
        session.exec(
            select(AnnualExecution)
            .where(AnnualExecution.project_id == project.id)
            .order_by(AnnualExecution.year)
        )
    )


def update_annual_execution(
    session: Session,
    execution_id: int,
    **changes: Any,
) -> AnnualExecution:
    execution = session.get(AnnualExecution, execution_id)
    if execution is None:
        raise ValueError("年度执行记录不存在")
    amount_fields = {"budget_amount", "contract_amount", "contract_only"}
    for key, value in changes.items():
        if hasattr(execution, key):
            setattr(execution, key, value)
            if key in amount_fields:
                execution.manual_amounts = True
    execution.updated_at = datetime.now()
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return execution


def save_annual_attachment_bytes(
    session: Session,
    *,
    execution: AnnualExecution,
    data_dir: Path,
    kind: AttachmentKind,
    original_filename: str,
    content: bytes,
) -> AnnualAttachment:
    if kind not in ANNUAL_ATTACHMENT_KINDS:
        raise ValueError("年度附件只支持验收单和发票")
    if not original_filename.lower().endswith(".pdf") or not content.startswith(b"%PDF"):
        raise ValueError("只支持上传 PDF 文件")
    if execution.id is None:
        raise ValueError("年度执行记录尚未保存，不能上传附件")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stored_name = attachment_storage_name(original_filename, kind, timestamp)
    relative_dir = (
        Path("annual_attachments")
        / str(execution.year)
        / str(execution.project_id)
        / str(execution.id)
    )
    absolute_dir = data_dir / relative_dir
    absolute_dir.mkdir(parents=True, exist_ok=True)
    relative_path = relative_dir / stored_name
    (data_dir / relative_path).write_bytes(content)

    attachment = AnnualAttachment(
        execution_id=execution.id,
        kind=kind,
        original_filename=original_filename,
        stored_path=relative_path.as_posix(),
    )
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def annual_project_overview(
    session: Session,
    execution: AnnualExecution,
) -> AnnualProjectOverview:
    project = session.get(Project, execution.project_id)
    if project is None:
        raise ValueError("项目不存在")
    project_attachments = list(
        session.exec(
            select(Attachment)
            .where(Attachment.project_id == project.id)
            .order_by(Attachment.uploaded_at.desc())
        )
    )
    annual_attachments = list(
        session.exec(
            select(AnnualAttachment)
            .where(AnnualAttachment.execution_id == execution.id)
            .order_by(AnnualAttachment.uploaded_at.desc())
        )
    )
    legacy_annual_attachments = _legacy_annual_attachments(
        session,
        project,
        execution,
        project_attachments,
    )
    annual_kinds = [attachment.kind for attachment in annual_attachments]
    annual_kinds.extend(attachment.kind for attachment in legacy_annual_attachments)
    status = _compute_annual_status(
        project,
        execution,
        [attachment.kind for attachment in project_attachments],
        annual_kinds,
    )
    budget_delta = (
        BudgetDelta(difference=None, is_over_budget=False)
        if execution.contract_only
        else contract_budget_delta(execution.budget_amount, execution.contract_amount)
    )
    row_project_attachments = [
        attachment
        for attachment in project_attachments
        if attachment.kind in PROJECT_ATTACHMENT_KINDS
    ]
    return AnnualProjectOverview(
        project=project,
        execution=execution,
        status=status,
        budget_delta=budget_delta,
        project_attachments=row_project_attachments,
        annual_attachments=annual_attachments,
        legacy_annual_attachments=legacy_annual_attachments,
        attachments=[
            *row_project_attachments,
            *annual_attachments,
            *legacy_annual_attachments,
        ],
    )


def list_annual_project_overviews(
    session: Session,
    year: int | None = None,
) -> list[AnnualProjectOverview]:
    projects = list(
        session.exec(select(Project).order_by(Project.year.desc(), Project.id.desc()))
    )
    for project in projects:
        sync_annual_executions(session, project)
    statement = select(AnnualExecution).order_by(
        AnnualExecution.year.desc(),
        AnnualExecution.project_id.desc(),
    )
    if year is not None:
        statement = (
            select(AnnualExecution)
            .where(AnnualExecution.year == year)
            .order_by(AnnualExecution.project_id.desc())
        )
    return [
        annual_project_overview(session, execution)
        for execution in session.exec(statement)
    ]


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
        budget_total=sum(_overview_budget_amount(item) or 0 for item in overviews),
        contract_total=sum(_overview_contract_amount(item) or 0 for item in overviews),
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
    projects = list_annual_project_overviews(session, year=year)
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
        unaccepted_count=sum(
            1
            for item in projects
            if not item.execution.contract_only and not item.status.accepted
        ),
        unpaid_count=sum(
            1
            for item in projects
            if not item.execution.contract_only and not item.status.paid
        ),
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
    searchable = [project.name, project.notes]
    if hasattr(item, "execution"):
        searchable.append(item.execution.notes)
    return any(keyword in (value or "").lower() for value in searchable)


def _matches_status_filter(item: ProjectOverview, status_filter: str) -> bool:
    if status_filter == "unsigned_contract":
        return not item.status.contract_signed
    if status_filter == "unaccepted":
        return not _is_contract_only(item) and not item.status.accepted
    if status_filter == "unpaid":
        return not _is_contract_only(item) and not item.status.paid
    if status_filter == "incomplete":
        return bool(item.status.missing_evidence)
    if status_filter == "completed":
        return (
            item.status.established
            and item.status.contract_signed
            and (_is_contract_only(item) or item.status.accepted)
            and (_is_contract_only(item) or item.status.paid)
            and not item.status.missing_evidence
        )
    return True


def _is_contract_only(item: ProjectOverview | AnnualProjectOverview) -> bool:
    return bool(getattr(getattr(item, "execution", None), "contract_only", False))


def _overview_budget_amount(item: ProjectOverview | AnnualProjectOverview) -> float | None:
    if hasattr(item, "execution"):
        return None if item.execution.contract_only else item.execution.budget_amount
    return item.project.budget_amount


def _overview_contract_amount(item: ProjectOverview | AnnualProjectOverview) -> float | None:
    if hasattr(item, "execution"):
        return None if item.execution.contract_only else item.execution.contract_amount
    return item.project.contract_amount


def _execution_years(project: Project) -> set[int]:
    service_years = _contract_service_years(project)
    if not service_years:
        return {project.year}
    years = set(service_years)
    if project.year < min(service_years):
        years.add(project.year)
    return years


def _contract_service_years(project: Project) -> set[int]:
    if not project.contract_start or not project.contract_end:
        return set()
    if project.contract_start > project.contract_end:
        return set()
    return set(range(project.contract_start.year, project.contract_end.year + 1))


def _split_amount(value: float | None, count: int) -> float | None:
    if value is None:
        return None
    return round(value / count, 2)


def _legacy_annual_attachments(
    session: Session,
    project: Project,
    execution: AnnualExecution,
    project_attachments: list[Attachment],
) -> list[Attachment]:
    first_execution = session.exec(
        select(AnnualExecution)
        .where(AnnualExecution.project_id == project.id)
        .order_by(AnnualExecution.year)
    ).first()
    if first_execution is None or first_execution.id != execution.id:
        return []
    return [
        attachment
        for attachment in project_attachments
        if attachment.kind in ANNUAL_ATTACHMENT_KINDS
    ]


def _compute_annual_status(
    project: Project,
    execution: AnnualExecution,
    project_attachment_kinds: list[AttachmentKind],
    annual_attachment_kinds: list[AttachmentKind],
) -> ProjectStatus:
    project_evidence = set(project_attachment_kinds)
    annual_evidence = set(annual_attachment_kinds)
    established = bool(execution.budget_amount or project.budget_amount or execution.contract_only)
    contract_signed = bool(
        project.contract_amount
        and project.contract_start
        and project.contract_end
        and AttachmentKind.SIGNED_CONTRACT in project_evidence
    )

    if execution.contract_only:
        missing = []
        if not contract_signed:
            missing.append(AttachmentKind.SIGNED_CONTRACT.label)
        return ProjectStatus(
            established=established,
            contract_signed=contract_signed,
            accepted=True,
            paid=True,
            missing_evidence=missing,
        )

    has_acceptance = AttachmentKind.ACCEPTANCE in annual_evidence
    has_invoice = AttachmentKind.INVOICE in annual_evidence
    accepted = bool(execution.acceptance_date and has_acceptance)
    paid = bool(execution.payment_date and has_invoice)

    missing: list[str] = []
    if not contract_signed:
        missing.append(AttachmentKind.SIGNED_CONTRACT.label)
    if not execution.acceptance_date:
        missing.append("验收日期")
    if not has_acceptance:
        missing.append(AttachmentKind.ACCEPTANCE.label)
    if not execution.payment_date:
        missing.append("付款日期")
    if not has_invoice:
        missing.append(AttachmentKind.INVOICE.label)

    return ProjectStatus(
        established=established,
        contract_signed=contract_signed,
        accepted=accepted,
        paid=paid,
        missing_evidence=missing,
    )
