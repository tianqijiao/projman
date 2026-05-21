from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.domain import AttachmentKind
from app.models import Attachment, Project
from app.services import (
    DashboardSummary,
    create_project,
    dashboard_summary,
    get_int_setting,
    save_attachment_bytes,
    set_int_setting,
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


def test_int_setting_returns_default_then_persists_update(engine):
    with Session(engine) as session:
        assert get_int_setting(session, "renewal_lead_days", default=60) == 60

        set_int_setting(session, "renewal_lead_days", 90)

        assert get_int_setting(session, "renewal_lead_days", default=60) == 90
