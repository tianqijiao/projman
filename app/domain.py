from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path


class AttachmentKind(StrEnum):
    PROCUREMENT_BASIS = "procurement_basis"
    CONTRACT_REVIEW = "contract_review"
    SIGNED_CONTRACT = "signed_contract"
    ACCEPTANCE = "acceptance"
    INVOICE = "invoice"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            AttachmentKind.PROCUREMENT_BASIS: "采购依据",
            AttachmentKind.CONTRACT_REVIEW: "合同审签 PDF",
            AttachmentKind.SIGNED_CONTRACT: "盖章合同扫描件",
            AttachmentKind.ACCEPTANCE: "验收单",
            AttachmentKind.INVOICE: "发票",
            AttachmentKind.OTHER: "其他附件",
        }[self]


@dataclass(frozen=True)
class ProjectDraft:
    year: int
    name: str
    budget_amount: float | None = None
    contract_amount: float | None = None
    contract_start: date | None = None
    contract_end: date | None = None
    acceptance_date: date | None = None
    payment_date: date | None = None


@dataclass(frozen=True)
class ProjectStatus:
    established: bool
    contract_signed: bool
    accepted: bool
    paid: bool
    missing_evidence: list[str]


@dataclass(frozen=True)
class BudgetDelta:
    difference: float | None
    is_over_budget: bool


def compute_project_status(
    project: ProjectDraft,
    attachment_kinds: list[AttachmentKind],
) -> ProjectStatus:
    evidence = set(attachment_kinds)
    established = bool(project.budget_amount)
    has_signed_contract = AttachmentKind.SIGNED_CONTRACT in evidence
    contract_signed = bool(
        project.contract_amount
        and project.contract_start
        and project.contract_end
        and has_signed_contract
    )
    has_acceptance = AttachmentKind.ACCEPTANCE in evidence
    has_invoice = AttachmentKind.INVOICE in evidence
    accepted = bool(project.acceptance_date and has_acceptance)
    paid = bool(project.payment_date and has_invoice)

    missing: list[str] = []
    if not has_signed_contract:
        missing.append(AttachmentKind.SIGNED_CONTRACT.label)
    if not project.acceptance_date:
        missing.append("验收日期")
    if not has_acceptance:
        missing.append(AttachmentKind.ACCEPTANCE.label)
    if not project.payment_date:
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


def is_renewal_due(project: ProjectDraft, today: date, lead_days: int) -> bool:
    if not project.contract_end:
        return False
    days_until_end = (project.contract_end - today).days
    return days_until_end <= lead_days


def contract_budget_delta(
    budget_amount: float | None,
    contract_amount: float | None,
) -> BudgetDelta:
    if budget_amount is None or contract_amount is None:
        return BudgetDelta(difference=None, is_over_budget=False)
    difference = budget_amount - contract_amount
    return BudgetDelta(difference=difference, is_over_budget=difference < 0)


def attachment_storage_name(
    original_filename: str,
    kind: AttachmentKind,
    timestamp: str,
) -> str:
    safe_name = Path(original_filename).name.replace("/", "_").replace("\\", "_")
    return f"{timestamp}-{kind.value}-{safe_name}"
