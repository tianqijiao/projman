from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


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


def test_dashboard_requires_login(tmp_path):
    client = make_client(tmp_path)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_create_project_and_show_it_on_dashboard(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.post(
        "/projects",
        data={"year": "2027", "name": "行政电脑维护服务", "budget_amount": "50000"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "行政电脑维护服务" in response.text
    assert "未签合同" in response.text


def test_edit_project_and_upload_pdf_attachment(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "行政电脑维护服务", "budget_amount": "50000"},
    )

    edit_response = client.post(
        "/projects/1/edit",
        data={
            "year": "2027",
            "name": "行政电脑维护服务",
            "budget_amount": "50000",
            "contract_amount": "48000",
            "contract_start": "2027-01-01",
            "contract_end": "2027-12-31",
            "acceptance_date": "",
            "payment_date": "",
            "notes": "测试备注",
        },
        follow_redirects=True,
    )
    upload_response = client.post(
        "/projects/1/attachments",
        data={"kind": "signed_contract"},
        files={"file": ("合同.pdf", b"%PDF-1.7 fake", "application/pdf")},
        follow_redirects=True,
    )

    assert edit_response.status_code == 200
    assert upload_response.status_code == 200
    assert "合同已签" in upload_response.text
    assert "合同.pdf" in upload_response.text
