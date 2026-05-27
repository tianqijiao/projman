from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import create_app
from app.models import AiProjectDraft, Attachment, Project


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        data_dir=tmp_path / "data",
        username="admin",
        password="secret",
        secret_key="test-secret",
    )
    return TestClient(app)


def login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def enable_ai(monkeypatch) -> None:
    monkeypatch.setenv("PROJMAN_AI_ENABLED", "true")
    monkeypatch.setenv("PROJMAN_AI_API_KEY", "test-key")


class FakeAiProjectClient:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None):
        self.response = response or {}
        self.error = error
        self.calls: list[dict] = []

    def extract_project(
        self,
        *,
        text: str = "",
        file_bytes: bytes | None = None,
        filename: str = "",
        **kwargs,
    ):
        self.calls.append(
            {
                "text": text,
                "file_bytes": file_bytes,
                "filename": filename,
                **kwargs,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


def ai_contract_response() -> dict:
    return {
        "fields": {
            "year": {
                "value": 2027,
                "source": "用户文字",
                "evidence": "2027 年行政电脑维护服务",
                "confidence": "高",
            },
            "name": {
                "value": "行政电脑维护服务",
                "source": "上传 PDF",
                "evidence": "项目名称：行政电脑维护服务",
                "confidence": "高",
            },
            "budget_amount": {
                "value": 50000,
                "source": "用户文字",
                "evidence": "预算 5 万",
                "confidence": "中",
            },
            "contract_amount": {
                "value": 48000,
                "source": "上传 PDF",
                "evidence": "合同金额人民币 4.8 万元",
                "confidence": "高",
            },
            "contract_start": {
                "value": "2027-01-01",
                "source": "上传 PDF",
                "evidence": "服务期自 2027 年 1 月 1 日起",
                "confidence": "高",
            },
            "contract_end": {
                "value": "2027-12-31",
                "source": "上传 PDF",
                "evidence": "至 2027 年 12 月 31 日止",
                "confidence": "高",
            },
            "notes": {
                "value": "供应商：示例科技；合同编号：HT-2027-001",
                "source": "上传 PDF",
                "evidence": "合同编号 HT-2027-001",
                "confidence": "中",
            },
        },
        "risk_tips": ["请核对合同金额是否含税。"],
    }


def ai_invalid_contract_response() -> dict:
    response = ai_contract_response()
    response["fields"] = {
        **response["fields"],
        "name": {
            "value": "   ",
            "source": "上传 PDF",
            "evidence": "未识别到明确项目名称",
            "confidence": "低",
        },
        "contract_amount": {
            "value": "not-a-number",
            "source": "上传 PDF",
            "evidence": "合同金额识别失败",
            "confidence": "低",
        },
        "contract_start": {
            "value": "2027-12-31",
            "source": "上传 PDF",
            "evidence": "开始日期可能识别错误",
            "confidence": "低",
        },
        "contract_end": {
            "value": "2027-01-01",
            "source": "上传 PDF",
            "evidence": "结束日期可能识别错误",
            "confidence": "低",
        },
    }
    return response


def test_ai_config_defaults_to_aliyun_dashscope_qwen3_vl_plus(monkeypatch):
    monkeypatch.delenv("PROJMAN_AI_PROVIDER", raising=False)
    monkeypatch.delenv("PROJMAN_AI_BASE_URL", raising=False)
    monkeypatch.delenv("PROJMAN_AI_MODEL", raising=False)

    from app.ai_project import load_ai_settings

    settings = load_ai_settings()

    assert settings.enabled is False
    assert settings.provider == "aliyun_dashscope"
    assert settings.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.model == "qwen3-vl-plus"
    assert settings.credential_source == "env_then_optional_file"
    assert settings.credential_file is None
    assert settings.timeout_seconds == 300
    assert settings.max_upload_mb == 10
    assert settings.draft_ttl_hours == 24


def test_ai_disabled_configuration_does_not_read_credentials(monkeypatch):
    credential_reads: list[Path] = []

    def fake_read_credentials(path: Path) -> str:
        credential_reads.append(path)
        raise AssertionError("disabled AI must not read credentials")

    monkeypatch.setenv("PROJMAN_AI_ENABLED", "false")
    from app.ai_project import load_ai_settings

    settings = load_ai_settings(read_credentials=fake_read_credentials)

    assert settings.enabled is False
    assert settings.api_key is None
    assert credential_reads == []


def test_ai_api_key_environment_value_takes_priority_over_credential_file(monkeypatch):
    credential_reads: list[Path] = []

    def fake_read_credentials(path: Path) -> str:
        credential_reads.append(path)
        return "file-key-should-not-win"

    monkeypatch.setenv("PROJMAN_AI_ENABLED", "true")
    monkeypatch.setenv("PROJMAN_AI_API_KEY", "env-key-wins")
    monkeypatch.setenv("PROJMAN_AI_CREDENTIAL_FILE", "D:/tmp/fake-credentials.md")
    from app.ai_project import load_ai_settings

    settings = load_ai_settings(read_credentials=fake_read_credentials)

    assert settings.enabled is True
    assert settings.api_key == "env-key-wins"
    assert credential_reads == []


def test_ai_config_does_not_probe_fixed_local_credential_path(monkeypatch):
    credential_reads: list[Path] = []

    def fake_read_credentials(path: Path) -> str:
        credential_reads.append(path)
        return "unexpected-key"

    monkeypatch.setenv("PROJMAN_AI_ENABLED", "true")
    monkeypatch.delenv("PROJMAN_AI_API_KEY", raising=False)
    monkeypatch.delenv("PROJMAN_AI_CREDENTIAL_FILE", raising=False)
    from app.ai_project import load_ai_settings

    settings = load_ai_settings(read_credentials=fake_read_credentials)

    assert settings.enabled is True
    assert settings.api_key is None
    assert credential_reads == []


def test_ai_new_project_page_requires_login(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/ai-projects/new", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ai_new_project_page_shows_supported_input_modes(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.get("/ai-projects/new")

    assert response.status_code == 200
    assert "AI 建档" in response.text
    assert "PDF" in response.text
    assert "图片" in response.text
    assert "文字" in response.text
    assert "开始识别" in response.text
    assert "AI 结果仅供参考" in response.text
    assert "人工确认" in response.text


def test_ai_disabled_page_shows_clear_message_without_api_or_credential_access(tmp_path):
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_credential_reader = lambda path: (_ for _ in ()).throw(
        AssertionError("disabled AI must not read credentials")
    )
    login(client)

    response = client.get("/ai-projects/new")

    assert response.status_code == 200
    assert "AI 功能未启用" in response.text
    assert client.app.state.ai_project_client.calls == []


def test_ai_new_project_page_cleans_expired_pending_drafts(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    login(client)
    ai_input_dir = tmp_path / "data" / "ai_inputs"
    ai_input_dir.mkdir(parents=True)
    old_file = ai_input_dir / "old.pdf"
    old_file.write_bytes(b"%PDF-1.7 old")
    monkeypatch.setenv("PROJMAN_AI_DRAFT_TTL_HOURS", "1")
    with Session(client.app.state.engine) as session:
        draft = AiProjectDraft(
            status="pending",
            input_kind="pdf_text",
            input_filename="old.pdf",
            stored_input_path="ai_inputs/old.pdf",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=2),
        )
        session.add(draft)
        session.commit()

    response = client.get("/ai-projects/new")

    with Session(client.app.state.engine) as session:
        draft = session.exec(select(AiProjectDraft)).one()
    assert response.status_code == 200
    assert draft.status == "abandoned"
    assert not old_file.exists()


def test_ai_disabled_post_rejects_even_with_fake_client(tmp_path):
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "AI 功能未启用" in response.text
    assert fake_ai.calls == []


def test_ai_text_input_creates_reviewable_draft_without_project(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务，预算 5 万，合同金额 4.8 万。"},
        follow_redirects=True,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 200
    assert "项目草稿确认" in response.text
    assert "行政电脑维护服务" in response.text
    assert "确认创建项目" in response.text
    assert projects == []


def test_ai_draft_page_shows_field_evidence_and_confidence(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "请从合同中识别行政电脑维护服务。"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "来源" in response.text
    assert "置信度" in response.text
    assert "项目名称：行政电脑维护服务" in response.text
    assert "合同金额人民币 4.8 万元" in response.text
    assert "高" in response.text
    assert "中" in response.text
    assert "请核对合同金额是否含税" in response.text


def test_ai_draft_page_makes_recognized_fields_clearly_editable(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "请从合同中识别行政电脑维护服务。"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "核对并修改字段" not in response.text
    assert "字段来源与置信度" in response.text
    assert 'name="name"' in response.text
    assert 'value="行政电脑维护服务"' in response.text
    assert 'name="contract_amount"' in response.text
    assert "<td>行政电脑维护服务</td>" not in response.text
    assert "合同金额人民币 4.8 万元" in response.text
    assert "高" in response.text


def test_ai_invalid_fields_are_marked_for_manual_correction_and_block_confirm(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(
        response=ai_invalid_contract_response()
    )
    login(client)

    draft_response = client.post(
        "/ai-projects/drafts",
        data={"text": "请识别合同，但合同字段可能不完整。"},
        follow_redirects=True,
    )

    assert draft_response.status_code == 200
    assert "需人工修正" in draft_response.text
    assert "未识别到明确项目名称" in draft_response.text
    assert "合同金额识别失败" in draft_response.text
    assert "确认创建项目" in draft_response.text
    assert "disabled" in draft_response.text or "不可确认" in draft_response.text


def test_ai_confirmed_draft_creates_project_from_user_edited_values(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]

    response = client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data={
            "year": "2028",
            "name": "行政电脑维护服务（用户修订）",
            "budget_amount": "52000",
            "contract_amount": "49000",
            "contract_start": "2028-01-01",
            "contract_end": "2028-12-31",
            "notes": "用户复核后确认",
            "archive_pdf_kind": "",
        },
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        project = session.exec(select(Project)).one()

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/projects/{project.id}")
    assert project.year == 2028
    assert project.name == "行政电脑维护服务（用户修订）"
    assert project.budget_amount == 52000
    assert project.contract_amount == 49000


def test_ai_confirm_rejects_invalid_contract_period_without_project(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.post(
        "/ai-projects/drafts/draft-1/confirm",
        data={
            "year": "2027",
            "name": "非法合同期项目",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-12-31",
            "contract_end": "2027-01-01",
            "notes": "",
        },
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "合同开始日期不能晚于合同结束日期" in response.text
    assert projects == []


def test_ai_abandoned_draft_cleans_temp_file_and_cannot_be_confirmed(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        files={"file": ("盖章合同.pdf", b"%PDF-1.7 fake", "application/pdf")},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]
    ai_input_dir = tmp_path / "data" / "ai_inputs"
    stored_inputs_before = list(ai_input_dir.glob("**/*"))

    abandon_response = client.post(
        f"/ai-projects/drafts/{draft_id}/abandon",
        follow_redirects=False,
    )
    confirm_response = client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data={
            "year": "2027",
            "name": "行政电脑维护服务",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-01-01",
            "contract_end": "2027-12-31",
            "notes": "",
        },
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert stored_inputs_before
    assert abandon_response.status_code == 303
    assert confirm_response.status_code == 400
    assert "草稿已放弃" in confirm_response.text
    assert projects == []
    assert list(ai_input_dir.glob("**/*")) == []


def test_ai_confirmed_draft_cannot_be_confirmed_twice(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]
    data = {
        "year": "2027",
        "name": "行政电脑维护服务",
        "budget_amount": "50000",
        "contract_amount": "48000",
        "contract_start": "2027-01-01",
        "contract_end": "2027-12-31",
        "notes": "",
    }

    first_response = client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data=data,
        follow_redirects=False,
    )
    second_response = client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data=data,
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert first_response.status_code == 303
    assert second_response.status_code == 400
    assert "草稿已创建项目" in second_response.text
    assert len(projects) == 1


def test_ai_empty_input_is_rejected_without_calling_api(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "   "},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "请至少提供文字或上传文件" in response.text
    assert "上传材料" in response.text
    assert "开始识别" in response.text
    assert fake_ai.calls == []
    assert projects == []


def test_ai_upload_rejects_unsupported_file_without_calling_api(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": ""},
        files={
            "file": (
                "合同.docx",
                b"not supported",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "仅支持 PDF、PNG、JPG、JPEG、WebP" in response.text
    assert "重新选择 PDF / 图片" in response.text
    assert "开始识别" in response.text
    assert fake_ai.calls == []
    assert projects == []


def test_ai_oversized_file_is_rejected_without_calling_api(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    monkeypatch.setenv("PROJMAN_AI_MAX_UPLOAD_MB", "1")
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        files={"file": ("大合同.pdf", b"%PDF-1.7 " + b"x" * 1_100_000, "application/pdf")},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "文件过大" in response.text
    assert "重新选择 PDF / 图片" in response.text
    assert "开始识别" in response.text
    assert fake_ai.calls == []
    assert projects == []


def test_ai_pdf_without_text_falls_back_to_rendered_page_images(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    client.app.state.ai_pdf_text_extractor = lambda content: ""
    client.app.state.ai_pdf_image_renderer = lambda content: [
        {
            "filename": "扫描合同-page-1.png",
            "content": b"\x89PNG\r\n\x1a\nfake-page",
            "content_type": "image/png",
            "page": 1,
        }
    ]
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        files={"file": ("扫描合同.pdf", b"%PDF-1.7 fake scanned", "application/pdf")},
        follow_redirects=True,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 200
    assert "项目草稿确认" in response.text
    assert fake_ai.calls == [
        {
            "text": "请识别 PDF 图片页：扫描合同.pdf",
            "file_bytes": None,
            "filename": "扫描合同.pdf",
            "input_kind": "pdf_image",
            "source_pages": [1],
            "image_files": [
                {
                    "filename": "扫描合同-page-1.png",
                    "content": b"\x89PNG\r\n\x1a\nfake-page",
                    "content_type": "image/png",
                    "page": 1,
                }
            ],
        }
    ]
    assert projects == []


def test_ai_pdf_with_no_text_and_no_rendered_images_returns_hint_without_calling_api(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    client.app.state.ai_pdf_text_extractor = lambda content: ""
    client.app.state.ai_pdf_image_renderer = lambda content: []
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        files={"file": ("扫描合同.pdf", b"%PDF-1.7 fake scanned", "application/pdf")},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "PDF 未提取到可识别文本" in response.text
    assert "无法生成预览图片" in response.text
    assert "重新选择 PDF / 图片" in response.text
    assert "开始识别" in response.text
    assert fake_ai.calls == []
    assert projects == []


def test_ai_pdf_draft_sends_normalized_text_input_to_fake_client(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    fake_ai = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_project_client = fake_ai
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        files={"file": ("盖章合同.pdf", b"%PDF-1.7 fake", "application/pdf")},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert fake_ai.calls == [
        {
            "text": "合同金额人民币 4.8 万元",
            "file_bytes": None,
            "filename": "盖章合同.pdf",
            "input_kind": "pdf_text",
            "source_pages": [],
        }
    ]


def test_ai_pdf_input_can_be_archived_as_selected_project_attachment(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        files={"file": ("盖章合同.pdf", b"%PDF-1.7 fake", "application/pdf")},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]

    client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data={
            "year": "2027",
            "name": "行政电脑维护服务",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-01-01",
            "contract_end": "2027-12-31",
            "notes": "",
            "archive_pdf_kind": "signed_contract",
        },
    )
    with Session(client.app.state.engine) as session:
        project = session.exec(select(Project)).one()
        attachment = session.exec(select(Attachment)).one()

    assert project.name == "行政电脑维护服务"
    assert attachment.project_id == project.id
    assert attachment.kind.value == "signed_contract"
    assert attachment.original_filename == "盖章合同.pdf"


def test_ai_confirm_archive_failure_does_not_create_half_project(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    client.app.state.ai_pdf_text_extractor = lambda content: "合同金额人民币 4.8 万元"
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        files={"file": ("伪造合同.pdf", b"not really a pdf", "application/pdf")},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]

    response = client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data={
            "year": "2027",
            "name": "行政电脑维护服务",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-01-01",
            "contract_end": "2027-12-31",
            "notes": "",
            "archive_pdf_kind": "signed_contract",
        },
        follow_redirects=False,
    )

    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()
        attachments = session.exec(select(Attachment)).all()
        draft = session.get(AiProjectDraft, int(draft_id))

    assert response.status_code == 400
    assert "只支持上传 PDF 文件" in response.text
    assert projects == []
    assert attachments == []
    assert draft.status == "pending"
    assert draft.project_id is None
    assert (tmp_path / "data" / draft.stored_input_path).exists()


def test_ai_image_input_is_not_archived_as_formal_attachment_by_default(
    tmp_path,
    monkeypatch,
):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    login(client)
    draft_response = client.post(
        "/ai-projects/drafts",
        files={"file": ("合同截图.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        follow_redirects=False,
    )
    draft_id = draft_response.headers["location"].rstrip("/").split("/")[-1]

    client.post(
        f"/ai-projects/drafts/{draft_id}/confirm",
        data={
            "year": "2027",
            "name": "行政电脑维护服务",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-01-01",
            "contract_end": "2027-12-31",
            "notes": "",
        },
    )
    with Session(client.app.state.engine) as session:
        project = session.exec(select(Project)).one()
        attachments = session.exec(select(Attachment)).all()

    assert project.name == "行政电脑维护服务"
    assert attachments == []


def test_ai_api_failure_returns_controlled_error_without_project(tmp_path, monkeypatch):
    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(error=TimeoutError("timed out"))
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 200
    assert "识别失败" in response.text
    assert "可重试" in response.text
    assert "手工新建" in response.text
    assert projects == []


def test_ai_api_failure_can_show_actionable_client_error_without_project(
    tmp_path,
    monkeypatch,
):
    from app.ai_project import AiProjectClientError

    enable_ai(monkeypatch)
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(
        error=AiProjectClientError("AI 服务拒绝了本次识别请求，请压缩 PDF 后重试")
    )
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 200
    assert "AI 服务拒绝了本次识别请求" in response.text
    assert "请压缩 PDF 后重试" in response.text
    assert projects == []


def test_ai_enabled_without_api_key_returns_configuration_error_without_draft(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    client.app.state.ai_project_client = FakeAiProjectClient(response=ai_contract_response())
    monkeypatch.setenv("PROJMAN_AI_ENABLED", "true")
    monkeypatch.delenv("PROJMAN_AI_API_KEY", raising=False)
    monkeypatch.delenv("PROJMAN_AI_CREDENTIAL_FILE", raising=False)
    login(client)

    response = client.post(
        "/ai-projects/drafts",
        data={"text": "2027 年行政电脑维护服务。"},
        follow_redirects=False,
    )
    with Session(client.app.state.engine) as session:
        projects = session.exec(select(Project)).all()

    assert response.status_code == 400
    assert "AI 功能已启用，但未配置 API key" in response.text
    assert client.app.state.ai_project_client.calls == []
    assert projects == []


def test_ai_log_redaction_hides_api_key_and_full_input():
    from app.ai_project import redact_ai_log

    redacted = redact_ai_log(
        {
            "api_key": "sk-should-not-appear",
            "input_text": "合同全文：" + "敏感内容" * 200,
            "provider": "aliyun_dashscope",
            "model": "qwen3-vl-plus",
            "success": False,
            "error_type": "timeout",
        }
    )

    assert "sk-should-not-appear" not in redacted
    assert "敏感内容敏感内容敏感内容" not in redacted
    assert "aliyun_dashscope" in redacted
    assert "qwen3-vl-plus" in redacted
    assert "timeout" in redacted


def test_extract_pdf_text_returns_empty_when_pdf_parsing_fails():
    from app.ai_project import extract_pdf_text

    assert extract_pdf_text(b"not really a pdf") == ""


def test_render_pdf_pages_converts_pdf_pages_to_compressed_jpeg_images():
    import fitz

    from app.ai_project import render_pdf_pages

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "contract scan page")
    content = document.tobytes()
    document.close()

    images = render_pdf_pages(content, max_pages=1, zoom=1.0)

    assert len(images) == 1
    assert images[0]["page"] == 1
    assert images[0]["content_type"] == "image/jpeg"
    assert images[0]["filename"] == "pdf-page-1.jpg"
    assert images[0]["content"].startswith(b"\xff\xd8\xff")


def test_ai_new_project_page_shows_submit_progress_indicator(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.get("/ai-projects/new")

    assert response.status_code == 200
    assert 'data-ai-draft-form' in response.text
    assert 'data-ai-async-form' in response.text
    assert 'class="ai-progress"' in response.text
    assert 'role="progressbar"' in response.text
    assert 'data-ai-progress-fill' in response.text
    assert 'data-ai-progress-percent' in response.text
    assert "0%" in response.text
    assert "正在识别" in response.text
    assert "上传材料" in response.text
    assert "解析 PDF" in response.text
    assert "调用 AI 识别" in response.text
    assert "生成草稿" in response.text


def test_ai_new_project_submit_script_uses_stage_progress_not_fetch_only():
    script = Path("app/static/app.js").read_text(encoding="utf-8")

    assert "XMLHttpRequest" in script
    assert "xhr.upload.onprogress" in script
    assert "requestAnimationFrame" in script
    assert "data-ai-progress-percent" in script
    assert "window.fetch(form.action" not in script


def test_ai_test_plan_documents_no_real_external_api_calls():
    plan = Path("docs/AI新建项目测试用例计划.md").read_text(encoding="utf-8")
    requirement = Path("docs/AI新建项目需求文档.md").read_text(encoding="utf-8")

    assert "不得真实调用外部 AI/API" in plan
    assert "fake client" in plan
    assert "qwen3-vl-plus" in plan
    assert "PROJMAN_AI_CREDENTIAL_FILE" in requirement
    assert ".credentials.md" not in requirement
    assert ".credentials.md" not in plan
    assert "阿里云百炼 DashScope" in requirement
    assert "测试也不得读取真实凭据文件" in requirement


def test_env_example_documents_ai_configuration_without_secret_value():
    env_example = Path(".env.example").read_text(encoding="utf-8")

    for key in [
        "PROJMAN_AI_ENABLED",
        "PROJMAN_AI_PROVIDER",
        "PROJMAN_AI_BASE_URL",
        "PROJMAN_AI_MODEL",
        "PROJMAN_AI_API_KEY",
        "PROJMAN_AI_CREDENTIAL_SOURCE",
        "PROJMAN_AI_CREDENTIAL_FILE",
        "PROJMAN_AI_PROXY",
        "PROJMAN_AI_TIMEOUT_SECONDS",
        "PROJMAN_AI_MAX_UPLOAD_MB",
        "PROJMAN_AI_DRAFT_TTL_HOURS",
    ]:
        assert key in env_example

    assert "aliyun_dashscope" in env_example
    assert "qwen3-vl-plus" in env_example
    assert "https://dashscope.aliyuncs.com/compatible-mode/v1" in env_example
    assert "PROJMAN_AI_CREDENTIAL_SOURCE=env_then_optional_file" in env_example
    assert "PROJMAN_AI_CREDENTIAL_FILE=\n" in env_example
    assert "PROJMAN_AI_MAX_UPLOAD_MB=10" in env_example
    assert "PROJMAN_AI_DRAFT_TTL_HOURS=24" in env_example
    assert "sk-" not in env_example.lower()
    assert "change-this" not in env_example.split("PROJMAN_AI_API_KEY=", maxsplit=1)[1]
