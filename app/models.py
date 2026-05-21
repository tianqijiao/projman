from datetime import date, datetime

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


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str
