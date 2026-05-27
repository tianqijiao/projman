from datetime import date, datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.domain import AttachmentKind


def default_money() -> None:
    return None


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    year: int = Field(index=True)
    name: str = Field(index=True)
    budget_amount: float | None = default_money()
    contract_amount: float | None = default_money()
    contract_start: date | None = None
    contract_end: date | None = Field(default=None, index=True)
    acceptance_date: date | None = None
    payment_date: date | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Attachment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    kind: AttachmentKind = Field(index=True)
    original_filename: str
    stored_path: str
    uploaded_at: datetime = Field(default_factory=datetime.now)


class AnnualExecution(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("project_id", "year", name="uq_annual_execution_project_year"),
    )

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    year: int = Field(index=True)
    budget_amount: float | None = default_money()
    contract_amount: float | None = default_money()
    contract_only: bool = Field(default=False, index=True)
    acceptance_date: date | None = None
    payment_date: date | None = None
    notes: str = ""
    manual_amounts: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class AnnualAttachment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    execution_id: int = Field(foreign_key="annualexecution.id", index=True)
    kind: AttachmentKind = Field(index=True)
    original_filename: str
    stored_path: str
    uploaded_at: datetime = Field(default_factory=datetime.now)


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class AiProjectDraft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    status: str = Field(default="pending", index=True)
    input_kind: str = Field(default="", index=True)
    input_filename: str = ""
    input_content_type: str = ""
    stored_input_path: str = ""
    input_text_summary: str = Field(default="", sa_column=Column(Text))
    raw_response_json: str = Field(default="{}", sa_column=Column(Text))
    fields_json: str = Field(default="[]", sa_column=Column(Text))
    risk_tips_json: str = Field(default="[]", sa_column=Column(Text))
    has_validation_errors: bool = Field(default=False, index=True)
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
