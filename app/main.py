from datetime import date
from io import BytesIO
import math
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

from app.ai_project import (
    AiProjectClientError,
    DashScopeAiProjectClient,
    SUPPORTED_AI_EXTENSIONS,
    cleanup_ai_input_file,
    cleanup_expired_ai_drafts,
    create_ai_draft,
    draft_field_value,
    draft_fields,
    draft_risk_tips,
    extract_pdf_text,
    load_ai_settings,
    render_pdf_pages,
    store_ai_input_file,
)
from app.domain import AttachmentKind
from app.exporters import build_projects_excel
from app.models import AiProjectDraft, AnnualAttachment, AnnualExecution, Attachment, Project
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
    resolve_stored_file,
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
    env_file: Path | str | None = None,
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
    app.state.env_path = Path(env_file) if env_file is not None else data_path.parent / ".env"
    app.state.ai_env_overrides = {}
    app.state.username = username
    app.state.password = password
    app.state.ai_project_client = None
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
                **_ai_settings_context(app),
            },
        )

    @app.post("/settings")
    def save_settings(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        renewal_lead_days: Annotated[int, Form()],
        ai_settings_submitted: Annotated[str, Form()] = "",
        ai_enabled: Annotated[str, Form()] = "false",
        ai_api_key: Annotated[str, Form()] = "",
        ai_model: Annotated[str, Form()] = "",
        ai_base_url: Annotated[str, Form()] = "",
        ai_proxy: Annotated[str, Form()] = "",
        ai_timeout_seconds: Annotated[str, Form()] = "",
        ai_max_upload_mb: Annotated[str, Form()] = "",
        ai_draft_ttl_hours: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        if renewal_lead_days < 1:
            return _bad_request("续采提醒提前天数必须大于 0")
        set_int_setting(session, "renewal_lead_days", renewal_lead_days)
        if ai_settings_submitted:
            try:
                _save_ai_settings_from_form(
                    app,
                    enabled=ai_enabled.lower() in {"true", "on", "1", "yes"},
                    api_key=ai_api_key,
                    model=ai_model,
                    base_url=ai_base_url,
                    proxy=ai_proxy,
                    timeout_seconds=ai_timeout_seconds,
                    max_upload_mb=ai_max_upload_mb,
                    draft_ttl_hours=ai_draft_ttl_hours,
                )
            except ValueError as exc:
                return _bad_request(str(exc))
        return RedirectResponse("/settings", status_code=303)

    @app.get("/ai-projects/new")
    def ai_new_project_page(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        settings = _load_ai_settings(app)
        cleanup_expired_ai_drafts(
            session,
            data_dir=app.state.data_dir,
            ttl_hours=settings.draft_ttl_hours,
        )
        return templates.TemplateResponse(
            request,
            "ai_project_new.html",
            _ai_page_context(app, settings=settings),
        )

    @app.post("/ai-projects/drafts")
    async def create_ai_project_draft_route(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        text: Annotated[str, Form()] = "",
        file: Annotated[UploadFile | None, File()] = None,
    ):
        if redirect := login_redirect(request):
            return redirect

        settings = _load_ai_settings(app)
        if not settings.enabled:
            return _bad_request("AI 功能未启用")
        if settings.enabled and not settings.api_key:
            return _bad_request("AI 功能已启用，但未配置 API key")
        ai_client = app.state.ai_project_client
        if ai_client is None:
            ai_client = DashScopeAiProjectClient(settings)

        text_input = text.strip()
        uploaded_filename = (file.filename or "").strip() if file is not None else ""
        if not text_input and not uploaded_filename:
            return _ai_form_error_response(app, request, "请至少提供文字或上传文件")

        stored_input_path = ""
        input_filename = ""
        input_content_type = ""
        input_kind = "text"
        ai_text = text_input
        file_bytes: bytes | None = None
        image_files = None
        source_pages: list[int] = []

        if uploaded_filename:
            content = await file.read()
            extension = Path(uploaded_filename).suffix.lower()
            if extension not in SUPPORTED_AI_EXTENSIONS:
                return _ai_form_error_response(app, request, "仅支持 PDF、PNG、JPG、JPEG、WebP")
            if len(content) > settings.max_upload_mb * 1_000_000:
                return _ai_form_error_response(
                    app,
                    request,
                    "文件过大，请压缩到限制以内后再上传",
                )
            input_filename = uploaded_filename
            input_content_type = file.content_type or ""
            stored_input_path = store_ai_input_file(
                app.state.data_dir,
                filename=input_filename,
                content=content,
            )
            if extension == ".pdf":
                extractor = getattr(app.state, "ai_pdf_text_extractor", extract_pdf_text)
                try:
                    pdf_text = extractor(content).strip()
                except Exception:
                    cleanup_ai_input_file(app.state.data_dir, stored_input_path)
                    return _ai_form_error_response(
                        app,
                        request,
                        "PDF 解析失败，请改传图片或输入文字",
                    )
                if not pdf_text:
                    renderer = getattr(app.state, "ai_pdf_image_renderer", render_pdf_pages)
                    try:
                        image_files = renderer(content)
                    except Exception:
                        image_files = []
                    if not image_files:
                        cleanup_ai_input_file(app.state.data_dir, stored_input_path)
                        return _ai_form_error_response(
                            app,
                            request,
                            "PDF 未提取到可识别文本，也无法生成预览图片，请换一个 PDF、改传图片或输入文字",
                        )
                    source_pages = []
                    for image_file in image_files:
                        try:
                            source_pages.append(int(image_file.get("page")))
                        except (TypeError, ValueError):
                            continue
                    ai_text = "\n\n".join(
                        value
                        for value in [
                            text_input,
                            f"请识别 PDF 图片页：{input_filename}",
                        ]
                        if value
                    )
                    input_kind = "pdf_image"
                else:
                    ai_text = "\n\n".join(
                        value for value in [text_input, pdf_text] if value
                    )
                    input_kind = "pdf_text"
            else:
                file_bytes = content
                input_kind = "image"

        try:
            ai_request = {
                "text": ai_text,
                "file_bytes": file_bytes,
                "filename": input_filename,
                "input_kind": input_kind,
                "source_pages": source_pages,
            }
            if image_files is not None:
                ai_request["image_files"] = image_files
            ai_response = ai_client.extract_project(**ai_request)
        except AiProjectClientError as exc:
            cleanup_ai_input_file(app.state.data_dir, stored_input_path)
            return templates.TemplateResponse(
                request,
                "ai_project_new.html",
                _ai_page_context(app, error=f"识别失败：{exc} 可重试或手工新建。"),
                status_code=200,
            )
        except Exception:
            cleanup_ai_input_file(app.state.data_dir, stored_input_path)
            return templates.TemplateResponse(
                request,
                "ai_project_new.html",
                _ai_page_context(
                    app,
                    error=(
                        "识别失败：AI 服务调用异常。可重试或手工新建；"
                        "如果是扫描 PDF，请压缩文件或补充文字说明。"
                    ),
                ),
                status_code=200,
            )

        draft = create_ai_draft(
            session,
            input_kind=input_kind,
            input_filename=input_filename,
            input_content_type=input_content_type,
            stored_input_path=stored_input_path,
            input_text=ai_text,
            ai_response=ai_response,
        )
        return RedirectResponse(f"/ai-projects/drafts/{draft.id}", status_code=303)

    @app.get("/ai-projects/drafts/{draft_id}")
    def ai_project_draft_page(
        request: Request,
        draft_id: str,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        draft = _get_ai_draft(session, draft_id)
        if draft is None:
            return _bad_request("草稿不存在")
        return templates.TemplateResponse(
            request,
            "ai_project_draft.html",
            _ai_draft_context(draft),
        )

    @app.post("/ai-projects/drafts/{draft_id}/confirm")
    def confirm_ai_project_draft(
        request: Request,
        draft_id: str,
        session: Annotated[Session, Depends(get_session)],
        year: Annotated[str, Form()],
        name: Annotated[str, Form()],
        budget_amount: Annotated[str, Form()] = "",
        contract_amount: Annotated[str, Form()] = "",
        contract_start: Annotated[str, Form()] = "",
        contract_end: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
        archive_pdf_kind: Annotated[str, Form()] = "",
    ):
        if redirect := login_redirect(request):
            return redirect
        try:
            parsed_year = int(year.strip())
            parsed_budget = _parse_float(budget_amount, "预算金额")
            parsed_contract_amount = _parse_float(contract_amount, "合同金额")
            parsed_contract_start = _parse_date(contract_start, "合同开始日期")
            parsed_contract_end = _parse_date(contract_end, "合同结束日期")
            if (
                parsed_contract_start
                and parsed_contract_end
                and parsed_contract_start > parsed_contract_end
            ):
                raise ValueError("合同开始日期不能晚于合同结束日期")
        except ValueError as exc:
            return _bad_request(str(exc))

        draft = _get_ai_draft(session, draft_id)
        if draft is None:
            return _bad_request("草稿不存在")
        if draft.status == "abandoned":
            return _bad_request("草稿已放弃")
        if draft.status == "created":
            return _bad_request("草稿已创建项目")

        archive_kind: AttachmentKind | None = None
        archive_content: bytes | None = None
        if archive_pdf_kind.strip():
            try:
                archive_kind = AttachmentKind(archive_pdf_kind.strip())
            except ValueError:
                return _bad_request("附件归档类型不正确")
            if archive_kind not in PROJECT_ATTACHMENT_KINDS:
                return _bad_request("附件归档类型不正确")
            if not draft.input_filename.lower().endswith(".pdf"):
                return _bad_request("只支持上传 PDF 文件")
            input_path = resolve_stored_file(app.state.data_dir, draft.stored_input_path)
            if input_path is None:
                return _bad_request("AI 输入文件不存在，不能归档")
            archive_content = input_path.read_bytes()
            if not archive_content.startswith(b"%PDF"):
                return _bad_request("只支持上传 PDF 文件")

        try:
            project = create_project(
                session,
                year=parsed_year,
                name=name,
                budget_amount=parsed_budget,
                notes=notes,
            )
            update_project(
                session,
                project.id,
                contract_amount=parsed_contract_amount,
                contract_start=parsed_contract_start,
                contract_end=parsed_contract_end,
            )
        except ValueError as exc:
            return _bad_request(str(exc))
        sync_annual_executions(session, project, data_dir=app.state.data_dir)

        if (
            archive_kind is not None
            and archive_content is not None
            and draft.input_filename.lower().endswith(".pdf")
        ):
            try:
                save_attachment_bytes(
                    session,
                    project=project,
                    data_dir=app.state.data_dir,
                    kind=archive_kind,
                    original_filename=draft.input_filename,
                    content=archive_content,
                )
            except ValueError as exc:
                return _bad_request(str(exc))
        cleanup_ai_input_file(app.state.data_dir, draft.stored_input_path)
        draft.status = "created"
        draft.project_id = project.id
        session.add(draft)
        session.commit()
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @app.post("/ai-projects/drafts/{draft_id}/abandon")
    def abandon_ai_project_draft(
        request: Request,
        draft_id: str,
        session: Annotated[Session, Depends(get_session)],
    ):
        if redirect := login_redirect(request):
            return redirect
        draft = _get_ai_draft(session, draft_id)
        if draft is None:
            return RedirectResponse("/ai-projects/new", status_code=303)
        draft.status = "abandoned"
        session.add(draft)
        session.commit()
        cleanup_ai_input_file(app.state.data_dir, draft.stored_input_path)
        return RedirectResponse("/ai-projects/new", status_code=303)

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
                path = resolve_stored_file(app.state.data_dir, attachment.stored_path)
                if path is not None:
                    zip_file.write(
                        path,
                        arcname=f"{attachment.kind.label}/{attachment.original_filename}",
                    )
            for attachment in annual_attachments:
                path = resolve_stored_file(app.state.data_dir, attachment.stored_path)
                if path is not None:
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
        try:
            project = create_project(
                session,
                year=year,
                name=name,
                budget_amount=_parse_float(budget_amount, "预算金额"),
                notes=notes,
            )
        except ValueError as exc:
            return _bad_request(str(exc))
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
        try:
            project = update_project(
                session,
                project_id,
                year=year,
                name=name.strip(),
                budget_amount=_parse_float(budget_amount, "项目预算总额"),
                contract_amount=_parse_float(contract_amount, "合同总额"),
                contract_start=_parse_date(contract_start, "合同开始日期"),
                contract_end=_parse_date(contract_end, "合同结束日期"),
                acceptance_date=_parse_date(acceptance_date, "验收日期"),
                payment_date=_parse_date(payment_date, "付款日期"),
                notes=notes.strip(),
            )
        except ValueError as exc:
            return _bad_request(str(exc))
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
        try:
            update_annual_execution(
                session,
                execution_id,
                budget_amount=_parse_float(budget_amount, "年度预算"),
                contract_amount=_parse_float(contract_amount, "年度合同金额"),
                contract_only=contract_only == "on",
                acceptance_date=_parse_date(acceptance_date, "验收日期"),
                payment_date=_parse_date(payment_date, "付款日期"),
                notes=notes.strip(),
            )
        except ValueError as exc:
            return _bad_request(str(exc))
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
        try:
            save_annual_attachment_bytes(
                session,
                execution=execution,
                data_dir=app.state.data_dir,
                kind=AttachmentKind(kind),
                original_filename=file.filename or "attachment.pdf",
                content=content,
            )
        except ValueError as exc:
            return _bad_request(str(exc))
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
                path = resolve_stored_file(app.state.data_dir, attachment.stored_path)
                if path is not None:
                    zip_file.write(
                        path,
                        arcname=f"项目资料/{attachment.kind.label}/{attachment.original_filename}",
                    )
            for attachment in [
                *overview.annual_attachments,
                *overview.legacy_annual_attachments,
            ]:
                path = resolve_stored_file(app.state.data_dir, attachment.stored_path)
                if path is not None:
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
        try:
            save_attachment_bytes(
                session,
                project=project,
                data_dir=app.state.data_dir,
                kind=AttachmentKind(kind),
                original_filename=file.filename or "attachment.pdf",
                content=content,
            )
        except ValueError as exc:
            return _bad_request(str(exc))
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
        path = resolve_stored_file(data_dir, attachment.stored_path)
        if path is None:
            return RedirectResponse("/projects", status_code=303)
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
        path = resolve_stored_file(data_dir, attachment.stored_path)
        if path is None:
            return RedirectResponse("/projects", status_code=303)
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


def _bad_request(message: str) -> Response:
    return Response(message, status_code=400, media_type="text/plain; charset=utf-8")


def _load_ai_settings(app: FastAPI):
    environ = {**os.environ, **getattr(app.state, "ai_env_overrides", {})}
    return load_ai_settings(
        environ=environ,
        read_credentials=getattr(app.state, "ai_credential_reader", None),
    )


def _ai_page_context(
    app: FastAPI,
    error: str | None = None,
    settings=None,
) -> dict:
    settings = settings or _load_ai_settings(app)
    return {
        "settings": settings,
        "error": error,
        "ai_disabled": not settings.enabled,
    }


def _ai_form_error_response(
    app: FastAPI,
    request: Request,
    message: str,
) -> Response:
    return templates.TemplateResponse(
        request,
        "ai_project_new.html",
        _ai_page_context(app, error=message),
        status_code=400,
    )


def _ai_settings_context(app: FastAPI) -> dict:
    settings = _load_ai_settings(app)
    return {
        "ai_settings": settings,
        "ai_api_key_configured": bool(settings.api_key),
        "ai_env_path": app.state.env_path,
    }


def _save_ai_settings_from_form(
    app: FastAPI,
    *,
    enabled: bool,
    api_key: str,
    model: str,
    base_url: str,
    proxy: str,
    timeout_seconds: str,
    max_upload_mb: str,
    draft_ttl_hours: str,
) -> None:
    current_settings = _load_ai_settings(app)
    timeout_value = _parse_positive_int(timeout_seconds or "300", "AI 调用超时时间")
    upload_value = _parse_positive_int(max_upload_mb or "10", "AI 上传大小上限")
    ttl_value = _parse_positive_int(draft_ttl_hours or "24", "AI 草稿保留时间")
    new_api_key = api_key.strip()
    existing_file_values = _read_env_key_values(app.state.env_path)
    stored_api_key = (
        new_api_key
        or existing_file_values.get("PROJMAN_AI_API_KEY", "")
        or current_settings.api_key
        or ""
    )
    if enabled and not stored_api_key:
        raise ValueError("启用 AI 前请填写 API Key")

    updates = {
        "PROJMAN_AI_ENABLED": "true" if enabled else "false",
        "PROJMAN_AI_PROVIDER": current_settings.provider or "aliyun_dashscope",
        "PROJMAN_AI_BASE_URL": (
            base_url.strip() or current_settings.base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "PROJMAN_AI_MODEL": model.strip() or current_settings.model or "qwen3-vl-plus",
        "PROJMAN_AI_API_KEY": stored_api_key,
        "PROJMAN_AI_CREDENTIAL_SOURCE": "env_then_optional_file",
        "PROJMAN_AI_CREDENTIAL_FILE": existing_file_values.get(
            "PROJMAN_AI_CREDENTIAL_FILE",
            "",
        ),
        "PROJMAN_AI_PROXY": proxy.strip(),
        "PROJMAN_AI_TIMEOUT_SECONDS": str(timeout_value),
        "PROJMAN_AI_MAX_UPLOAD_MB": str(upload_value),
        "PROJMAN_AI_DRAFT_TTL_HOURS": str(ttl_value),
    }
    _update_env_file(app.state.env_path, updates)
    app.state.ai_env_overrides = {**getattr(app.state, "ai_env_overrides", {}), **updates}


def _parse_positive_int(value: str, field_label: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError:
        raise ValueError(f"{field_label}必须是整数") from None
    if parsed < 1:
        raise ValueError(f"{field_label}必须大于 0")
    return parsed


def _read_env_key_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(raw_line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(raw_line)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def _get_ai_draft(session: Session, draft_id: str) -> AiProjectDraft | None:
    try:
        parsed_id = int(draft_id)
    except ValueError:
        return None
    return session.get(AiProjectDraft, parsed_id)


def _ai_draft_context(draft: AiProjectDraft) -> dict:
    fields = draft_fields(draft)
    field_values = {
        key: draft_field_value(draft, key)
        for key in [
            "year",
            "name",
            "budget_amount",
            "contract_amount",
            "contract_start",
            "contract_end",
            "notes",
        ]
    }
    fields_by_key = {field["key"]: field for field in fields}
    review_fields = []
    for key in [
        "year",
        "name",
        "budget_amount",
        "contract_amount",
        "contract_start",
        "contract_end",
        "notes",
    ]:
        field = fields_by_key.get(key, {})
        review_fields.append(
            {
                "key": key,
                "label": field.get("label") or key,
                "value": field_values[key],
                "source": field.get("source") or "未标注",
                "evidence": field.get("evidence") or "未提供证据摘录",
                "confidence": field.get("confidence") or "未标注",
                "valid": field.get("valid", True),
                "error": field.get("error", ""),
            }
        )
    return {
        "draft": draft,
        "fields": fields,
        "review_fields": review_fields,
        "field_values": field_values,
        "risk_tips": draft_risk_tips(draft),
        "project_attachment_kinds": PROJECT_ATTACHMENT_KINDS,
    }


def _parse_float(value: str, field_label: str = "金额") -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError(f"{field_label}格式不正确") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{field_label}格式不正确")
    return parsed


def _parse_date(value: str, field_label: str = "日期") -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field_label}格式不正确") from None


def _parse_optional_year(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


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
    env_file=Path(os.getenv("PROJMAN_ENV_FILE", ".env")),
    username=os.getenv("PROJMAN_USERNAME", "admin"),
    password=os.getenv("PROJMAN_PASSWORD", "admin"),
    secret_key=os.getenv("PROJMAN_SECRET_KEY", "projman-local-secret"),
)
