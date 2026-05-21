from datetime import date
from io import BytesIO
import os
from pathlib import Path
from typing import Annotated
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, SQLModel, create_engine
from starlette.middleware.sessions import SessionMiddleware

from app.domain import AttachmentKind
from app.exporters import build_projects_excel
from app.models import Attachment, Project
from app.services import (
    create_project,
    dashboard_summary,
    get_int_setting,
    list_project_overviews,
    project_overview,
    save_attachment_bytes,
    set_int_setting,
    update_project,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def create_app(
    *,
    database_url: str = "sqlite:///data/app.db",
    data_dir: Path | str = Path("data"),
    username: str = "admin",
    password: str = "admin",
    secret_key: str = "change-me-in-env",
) -> FastAPI:
    app = FastAPI(title="运维项目管理")
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    app.state.engine = engine
    app.state.data_dir = data_path
    app.state.username = username
    app.state.password = password
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def get_session() -> Session:
        with Session(app.state.engine) as session:
            yield session

    def login_redirect(request: Request) -> RedirectResponse | None:
        if request.session.get("user") != app.state.username:
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/login")
    def login_page(request: Request):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": None},
        )

    @app.post("/login")
    def login(
        request: Request,
        form_username: Annotated[str, Form(alias="username")],
        form_password: Annotated[str, Form(alias="password")],
    ):
        if form_username == app.state.username and form_password == app.state.password:
            request.session["user"] = form_username
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "账号或密码不正确"},
            status_code=401,
        )

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    def dashboard(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        year: int | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        selected_year = year or date.today().year
        renewal_lead_days = get_int_setting(session, "renewal_lead_days", default=60)
        summary = dashboard_summary(
            session,
            year=selected_year,
            today=date.today(),
            renewal_lead_days=renewal_lead_days,
        )
        years = _available_years(session, selected_year)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": summary,
                "years": years,
                "selected_year": selected_year,
                "renewal_lead_days": renewal_lead_days,
            },
        )

    @app.get("/settings")
    def settings_page(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "renewal_lead_days": get_int_setting(
                    session, "renewal_lead_days", default=60
                ),
            },
        )

    @app.post("/settings")
    def save_settings(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        renewal_lead_days: Annotated[int, Form()],
    ):
        if redirect := login_redirect(request):
            return redirect
        set_int_setting(session, "renewal_lead_days", renewal_lead_days)
        return RedirectResponse("/settings", status_code=303)

    @app.get("/projects")
    def projects(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        year: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        selected_year = _parse_optional_year(year)
        overviews = list_project_overviews(session, year=selected_year)
        return templates.TemplateResponse(
            request,
            "projects.html",
            {
                "overviews": overviews,
                "year": selected_year,
                "years": _project_years(session),
                "attachment_kinds": list(AttachmentKind),
            },
        )

    @app.get("/projects/export")
    def export_projects(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        year: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        selected_year = _parse_optional_year(year)
        overviews = list_project_overviews(session, year=selected_year)
        title = (
            f"{selected_year} 年度运维项目台账"
            if selected_year
            else "全部年度运维项目台账"
        )
        filename = f"projects-{selected_year}.xlsx" if selected_year else "projects-all.xlsx"
        content = build_projects_excel(overviews, title)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/projects/{project_id}/attachments/download-all")
    def download_project_attachments(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        project = session.get(Project, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        attachments = project_overview(session, project).attachments
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
            for attachment in attachments:
                path = app.state.data_dir / attachment.stored_path
                if path.exists():
                    zip_file.write(
                        path,
                        arcname=f"{attachment.kind.label}/{attachment.original_filename}",
                    )
        archive.seek(0)
        return Response(
            archive.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="project-{project_id}-attachments.zip"'
                )
            },
        )

    @app.get("/projects/new")
    def new_project(request: Request):
        if redirect := login_redirect(request):
            return redirect
        return templates.TemplateResponse(
            request,
            "project_form.html",
            {
                "project": None,
                "today_year": date.today().year,
            },
        )

    @app.post("/projects")
    def create_project_route(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        year: Annotated[int, Form()],
        name: Annotated[str, Form()],
        budget_amount: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        create_project(
            session,
            year=year,
            name=name,
            budget_amount=_parse_float(budget_amount),
            notes=notes,
        )
        return RedirectResponse(f"/?year={year}", status_code=303)

    @app.get("/projects/{project_id}")
    def project_detail(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        project = session.get(Project, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            {
                "overview": project_overview(session, project),
                "attachment_kinds": list(AttachmentKind),
            },
        )

    @app.post("/projects/{project_id}/edit")
    def edit_project(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
        year: Annotated[int, Form()],
        name: Annotated[str, Form()],
        budget_amount: Annotated[str, Form()] = "",
        contract_amount: Annotated[str, Form()] = "",
        contract_start: Annotated[str, Form()] = "",
        contract_end: Annotated[str, Form()] = "",
        acceptance_date: Annotated[str, Form()] = "",
        payment_date: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        update_project(
            session,
            project_id,
            year=year,
            name=name.strip(),
            budget_amount=_parse_float(budget_amount),
            contract_amount=_parse_float(contract_amount),
            contract_start=_parse_date(contract_start),
            contract_end=_parse_date(contract_end),
            acceptance_date=_parse_date(acceptance_date),
            payment_date=_parse_date(payment_date),
            notes=notes.strip(),
        )
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/attachments")
    async def upload_attachment(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ):
        if redirect := login_redirect(request):
            return redirect
        project = session.get(Project, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        content = await file.read()
        save_attachment_bytes(
            session,
            project=project,
            data_dir=app.state.data_dir,
            kind=AttachmentKind(kind),
            original_filename=file.filename or "attachment.pdf",
            content=content,
        )
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.get("/attachments/{attachment_id}")
    def attachment_entry(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        if session.get(Attachment, attachment_id) is None:
            return RedirectResponse("/projects", status_code=303)
        return RedirectResponse(f"/attachments/{attachment_id}/preview", status_code=303)

    @app.get("/attachments/{attachment_id}/preview")
    def preview_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        attachment = session.get(Attachment, attachment_id)
        if attachment is None:
            return RedirectResponse("/projects", status_code=303)
        return templates.TemplateResponse(
            request,
            "attachment_preview.html",
            {"attachment": attachment},
        )

    @app.get("/attachments/{attachment_id}/file")
    def inline_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        return _attachment_file_response(
            request,
            attachment_id,
            session,
            app.state.data_dir,
            content_disposition_type="inline",
        )

    @app.get("/attachments/{attachment_id}/download")
    def download_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        return _attachment_file_response(
            request,
            attachment_id,
            session,
            app.state.data_dir,
            content_disposition_type="attachment",
        )

    def _attachment_file_response(
        request: Request,
        attachment_id: int,
        session: Session,
        data_dir: Path,
        *,
        content_disposition_type: str,
    ):
        if redirect := login_redirect(request):
            return redirect
        attachment = session.get(Attachment, attachment_id)
        if attachment is None:
            return RedirectResponse("/projects", status_code=303)
        path = data_dir / attachment.stored_path
        return FileResponse(
            path,
            filename=attachment.original_filename,
            media_type="application/pdf",
            content_disposition_type=content_disposition_type,
        )

    return app


def _available_years(session: Session, selected_year: int) -> list[int]:
    years = {selected_year}
    for project in session.exec(select_projects_by_year()):
        years.add(project.year)
    return sorted(years, reverse=True)


def _project_years(session: Session) -> list[int]:
    return sorted({project.year for project in session.exec(select_projects_by_year())}, reverse=True)


def select_projects_by_year():
    from sqlmodel import select

    return select(Project).order_by(Project.year.desc())


def _parse_float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    return float(value)


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_optional_year(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


app = create_app(
    database_url=os.getenv("PROJMAN_DATABASE_URL", "sqlite:///data/app.db"),
    data_dir=Path(os.getenv("PROJMAN_DATA_DIR", "data")),
    username=os.getenv("PROJMAN_USERNAME", "admin"),
    password=os.getenv("PROJMAN_PASSWORD", "admin"),
    secret_key=os.getenv("PROJMAN_SECRET_KEY", "projman-local-secret"),
)
