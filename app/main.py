from datetime import date
from io import BytesIO
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, SQLModel, create_engine
from starlette.middleware.sessions import SessionMiddleware

from app.domain import AttachmentKind
from app.exporters import build_projects_excel
from app.models import AnnualAttachment, AnnualExecution, Attachment, Project
from app.services import (
    ANNUAL_ATTACHMENT_KINDS,
    PROJECT_ATTACHMENT_KINDS,
    STATUS_FILTER_OPTIONS,
    annual_project_overview,
    create_project,
    delete_annual_attachment as delete_annual_attachment_record,
    delete_attachment as delete_attachment_record,
    dashboard_summary,
    delete_project as delete_project_record,
    filter_project_overviews,
    format_money,
    get_int_setting,
    list_annual_project_overviews,
    list_project_overviews,
    normalize_status_filter,
    project_overview,
    save_annual_attachment_bytes,
    save_attachment_bytes,
    set_int_setting,
    summarize_project_overviews,
    sync_annual_executions,
    update_annual_execution,
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
            data_dir=app.state.data_dir,
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
        q: str | None = None,
        status: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        selected_year = _parse_optional_year(year)
        query = (q or "").strip()
        selected_status = normalize_status_filter(status)
        overviews = filter_project_overviews(
            list_annual_project_overviews(
                session,
                year=selected_year,
                data_dir=app.state.data_dir,
            ),
            query=query,
            status_filter=selected_status,
        )
        return templates.TemplateResponse(
            request,
            "projects.html",
            {
                "overviews": overviews,
                "year": selected_year,
                "query": query,
                "status_filter": selected_status,
                "status_filter_options": STATUS_FILTER_OPTIONS,
                "ledger_summary": summarize_project_overviews(overviews),
                "project_detail_url": _project_detail_url,
                "return_context_url": _return_context_url,
                "export_url": _build_project_url(
                    "/projects/export",
                    year=selected_year,
                    q=query,
                    status=selected_status,
                ),
                "years": _project_years(session),
                "attachment_kinds": list(AttachmentKind),
                "money": format_money,
            },
        )

    @app.get("/projects/export")
    def export_projects(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        year: str | None = None,
        q: str | None = None,
        status: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        selected_year = _parse_optional_year(year)
        selected_status = normalize_status_filter(status)
        overviews = filter_project_overviews(
            list_annual_project_overviews(
                session,
                year=selected_year,
                data_dir=app.state.data_dir,
            ),
            query=q,
            status_filter=selected_status,
        )
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
        annual_executions = sync_annual_executions(
            session,
            project,
            data_dir=app.state.data_dir,
        )
        annual_attachments = [
            attachment
            for execution in annual_executions
            for attachment in annual_project_overview(session, execution).annual_attachments
        ]
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
            for attachment in attachments:
                path = app.state.data_dir / attachment.stored_path
                if path.exists():
                    zip_file.write(
                        path,
                        arcname=f"{attachment.kind.label}/{attachment.original_filename}",
                    )
            for attachment in annual_attachments:
                path = app.state.data_dir / attachment.stored_path
                if path.exists():
                    zip_file.write(
                        path,
                        arcname=(
                            f"年度资料/{attachment.kind.label}/"
                            f"{attachment.original_filename}"
                        ),
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
        project = create_project(
            session,
            year=year,
            name=name,
            budget_amount=_parse_float(budget_amount),
            notes=notes,
        )
        sync_annual_executions(session, project, data_dir=app.state.data_dir)
        return RedirectResponse(f"/?year={year}", status_code=303)

    @app.get("/projects/{project_id}")
    def project_detail(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
        return_year: str | None = None,
        q: str | None = None,
        status: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        project = session.get(Project, project_id)
        if project is None:
            return RedirectResponse("/projects", status_code=303)
        overview = project_overview(session, project)
        selected_return_year = _normalize_return_year(return_year)
        return_query = (q or "").strip()
        return_status = normalize_status_filter(status)
        back_year = selected_return_year or overview.project.year
        annual_overviews = [
            annual_project_overview(session, execution)
            for execution in sync_annual_executions(
                session,
                project,
                data_dir=app.state.data_dir,
            )
        ]
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            {
                "overview": overview,
                "annual_overviews": annual_overviews,
                "project_attachment_kinds": PROJECT_ATTACHMENT_KINDS,
                "annual_attachment_kinds": ANNUAL_ATTACHMENT_KINDS,
                "return_year": selected_return_year,
                "return_query": return_query,
                "return_status": return_status,
                "return_context_url": _return_context_url,
                "back_to_projects_url": _build_project_url(
                    "/projects",
                    year=back_year,
                    q=return_query,
                    status=return_status,
                ),
                "money": format_money,
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
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        project = update_project(
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
        sync_annual_executions(session, project, data_dir=app.state.data_dir)
        return RedirectResponse(
            _project_detail_url(project_id, return_year, q=q, status=status),
            status_code=303,
        )

    @app.post("/projects/{project_id}/delete")
    def delete_project_route(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        project = session.get(Project, project_id)
        back_year = _normalize_return_year(return_year)
        if project is not None and back_year is None:
            back_year = project.year
        delete_project_record(session, project_id, data_dir=app.state.data_dir)
        return RedirectResponse(
            _build_project_url(
                "/projects",
                year=back_year,
                q=q.strip(),
                status=normalize_status_filter(status),
            ),
            status_code=303,
        )

    @app.post("/annual-executions/{execution_id}/edit")
    def edit_annual_execution(
        request: Request,
        execution_id: int,
        session: Annotated[Session, Depends(get_session)],
        budget_amount: Annotated[str, Form()] = "",
        contract_amount: Annotated[str, Form()] = "",
        contract_only: Annotated[str | None, Form()] = None,
        acceptance_date: Annotated[str, Form()] = "",
        payment_date: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        execution = session.get(AnnualExecution, execution_id)
        if execution is None:
            return RedirectResponse("/projects", status_code=303)
        update_annual_execution(
            session,
            execution_id,
            budget_amount=_parse_float(budget_amount),
            contract_amount=_parse_float(contract_amount),
            contract_only=contract_only == "on",
            acceptance_date=_parse_date(acceptance_date),
            payment_date=_parse_date(payment_date),
            notes=notes.strip(),
        )
        return RedirectResponse(
            _project_detail_url(execution.project_id, return_year, q=q, status=status),
            status_code=303,
        )

    @app.post("/annual-executions/{execution_id}/attachments")
    async def upload_annual_attachment(
        request: Request,
        execution_id: int,
        session: Annotated[Session, Depends(get_session)],
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        execution = session.get(AnnualExecution, execution_id)
        if execution is None:
            return RedirectResponse("/projects", status_code=303)
        content = await file.read()
        save_annual_attachment_bytes(
            session,
            execution=execution,
            data_dir=app.state.data_dir,
            kind=AttachmentKind(kind),
            original_filename=file.filename or "attachment.pdf",
            content=content,
        )
        return RedirectResponse(
            _project_detail_url(execution.project_id, return_year, q=q, status=status),
            status_code=303,
        )

    @app.get("/annual-executions/{execution_id}/attachments/download-year")
    def download_annual_execution_attachments(
        request: Request,
        execution_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        execution = session.get(AnnualExecution, execution_id)
        if execution is None:
            return RedirectResponse("/projects", status_code=303)
        overview = annual_project_overview(session, execution)
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
            for attachment in overview.project_attachments:
                path = app.state.data_dir / attachment.stored_path
                if path.exists():
                    zip_file.write(
                        path,
                        arcname=f"项目资料/{attachment.kind.label}/{attachment.original_filename}",
                    )
            for attachment in [
                *overview.annual_attachments,
                *overview.legacy_annual_attachments,
            ]:
                path = app.state.data_dir / attachment.stored_path
                if path.exists():
                    zip_file.write(
                        path,
                        arcname=f"年度资料/{attachment.kind.label}/{attachment.original_filename}",
                    )
        archive.seek(0)
        return Response(
            archive.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="project-{execution.project_id}-'
                    f'{execution.year}-attachments.zip"'
                )
            },
        )

    @app.post("/projects/{project_id}/attachments")
    async def upload_attachment(
        request: Request,
        project_id: int,
        session: Annotated[Session, Depends(get_session)],
        kind: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
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
        return RedirectResponse(
            _project_detail_url(project_id, return_year, q=q, status=status),
            status_code=303,
        )

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
        return_year: str | None = None,
        q: str | None = None,
        status: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        attachment = session.get(Attachment, attachment_id)
        if attachment is None:
            return RedirectResponse("/projects", status_code=303)
        selected_return_year = _normalize_return_year(return_year)
        return_query = (q or "").strip()
        return_status = normalize_status_filter(status)
        return templates.TemplateResponse(
            request,
            "attachment_preview.html",
            {
                "attachment": attachment,
                "file_url": f"/attachments/{attachment.id}/file",
                "download_url": f"/attachments/{attachment.id}/download",
                "delete_url": f"/attachments/{attachment.id}/delete",
                "back_url": _project_detail_url(
                    attachment.project_id,
                    selected_return_year,
                    q=return_query,
                    status=return_status,
                ),
                "return_year": selected_return_year,
                "return_query": return_query,
                "return_status": return_status,
            },
        )

    @app.get("/annual-attachments/{attachment_id}/preview")
    def preview_annual_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
        return_year: str | None = None,
        q: str | None = None,
        status: str | None = None,
    ):
        if redirect := login_redirect(request):
            return redirect
        attachment = session.get(AnnualAttachment, attachment_id)
        if attachment is None:
            return RedirectResponse("/projects", status_code=303)
        execution = session.get(AnnualExecution, attachment.execution_id)
        if execution is None:
            return RedirectResponse("/projects", status_code=303)
        selected_return_year = _normalize_return_year(return_year)
        return_query = (q or "").strip()
        return_status = normalize_status_filter(status)
        return templates.TemplateResponse(
            request,
            "attachment_preview.html",
            {
                "attachment": attachment,
                "file_url": f"/annual-attachments/{attachment.id}/file",
                "download_url": f"/annual-attachments/{attachment.id}/download",
                "delete_url": f"/annual-attachments/{attachment.id}/delete",
                "back_url": _project_detail_url(
                    execution.project_id,
                    selected_return_year,
                    q=return_query,
                    status=return_status,
                ),
                "return_year": selected_return_year,
                "return_query": return_query,
                "return_status": return_status,
            },
        )

    @app.post("/attachments/{attachment_id}/delete")
    def delete_attachment_route(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        deleted = delete_attachment_record(
            session,
            attachment_id,
            data_dir=app.state.data_dir,
        )
        if deleted is None:
            return RedirectResponse("/projects", status_code=303)
        return RedirectResponse(
            _project_detail_url(deleted.project_id, return_year, q=q, status=status),
            status_code=303,
        )

    @app.post("/annual-attachments/{attachment_id}/delete")
    def delete_annual_attachment_route(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
        return_year: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        deleted = delete_annual_attachment_record(
            session,
            attachment_id,
            data_dir=app.state.data_dir,
        )
        if deleted is None:
            return RedirectResponse("/projects", status_code=303)
        return RedirectResponse(
            _project_detail_url(deleted.project_id, return_year, q=q, status=status),
            status_code=303,
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

    @app.get("/annual-attachments/{attachment_id}/file")
    def inline_annual_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        return _annual_attachment_file_response(
            request,
            attachment_id,
            session,
            app.state.data_dir,
            content_disposition_type="inline",
        )

    @app.get("/annual-attachments/{attachment_id}/download")
    def download_annual_attachment(
        request: Request,
        attachment_id: int,
        session: Annotated[Session, Depends(get_session)],
    ):
        return _annual_attachment_file_response(
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

    def _annual_attachment_file_response(
        request: Request,
        attachment_id: int,
        session: Session,
        data_dir: Path,
        *,
        content_disposition_type: str,
    ):
        if redirect := login_redirect(request):
            return redirect
        attachment = session.get(AnnualAttachment, attachment_id)
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
    for execution in session.exec(select_annual_executions_by_year()):
        years.add(execution.year)
    for project in session.exec(select_projects_by_year()):
        years.add(project.year)
    return sorted(years, reverse=True)


def _project_years(session: Session) -> list[int]:
    years = {execution.year for execution in session.exec(select_annual_executions_by_year())}
    years.update(project.year for project in session.exec(select_projects_by_year()))
    return sorted(years, reverse=True)


def select_projects_by_year():
    from sqlmodel import select

    return select(Project).order_by(Project.year.desc())


def select_annual_executions_by_year():
    from sqlmodel import select

    return select(AnnualExecution).order_by(AnnualExecution.year.desc())


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


def _normalize_return_year(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    try:
        return _parse_optional_year(value)
    except ValueError:
        return None


def _project_detail_url(
    project_id: int,
    return_year: str | int | None = None,
    *,
    q: str = "",
    status: str = "",
) -> str:
    params: dict[str, str | int] = {}
    year = _normalize_return_year(return_year)
    if year is not None:
        params["return_year"] = year
    if q.strip():
        params["q"] = q.strip()
    normalized_status = normalize_status_filter(status)
    if normalized_status:
        params["status"] = normalized_status
    query_string = urlencode(params)
    return f"/projects/{project_id}?{query_string}" if query_string else f"/projects/{project_id}"


def _return_context_url(
    path: str,
    return_year: str | int | None = None,
    *,
    q: str = "",
    status: str = "",
) -> str:
    params: dict[str, str | int] = {}
    year = _normalize_return_year(return_year)
    if year is not None:
        params["return_year"] = year
    if q.strip():
        params["q"] = q.strip()
    normalized_status = normalize_status_filter(status)
    if normalized_status:
        params["status"] = normalized_status
    query_string = urlencode(params)
    return f"{path}?{query_string}" if query_string else path


def _build_project_url(
    path: str,
    *,
    year: int | None,
    q: str,
    status: str,
) -> str:
    params: dict[str, str | int] = {}
    if year:
        params["year"] = year
    if q:
        params["q"] = q
    if status:
        params["status"] = status
    query_string = urlencode(params)
    return f"{path}?{query_string}" if query_string else path


app = create_app(
    database_url=os.getenv("PROJMAN_DATABASE_URL", "sqlite:///data/app.db"),
    data_dir=Path(os.getenv("PROJMAN_DATA_DIR", "data")),
    username=os.getenv("PROJMAN_USERNAME", "admin"),
    password=os.getenv("PROJMAN_PASSWORD", "admin"),
    secret_key=os.getenv("PROJMAN_SECRET_KEY", "projman-local-secret"),
)
