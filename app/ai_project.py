from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
import base64
import json
import math
import mimetypes
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib import request as urlrequest
from uuid import uuid4

from sqlmodel import Session
from sqlmodel import select

from app.models import AiProjectDraft


DEFAULT_AI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SUPPORTED_AI_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_PDF_RENDER_MAX_PAGES = 3
DEFAULT_PDF_RENDER_ZOOM = 1.25
PROJECT_FIELD_KEYS = [
    "year",
    "name",
    "budget_amount",
    "contract_amount",
    "contract_start",
    "contract_end",
    "notes",
]
FIELD_LABELS = {
    "year": "年度",
    "name": "项目名称",
    "budget_amount": "预算金额",
    "contract_amount": "合同金额",
    "contract_start": "合同开始日期",
    "contract_end": "合同结束日期",
    "notes": "备注",
}


class AiProjectClientError(RuntimeError):
    """用户可理解的 AI 调用错误，避免把底层异常或密钥相关信息带到页面。"""


@dataclass(frozen=True)
class AiSettings:
    enabled: bool
    provider: str
    base_url: str
    model: str
    api_key: str | None
    credential_source: str
    credential_file: Path | None
    proxy: str | None
    timeout_seconds: int
    max_upload_mb: int
    draft_ttl_hours: int


class DashScopeAiProjectClient:
    def __init__(self, settings: AiSettings):
        self.settings = settings

    def extract_project(
        self,
        *,
        text: str = "",
        file_bytes: bytes | None = None,
        image_files: list[Mapping[str, Any]] | None = None,
        filename: str = "",
        input_kind: str = "text",
        source_pages: list[int] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.api_key:
            raise ValueError("AI 功能已启用，但未配置 API key")
        messages = [
            {
                "role": "system",
                "content": (
                    "你是运维项目管理助手。请从用户提供的合同、截图或说明中抽取项目建档字段，"
                    "只返回 JSON，对应格式为 {\"fields\": {字段名: {\"value\": 值, "
                    "\"source\": 来源, \"evidence\": 证据摘录, \"confidence\": 高/中/低}}, "
                    "\"risk_tips\": [提示]}。字段名限定为 year, name, budget_amount, "
                    "contract_amount, contract_start, contract_end, notes。"
                ),
            }
        ]
        user_content: str | list[dict[str, Any]]
        if image_files:
            user_content = [
                {"type": "text", "text": text or f"请识别文件：{filename}"},
            ]
            for image_file in image_files:
                image_bytes = image_file.get("content")
                if not isinstance(image_bytes, bytes):
                    continue
                image_filename = str(image_file.get("filename") or filename)
                mime_type = (
                    str(image_file.get("content_type") or "")
                    or mimetypes.guess_type(image_filename)[0]
                    or "image/png"
                )
                encoded = base64.b64encode(image_bytes).decode("ascii")
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    }
                )
        elif file_bytes is None:
            user_content = text
        else:
            mime_type = mimetypes.guess_type(filename)[0] or "image/png"
            encoded = base64.b64encode(file_bytes).decode("ascii")
            user_content = [
                {"type": "text", "text": text or f"请识别文件：{filename}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                },
            ]
        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "metadata": {
                "input_kind": input_kind,
                "filename": filename,
                "source_pages": source_pages or [],
            },
        }
        endpoint = self.settings.base_url.rstrip("/") + "/chat/completions"
        req = urlrequest.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urlrequest.build_opener(
            urlrequest.ProxyHandler(
                {"http": self.settings.proxy, "https": self.settings.proxy}
            )
            if self.settings.proxy
            else urlrequest.ProxyHandler({})
        )
        try:
            with opener.open(req, timeout=self.settings.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise AiProjectClientError(_describe_http_error(exc)) from exc
        except TimeoutError as exc:
            raise AiProjectClientError("AI 服务响应超时，请稍后重试或检查网络。") from exc
        except URLError as exc:
            raise AiProjectClientError("连接 AI 服务失败，请检查网络、代理或 Base URL。") from exc
        except json.JSONDecodeError as exc:
            raise AiProjectClientError("AI 服务返回内容无法解析，请稍后重试。") from exc

        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, str):
                return json.loads(content)
            if isinstance(content, Mapping):
                return dict(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AiProjectClientError("AI 返回内容格式异常，请重试或手工新建。") from exc
        raise AiProjectClientError("AI 返回内容格式异常，请重试或手工新建。")


def load_ai_settings(
    *,
    environ: Mapping[str, str] | None = None,
    read_credentials: Callable[[Path], str] | None = None,
) -> AiSettings:
    env = os.environ if environ is None else environ
    enabled = _env_bool(env.get("PROJMAN_AI_ENABLED"), default=False)
    provider = env.get("PROJMAN_AI_PROVIDER", "aliyun_dashscope").strip()
    base_url = env.get("PROJMAN_AI_BASE_URL", DEFAULT_AI_BASE_URL).strip()
    model = env.get("PROJMAN_AI_MODEL", "qwen3-vl-plus").strip()
    credential_source = env.get(
        "PROJMAN_AI_CREDENTIAL_SOURCE",
        "env_then_optional_file",
    ).strip()
    credential_file_value = env.get("PROJMAN_AI_CREDENTIAL_FILE", "").strip()
    credential_file = Path(credential_file_value) if credential_file_value else None
    api_key = _blank_to_none(env.get("PROJMAN_AI_API_KEY", ""))
    if enabled and api_key is None and credential_file is not None:
        reader = read_credentials or read_credential_file
        api_key = _blank_to_none(reader(credential_file))
    return AiSettings(
        enabled=enabled,
        provider=provider or "aliyun_dashscope",
        base_url=base_url or DEFAULT_AI_BASE_URL,
        model=model or "qwen3-vl-plus",
        api_key=api_key,
        credential_source=credential_source or "env_then_optional_file",
        credential_file=credential_file,
        proxy=_blank_to_none(env.get("PROJMAN_AI_PROXY", "")),
        timeout_seconds=_env_int(env.get("PROJMAN_AI_TIMEOUT_SECONDS"), 300),
        max_upload_mb=_env_int(env.get("PROJMAN_AI_MAX_UPLOAD_MB"), 10),
        draft_ttl_hours=_env_int(env.get("PROJMAN_AI_DRAFT_TTL_HOURS"), 24),
    )


def read_credential_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    for pattern in [
        r"PROJMAN_AI_API_KEY\s*=\s*([^\s]+)",
        r"DASHSCOPE_API_KEY\s*=\s*([^\s]+)",
        r"api[_ -]?key\s*[:=]\s*([^\s]+)",
    ]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def redact_ai_log(data: Mapping[str, Any]) -> str:
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if "key" in lowered or "secret" in lowered or "token" in lowered:
            redacted[key] = "[redacted]"
        elif lowered in {"input_text", "text", "prompt", "file_bytes"}:
            redacted[key] = f"[redacted length={len(str(value))}]"
        else:
            redacted[key] = value
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _describe_http_error(exc: HTTPError) -> str:
    status = exc.code
    if status in {401, 403}:
        return "AI 服务鉴权失败，请在设置页检查 API Key 或模型权限。"
    if status == 413:
        return "AI 服务拒绝了本次识别请求，可能是 PDF 页面图片过大，请压缩 PDF 或减少页数后重试。"
    if status == 429:
        return "AI 服务请求过于频繁或额度不足，请稍后重试或检查账号额度。"
    if 400 <= status < 500:
        return "AI 服务拒绝了本次识别请求，可能是 PDF 页面图片过大或格式不被支持，请压缩 PDF 后重试。"
    if status >= 500:
        return "AI 服务暂时不可用，请稍后重试。"
    return "AI 服务调用失败，请稍后重试。"


def extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception:
        return ""


def render_pdf_pages(
    content: bytes,
    *,
    max_pages: int = DEFAULT_PDF_RENDER_MAX_PAGES,
    zoom: float = DEFAULT_PDF_RENDER_ZOOM,
) -> list[dict[str, Any]]:
    try:
        import fitz

        document = fitz.open(stream=content, filetype="pdf")
        page_count = min(max_pages, len(document))
        matrix = fitz.Matrix(zoom, zoom)
        images: list[dict[str, Any]] = []
        for page_index in range(page_count):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            page_number = page_index + 1
            images.append(
                {
                    "filename": f"pdf-page-{page_number}.jpg",
                    "content": pixmap.tobytes("jpeg"),
                    "content_type": "image/jpeg",
                    "page": page_number,
                }
            )
        document.close()
        return images
    except Exception:
        return []


def store_ai_input_file(
    data_dir: Path,
    *,
    filename: str,
    content: bytes,
) -> str:
    safe_name = Path(filename).name.replace("/", "_").replace("\\", "_")
    stored_name = f"{uuid4().hex}-{safe_name or 'ai-input'}"
    relative_path = Path("ai_inputs") / stored_name
    absolute_path = data_dir / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(content)
    return relative_path.as_posix()


def create_ai_draft(
    session: Session,
    *,
    input_kind: str,
    input_filename: str = "",
    input_content_type: str = "",
    stored_input_path: str = "",
    input_text: str = "",
    ai_response: Mapping[str, Any],
) -> AiProjectDraft:
    fields, has_errors = normalize_ai_fields(ai_response)
    risk_tips = ai_response.get("risk_tips") or []
    if not isinstance(risk_tips, list):
        risk_tips = [str(risk_tips)]
    draft = AiProjectDraft(
        status="pending",
        input_kind=input_kind,
        input_filename=input_filename,
        input_content_type=input_content_type,
        stored_input_path=stored_input_path,
        input_text_summary=input_text.strip()[:500],
        raw_response_json=json.dumps(ai_response, ensure_ascii=False),
        fields_json=json.dumps(fields, ensure_ascii=False),
        risk_tips_json=json.dumps(risk_tips, ensure_ascii=False),
        has_validation_errors=has_errors,
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def normalize_ai_fields(ai_response: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    raw_fields = ai_response.get("fields") or {}
    if not isinstance(raw_fields, Mapping):
        raw_fields = {}
    field_items: list[dict[str, Any]] = []
    parsed_dates: dict[str, date | None] = {}
    has_errors = False
    for key in PROJECT_FIELD_KEYS:
        raw_item = raw_fields.get(key) or {}
        if not isinstance(raw_item, Mapping):
            raw_item = {"value": raw_item}
        value = raw_item.get("value", "")
        valid, error, parsed_date = _validate_ai_field(key, value)
        if parsed_date is not None:
            parsed_dates[key] = parsed_date
        if not valid:
            has_errors = True
        field_items.append(
            {
                "key": key,
                "label": FIELD_LABELS[key],
                "value": "" if value is None else str(value),
                "source": str(raw_item.get("source") or ""),
                "evidence": str(raw_item.get("evidence") or ""),
                "confidence": str(raw_item.get("confidence") or "中"),
                "valid": valid,
                "error": error,
            }
        )
    start = parsed_dates.get("contract_start")
    end = parsed_dates.get("contract_end")
    if start is not None and end is not None and start > end:
        has_errors = True
        for item in field_items:
            if item["key"] in {"contract_start", "contract_end"}:
                item["valid"] = False
                item["error"] = "合同开始日期不能晚于合同结束日期"
    return field_items, has_errors


def draft_fields(draft: AiProjectDraft) -> list[dict[str, Any]]:
    return json.loads(draft.fields_json or "[]")


def draft_risk_tips(draft: AiProjectDraft) -> list[str]:
    return json.loads(draft.risk_tips_json or "[]")


def draft_field_value(draft: AiProjectDraft, key: str) -> str:
    for field in draft_fields(draft):
        if field.get("key") == key:
            return str(field.get("value") or "")
    return ""


def mark_draft_abandoned(session: Session, draft: AiProjectDraft) -> AiProjectDraft:
    draft.status = "abandoned"
    draft.updated_at = datetime.now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def mark_draft_created(
    session: Session,
    draft: AiProjectDraft,
    *,
    project_id: int,
) -> AiProjectDraft:
    draft.status = "created"
    draft.project_id = project_id
    draft.updated_at = datetime.now()
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def cleanup_ai_input_file(data_dir: Path, stored_path: str) -> None:
    if not stored_path:
        return
    base_dir = data_dir.resolve()
    file_path = (data_dir / stored_path).resolve()
    try:
        file_path.relative_to(base_dir)
    except ValueError:
        return
    if file_path.is_file():
        file_path.unlink()
    ai_input_dir = (data_dir / "ai_inputs").resolve()
    parent = file_path.parent
    while parent != ai_input_dir and parent != base_dir and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def cleanup_expired_ai_drafts(
    session: Session,
    *,
    data_dir: Path,
    ttl_hours: int,
) -> int:
    cutoff = datetime.now() - timedelta(hours=ttl_hours)
    drafts = list(
        session.exec(
            select(AiProjectDraft).where(
                AiProjectDraft.status == "pending",
                AiProjectDraft.created_at < cutoff,
            )
        )
    )
    for draft in drafts:
        cleanup_ai_input_file(data_dir, draft.stored_input_path)
        draft.status = "abandoned"
        draft.updated_at = datetime.now()
        session.add(draft)
    if drafts:
        session.commit()
    return len(drafts)


def _validate_ai_field(key: str, value: Any) -> tuple[bool, str, date | None]:
    if key == "name":
        return (bool(str(value or "").strip()), "项目名称不能为空", None)
    if key == "year":
        if str(value or "").strip() == "":
            return True, "", None
        try:
            int(str(value).strip())
            return True, "", None
        except ValueError:
            return False, "年度格式不正确", None
    if key in {"budget_amount", "contract_amount"}:
        if str(value or "").strip() == "":
            return True, "", None
        try:
            parsed = float(str(value).strip())
        except ValueError:
            return False, f"{FIELD_LABELS[key]}格式不正确", None
        return (
            math.isfinite(parsed),
            "" if math.isfinite(parsed) else f"{FIELD_LABELS[key]}格式不正确",
            None,
        )
    if key in {"contract_start", "contract_end"}:
        if str(value or "").strip() == "":
            return True, "", None
        try:
            parsed_date = date.fromisoformat(str(value).strip())
            return True, "", parsed_date
        except ValueError:
            return False, f"{FIELD_LABELS[key]}格式不正确", None
    return True, "", None


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(value: str | None, default: int) -> int:
    try:
        parsed = int(str(value).strip()) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
