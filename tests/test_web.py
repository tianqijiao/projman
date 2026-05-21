from pathlib import Path
from zipfile import ZipFile

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


def test_project_list_is_first_navigation_item(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.get("/projects")

    assert response.status_code == 200
    assert response.text.index('href="/projects">项目台账') < response.text.index(
        'href="/">年度看板'
    )


def test_new_project_is_not_in_top_navigation_but_kept_in_toolbar(tmp_path):
    client = make_client(tmp_path)
    login(client)

    response = client.get("/projects")

    assert response.status_code == 200
    header_html = response.text.split("</header>", maxsplit=1)[0]
    assert 'href="/projects/new">新增项目' not in header_html
    assert 'href="/projects/new">新增项目' in response.text


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


def test_project_list_shows_all_attachment_types_and_download_links(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "行政电脑维护服务", "budget_amount": "50000"},
    )
    client.post(
        "/projects/1/attachments",
        data={"kind": "signed_contract"},
        files={"file": ("盖章合同.pdf", b"%PDF-1.7 fake", "application/pdf")},
    )
    client.post(
        "/projects/1/attachments",
        data={"kind": "invoice"},
        files={"file": ("发票.pdf", b"%PDF-1.7 fake", "application/pdf")},
    )

    response = client.get("/projects")

    assert response.status_code == 200
    assert "采购依据" in response.text
    assert "合同审签 PDF" in response.text
    assert "盖章合同扫描件" in response.text
    assert "验收单" in response.text
    assert "发票" in response.text
    assert "盖章合同.pdf" in response.text
    assert "发票.pdf" in response.text
    assert 'href="/attachments/1/preview"' in response.text
    assert 'href="/attachments/1/download"' in response.text
    assert 'href="/projects/1/attachments/download-all"' in response.text
    assert '<details class="attachment-type" open>' not in response.text


def test_attachment_preview_download_and_download_all(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "行政电脑维护服务", "budget_amount": "50000"},
    )
    client.post(
        "/projects/1/attachments",
        data={"kind": "procurement_basis"},
        files={"file": ("采购依据.pdf", b"%PDF-1.7 fake basis", "application/pdf")},
    )
    client.post(
        "/projects/1/attachments",
        data={"kind": "invoice"},
        files={"file": ("发票.pdf", b"%PDF-1.7 fake invoice", "application/pdf")},
    )

    preview_response = client.get("/attachments/1/preview")
    inline_response = client.get("/attachments/1/file")
    download_response = client.get("/attachments/1/download")
    all_response = client.get("/projects/1/attachments/download-all")

    assert preview_response.status_code == 200
    assert "采购依据.pdf" in preview_response.text
    assert 'src="/attachments/1/file"' in preview_response.text
    assert 'href="/attachments/1/download"' in preview_response.text
    assert inline_response.headers["content-disposition"].startswith("inline;")
    assert download_response.headers["content-disposition"].startswith("attachment;")
    assert all_response.status_code == 200
    assert all_response.headers["content-type"].startswith("application/zip")
    assert "project-1-attachments.zip" in all_response.headers["content-disposition"]

    zip_path = tmp_path / "attachments.zip"
    zip_path.write_bytes(all_response.content)
    with ZipFile(zip_path) as attachments_zip:
        names = attachments_zip.namelist()
        assert any(name.endswith("采购依据.pdf") for name in names)
        assert any(name.endswith("发票.pdf") for name in names)


def test_project_list_can_filter_by_year(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "2027 年项目", "budget_amount": "100"},
    )
    client.post(
        "/projects",
        data={"year": "2028", "name": "2028 年项目", "budget_amount": "200"},
    )

    all_response = client.get("/projects")
    filtered_response = client.get("/projects?year=2027")

    assert all_response.status_code == 200
    assert 'name="year"' in all_response.text
    assert '<option value="">全部年度</option>' in all_response.text
    assert '<option value="2027"' in all_response.text
    assert '<option value="2028"' in all_response.text
    assert "2027 年项目" in filtered_response.text
    assert "2028 年项目" not in filtered_response.text
    assert '<option value="2027" selected>2027 年</option>' in filtered_response.text


def test_project_list_exports_filtered_year_to_excel(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "2027 年项目", "budget_amount": "100"},
    )
    client.post(
        "/projects",
        data={"year": "2028", "name": "2028 年项目", "budget_amount": "200"},
    )

    page_response = client.get("/projects?year=2027")
    export_response = client.get("/projects/export?year=2027")

    assert page_response.status_code == 200
    assert 'href="/projects/export?year=2027"' in page_response.text
    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "projects-2027.xlsx" in export_response.headers["content-disposition"]

    workbook_path = tmp_path / "export.xlsx"
    workbook_path.write_bytes(export_response.content)
    with ZipFile(workbook_path) as workbook_zip:
        workbook_xml = "\n".join(
            workbook_zip.read(name).decode("utf-8", errors="ignore")
            for name in workbook_zip.namelist()
            if name.startswith("xl/")
            and name.endswith(".xml")
            and ("worksheet" in name or "sharedStrings" in name)
        )
    assert "2027 年项目" in workbook_xml
    assert "2028 年项目" not in workbook_xml


def test_project_excel_export_omits_attachment_columns(tmp_path):
    client = make_client(tmp_path)
    login(client)
    client.post(
        "/projects",
        data={"year": "2027", "name": "2027 年项目", "budget_amount": "100"},
    )
    client.post(
        "/projects/1/attachments",
        data={"kind": "procurement_basis"},
        files={"file": ("采购依据.pdf", b"%PDF-1.7 fake basis", "application/pdf")},
    )

    export_response = client.get("/projects/export?year=2027")

    workbook_path = tmp_path / "export.xlsx"
    workbook_path.write_bytes(export_response.content)
    with ZipFile(workbook_path) as workbook_zip:
        workbook_xml = "\n".join(
            workbook_zip.read(name).decode("utf-8", errors="ignore")
            for name in workbook_zip.namelist()
            if name.startswith("xl/")
            and name.endswith(".xml")
            and ("worksheet" in name or "sharedStrings" in name)
        )
    assert "采购依据.pdf" not in workbook_xml
    assert "合同审签 PDF" not in workbook_xml
    assert "其他附件" not in workbook_xml
