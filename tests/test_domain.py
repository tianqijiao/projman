from datetime import date

from app.domain import (
    AttachmentKind,
    ProjectDraft,
    attachment_storage_name,
    compute_project_status,
    contract_budget_delta,
    is_renewal_due,
)


def test_budgeted_project_is_established_only():
    project = ProjectDraft(year=2027, name="行政电脑维护服务", budget_amount=50000)

    status = compute_project_status(project, attachment_kinds=[])

    assert status.established is True
    assert status.contract_signed is False
    assert status.accepted is False
    assert status.paid is False
    assert status.missing_evidence == ["盖章合同扫描件", "验收单", "发票"]


def test_contract_signed_requires_dates_amount_and_signed_contract_scan():
    project = ProjectDraft(
        year=2027,
        name="行政电脑维护服务",
        budget_amount=50000,
        contract_amount=48000,
        contract_start=date(2027, 1, 1),
        contract_end=date(2027, 12, 31),
    )

    status = compute_project_status(
        project,
        attachment_kinds=[AttachmentKind.SIGNED_CONTRACT],
    )

    assert status.contract_signed is True
    assert status.accepted is False
    assert status.paid is False


def test_acceptance_and_payment_can_be_proven_by_dates_or_pdf_evidence():
    project = ProjectDraft(
        year=2027,
        name="行政电脑维护服务",
        budget_amount=50000,
        contract_amount=48000,
        contract_start=date(2027, 1, 1),
        contract_end=date(2027, 12, 31),
        payment_date=date(2028, 1, 15),
    )

    status = compute_project_status(
        project,
        attachment_kinds=[
            AttachmentKind.SIGNED_CONTRACT,
            AttachmentKind.ACCEPTANCE,
            AttachmentKind.INVOICE,
        ],
    )

    assert status.contract_signed is True
    assert status.accepted is True
    assert status.paid is True
    assert status.missing_evidence == []


def test_renewal_reminder_starts_sixty_days_before_contract_end():
    project = ProjectDraft(
        year=2027,
        name="行政电脑维护服务",
        contract_start=date(2027, 1, 1),
        contract_end=date(2027, 12, 31),
    )

    assert is_renewal_due(project, today=date(2027, 11, 1), lead_days=60) is True
    assert is_renewal_due(project, today=date(2027, 10, 31), lead_days=60) is False
    assert is_renewal_due(project, today=date(2028, 1, 1), lead_days=60) is True


def test_budget_delta_marks_contract_over_budget_without_blocking_save():
    delta = contract_budget_delta(budget_amount=50000, contract_amount=52000)

    assert delta.difference == -2000
    assert delta.is_over_budget is True


def test_attachment_storage_name_keeps_pdf_extension_and_avoids_raw_path_use():
    stored = attachment_storage_name(
        original_filename="合同扫描件.pdf",
        kind=AttachmentKind.SIGNED_CONTRACT,
        timestamp="20270521-091500",
    )

    assert stored == "20270521-091500-signed_contract-合同扫描件.pdf"
