from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.domain import AttachmentKind
from app.models import AnnualAttachment, AnnualExecution, Attachment, Project
from app.services import (
    AnnualProjectOverview,
    DashboardSummary,
    create_project,
    dashboard_summary,
    get_int_setting,
    list_annual_project_overviews,
    save_annual_attachment_bytes,
    save_attachment_bytes,
    set_int_setting,
    sync_annual_executions,
    update_annual_execution,
    update_project,
)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_create_and_update_project_persists_fields(engine):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2027,
            name="行政电脑维护服务",
            budget_amount=50000,
            notes="年度预算项目",
        )

        updated = update_project(
            session,
            project.id,
            contract_amount=48000,
            contract_start=date(2027, 1, 1),
            contract_end=date(2027, 12, 31),
        )

        assert updated.name == "行政电脑维护服务"
        assert updated.budget_amount == 50000
        assert updated.contract_amount == 48000
        assert updated.contract_end == date(2027, 12, 31)


def test_save_attachment_copies_pdf_under_project_directory(engine, tmp_path):
    with Session(engine) as session:
        project = create_project(session, year=2027, name="行政电脑维护服务")

        attachment = save_attachment_bytes(
            session,
            project=project,
            data_dir=tmp_path,
            kind=AttachmentKind.SIGNED_CONTRACT,
            original_filename="盖章合同.pdf",
            content=b"%PDF-1.7 fake",
        )

        stored_file = tmp_path / attachment.stored_path
        saved = session.exec(select(Attachment)).one()
        assert stored_file.exists()
        assert stored_file.read_bytes() == b"%PDF-1.7 fake"
        assert saved.original_filename == "盖章合同.pdf"
        assert saved.kind == AttachmentKind.SIGNED_CONTRACT


def test_save_attachment_rejects_non_pdf(engine, tmp_path):
    with Session(engine) as session:
        project = create_project(session, year=2027, name="行政电脑维护服务")

        with pytest.raises(ValueError, match="PDF"):
            save_attachment_bytes(
                session,
                project=project,
                data_dir=tmp_path,
                kind=AttachmentKind.INVOICE,
                original_filename="发票.docx",
                content=b"not a pdf",
            )


def test_dashboard_summary_counts_open_work_and_renewal_alerts(engine, tmp_path):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2027,
            name="行政电脑维护服务",
            budget_amount=50000,
        )
        update_project(
            session,
            project.id,
            contract_amount=48000,
            contract_start=date(2027, 1, 1),
            contract_end=date(2027, 12, 31),
        )
        save_attachment_bytes(
            session,
            project=project,
            data_dir=tmp_path,
            kind=AttachmentKind.SIGNED_CONTRACT,
            original_filename="合同.pdf",
            content=b"%PDF-1.7 fake",
        )
        create_project(
            session,
            year=2027,
            name="机房维保服务",
            budget_amount=100000,
        )

        summary = dashboard_summary(
            session,
            year=2027,
            today=date(2027, 11, 1),
            renewal_lead_days=60,
        )

        assert isinstance(summary, DashboardSummary)
        assert summary.total_projects == 2
        assert summary.established_count == 2
        assert summary.unsigned_contract_count == 1
        assert summary.unaccepted_count == 2
        assert summary.unpaid_count == 2
        assert [item.project.name for item in summary.renewal_due] == ["行政电脑维护服务"]


def test_sync_annual_executions_expands_cross_year_project_and_preserves_manual_amounts(engine):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2026,
            name="三年云平台服务",
            budget_amount=300000,
        )
        update_project(
            session,
            project.id,
            contract_amount=270000,
            contract_start=date(2027, 1, 1),
            contract_end=date(2029, 12, 31),
        )

        executions = sync_annual_executions(session, project)

        assert [(item.year, item.contract_only) for item in executions] == [
            (2026, True),
            (2027, False),
            (2028, False),
            (2029, False),
        ]
        assert [item.budget_amount for item in executions] == [None, 100000, 100000, 100000]
        assert [item.contract_amount for item in executions] == [None, 90000, 90000, 90000]

        update_annual_execution(
            session,
            executions[2].id,
            budget_amount=120000,
            contract_amount=95000,
            contract_only=False,
        )
        update_project(session, project.id, contract_amount=300000, budget_amount=330000)

        refreshed = sync_annual_executions(session, project)

        assert refreshed[2].year == 2028
        assert refreshed[2].budget_amount == 120000
        assert refreshed[2].contract_amount == 95000


def test_sync_annual_executions_does_not_create_post_contract_management_year(engine):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2028,
            name="先前误标合同管理年的项目",
            budget_amount=120000,
        )
        update_project(
            session,
            project.id,
            contract_amount=120000,
            contract_start=date(2026, 5, 22),
            contract_end=date(2027, 5, 21),
        )
        stale = AnnualExecution(
            project_id=project.id,
            year=2028,
            contract_only=True,
            manual_amounts=False,
        )
        session.add(stale)
        session.commit()

        executions = sync_annual_executions(session, project)

        assert [(item.year, item.contract_only) for item in executions] == [
            (2026, False),
            (2027, False),
        ]
        assert session.get(AnnualExecution, stale.id) is None


def test_cross_year_contract_first_service_year_is_payable_execution_year(engine):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2026,
            name="三年服务项目",
            budget_amount=300000,
        )
        update_project(
            session,
            project.id,
            contract_amount=270000,
            contract_start=date(2026, 1, 1),
            contract_end=date(2028, 12, 31),
        )

        executions = sync_annual_executions(session, project)

        assert [(item.year, item.contract_only) for item in executions] == [
            (2026, False),
            (2027, False),
            (2028, False),
        ]
        assert [item.budget_amount for item in executions] == [100000, 100000, 100000]
        assert [item.contract_amount for item in executions] == [90000, 90000, 90000]


def test_annual_status_uses_year_specific_acceptance_and_payment(engine, tmp_path):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2027,
            name="年度安全服务",
            budget_amount=60000,
        )
        update_project(
            session,
            project.id,
            contract_amount=60000,
            contract_start=date(2027, 1, 1),
            contract_end=date(2028, 12, 31),
        )
        save_attachment_bytes(
            session,
            project=project,
            data_dir=tmp_path,
            kind=AttachmentKind.SIGNED_CONTRACT,
            original_filename="盖章合同.pdf",
            content=b"%PDF-1.7 fake",
        )
        executions = sync_annual_executions(session, project)
        first_year = next(item for item in executions if item.year == 2027)
        second_year = next(item for item in executions if item.year == 2028)

        update_annual_execution(
            session,
            first_year.id,
            acceptance_date=date(2027, 12, 31),
            payment_date=date(2028, 1, 15),
        )
        save_annual_attachment_bytes(
            session,
            execution=first_year,
            data_dir=tmp_path,
            kind=AttachmentKind.ACCEPTANCE,
            original_filename="2027验收单.pdf",
            content=b"%PDF-1.7 fake",
        )
        save_annual_attachment_bytes(
            session,
            execution=first_year,
            data_dir=tmp_path,
            kind=AttachmentKind.INVOICE,
            original_filename="2027发票.pdf",
            content=b"%PDF-1.7 fake",
        )

        overviews = list_annual_project_overviews(session, year=2027)
        next_overviews = list_annual_project_overviews(session, year=2028)

        assert isinstance(overviews[0], AnnualProjectOverview)
        assert overviews[0].execution.id == first_year.id
        assert overviews[0].status.accepted
        assert overviews[0].status.paid
        assert next_overviews[0].execution.id == second_year.id
        assert not next_overviews[0].status.accepted
        assert not next_overviews[0].status.paid


def test_legacy_project_acceptance_and_invoice_count_for_first_execution(engine, tmp_path):
    with Session(engine) as session:
        project = create_project(
            session,
            year=2027,
            name="旧数据项目",
            budget_amount=50000,
        )
        update_project(
            session,
            project.id,
            contract_amount=50000,
            contract_start=date(2027, 1, 1),
            contract_end=date(2027, 12, 31),
            acceptance_date=date(2027, 12, 31),
            payment_date=date(2028, 1, 15),
        )
        for kind, filename in [
            (AttachmentKind.SIGNED_CONTRACT, "盖章合同.pdf"),
            (AttachmentKind.ACCEPTANCE, "旧验收单.pdf"),
            (AttachmentKind.INVOICE, "旧发票.pdf"),
        ]:
            save_attachment_bytes(
                session,
                project=project,
                data_dir=tmp_path,
                kind=kind,
                original_filename=filename,
                content=b"%PDF-1.7 fake",
            )

        overview = list_annual_project_overviews(session, year=2027)[0]

        assert overview.status.accepted
        assert overview.status.paid
        assert session.exec(select(AnnualExecution)).one().year == 2027
        assert session.exec(select(AnnualAttachment)).all() == []


def test_int_setting_returns_default_then_persists_update(engine):
    with Session(engine) as session:
        assert get_int_setting(session, "renewal_lead_days", default=60) == 60

        set_int_setting(session, "renewal_lead_days", 90)

        assert get_int_setting(session, "renewal_lead_days", default=60) == 90
