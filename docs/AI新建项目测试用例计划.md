# AI 辅助新建项目测试用例计划

日期：2026-05-25  
依据：[AI 新建项目需求文档](AI新建项目需求文档.md)

## 1. 编写原则

- AI 建档测试已从自动化规格用例转为正式回归用例；后续若发现新缺口，才使用严格 `xfail` 标记并写明退出条件。
- 测试不得真实调用外部 AI/API，不读取真实凭据文件，统一使用 fake client 或 mock 响应。
- 文件输入使用 `tmp_path` 和临时 SQLite，不污染真实 `data/app.db`、`data/ai_inputs/` 或附件目录。
- PDF 失败路径使用损坏 PDF 字节验证解析失败；PDF 成功路径如需使用最小 PDF 字节，必须注入 fake PDF extractor，不依赖损坏 PDF 的字节回退。
- PDF MVP 以“先本地提取文本，空文本时渲染页面图片后传给 fake client”为契约；加密、损坏或无法渲染的 PDF 不调用 AI。
- Web 行为仍使用 FastAPI `TestClient` 和 HTML 响应断言，不为当前 MVP 引入 Playwright。

## 2. 覆盖矩阵

| 需求编号 | 需求点 | 测试文件 | 代表测试 | 当前状态 |
| --- | --- | --- | --- | --- |
| R-AI-CONFIG-01 | 默认使用阿里云 DashScope 和 `qwen3-vl-plus` | `tests/test_ai_project.py` | `test_ai_config_defaults_to_aliyun_dashscope_qwen3_vl_plus` | 已覆盖 |
| R-AI-CONFIG-02 | AI 关闭时不读取凭据文件 | `tests/test_ai_project.py` | `test_ai_disabled_configuration_does_not_read_credentials` | 已覆盖 |
| R-AI-CONFIG-03 | `.env.example` 记录 AI 配置但不包含真实密钥 | `tests/test_ai_project.py` | `test_env_example_documents_ai_configuration_without_secret_value` | 已覆盖 |
| R-AI-CONFIG-04 | 环境变量 API key 优先于可选凭据文件 | `tests/test_ai_project.py` | `test_ai_api_key_environment_value_takes_priority_over_credential_file` | 已覆盖 |
| R-AI-CONFIG-05 | AI 启用但缺少 key 时返回中文配置错误 | `tests/test_ai_project.py` | `test_ai_enabled_without_api_key_returns_configuration_error_without_draft` | 已覆盖 |
| R-AI-CONFIG-06 | 未配置凭据文件时不访问固定本机路径 | `tests/test_ai_project.py` | `test_ai_config_does_not_probe_fixed_local_credential_path` | 已覆盖 |
| R-AI-CONFIG-07 | AI 关闭时 POST 识别硬性阻断，即使注入 fake client 也不调用 | `tests/test_ai_project.py` | `test_ai_disabled_post_rejects_even_with_fake_client` | 已覆盖 |
| R-AI-SETTING-01 | 设置页可维护 AI 开关和 `.env` 配置，并按本地工具偏好明文显示 API key | `tests/test_web.py` | `test_settings_page_shows_ai_controls_with_plaintext_api_key`、`test_settings_can_enable_ai_and_write_local_env` | 已覆盖 |
| R-AI-SETTING-02 | 仅由进程环境变量提供的 API key 不会被空表单覆盖为空 | `tests/test_web.py` | `test_settings_keeps_process_api_key_when_enabled_form_leaves_key_blank` | 已覆盖 |
| R-AI-AUTH-01 | 未登录访问 AI 建档页跳转登录 | `tests/test_ai_project.py` | `test_ai_new_project_page_requires_login` | 已覆盖 |
| R-AI-UI-01 | 登录后可见 PDF、图片、文字输入入口和人工确认提示 | `tests/test_ai_project.py` | `test_ai_new_project_page_shows_supported_input_modes` | 已覆盖 |
| R-AI-UI-02 | AI 关闭时入口隐藏或显示禁用提示 | `tests/test_ai_project.py` | `test_ai_disabled_page_shows_clear_message_without_api_or_credential_access` | 已覆盖 |
| R-AI-UI-03 | 提交识别后页面提供识别中进度提示 | `tests/test_ai_project.py` | `test_ai_new_project_page_shows_submit_progress_indicator` | 已覆盖 |
| R-AI-UI-04 | 识别提交使用可推进的阶段进度和百分比，不退回纯等待提交 | `tests/test_ai_project.py` | `test_ai_new_project_submit_script_uses_stage_progress_not_fetch_only` | 已覆盖 |
| R-AI-DRAFT-01 | 文本识别只生成草稿，不直接创建正式项目 | `tests/test_ai_project.py` | `test_ai_text_input_creates_reviewable_draft_without_project` | 已覆盖 |
| R-AI-DRAFT-02 | 草稿页展示字段来源、证据和置信度 | `tests/test_ai_project.py` | `test_ai_draft_page_shows_field_evidence_and_confidence` | 已覆盖 |
| R-AI-DRAFT-03 | AI 返回非法字段时草稿标记需人工修正 | `tests/test_ai_project.py` | `test_ai_invalid_fields_are_marked_for_manual_correction_and_block_confirm` | 已覆盖 |
| R-AI-DRAFT-04 | 访问 AI 建档页时清理过期待确认草稿和临时文件 | `tests/test_ai_project.py` | `test_ai_new_project_page_cleans_expired_pending_drafts` | 已覆盖 |
| R-AI-DRAFT-05 | 草稿页把识别字段明确展示为可编辑复核表单 | `tests/test_ai_project.py` | `test_ai_draft_page_makes_recognized_fields_clearly_editable` | 已覆盖 |
| R-AI-CONFIRM-01 | 用户确认后按编辑后的字段创建项目 | `tests/test_ai_project.py` | `test_ai_confirmed_draft_creates_project_from_user_edited_values` | 已覆盖 |
| R-AI-CONFIRM-02 | 草稿非法合同期不能确认创建 | `tests/test_ai_project.py` | `test_ai_confirm_rejects_invalid_contract_period_without_project` | 已覆盖 |
| R-AI-CONFIRM-03 | 放弃草稿后不创建项目且不能再次确认 | `tests/test_ai_project.py` | `test_ai_abandoned_draft_cleans_temp_file_and_cannot_be_confirmed` | 已覆盖 |
| R-AI-CONFIRM-04 | 已创建草稿不能重复确认 | `tests/test_ai_project.py` | `test_ai_confirmed_draft_cannot_be_confirmed_twice` | 已覆盖 |
| R-AI-FILE-00 | 空文本且无文件时不调用 AI | `tests/test_ai_project.py` | `test_ai_empty_input_is_rejected_without_calling_api` | 已覆盖 |
| R-AI-FILE-01 | 不支持的文件不调用 AI/API | `tests/test_ai_project.py` | `test_ai_upload_rejects_unsupported_file_without_calling_api` | 已覆盖 |
| R-AI-FILE-02 | PDF 输入可由用户选择归档为项目附件 | `tests/test_ai_project.py` | `test_ai_pdf_input_can_be_archived_as_selected_project_attachment` | 已覆盖 |
| R-AI-FILE-03 | 图片输入默认不成为正式项目附件 | `tests/test_ai_project.py` | `test_ai_image_input_is_not_archived_as_formal_attachment_by_default` | 已覆盖 |
| R-AI-FILE-04 | 超过大小限制的文件不调用 AI | `tests/test_ai_project.py` | `test_ai_oversized_file_is_rejected_without_calling_api` | 已覆盖 |
| R-AI-FILE-05 | PDF 归档校验失败时不得创建半成功项目 | `tests/test_ai_project.py` | `test_ai_confirm_archive_failure_does_not_create_half_project` | 已覆盖 |
| R-AI-PDF-01 | PDF 文本提取为空时自动渲染页面图片交给视觉模型识别 | `tests/test_ai_project.py` | `test_ai_pdf_without_text_falls_back_to_rendered_page_images` | 已覆盖 |
| R-AI-PDF-02 | fake client 接收规范化后的 PDF 文本输入 | `tests/test_ai_project.py` | `test_ai_pdf_draft_sends_normalized_text_input_to_fake_client` | 已覆盖 |
| R-AI-PDF-03 | PDF 解析异常不回退读取原始字节 | `tests/test_ai_project.py` | `test_extract_pdf_text_returns_empty_when_pdf_parsing_fails` | 已覆盖 |
| R-AI-PDF-04 | PDF 文本为空且无法渲染页面图片时不调用 AI 并保留重选入口 | `tests/test_ai_project.py` | `test_ai_pdf_with_no_text_and_no_rendered_images_returns_hint_without_calling_api` | 已覆盖 |
| R-AI-PDF-05 | 真实 PDF 页面可以渲染为压缩 JPEG 图片输入 | `tests/test_ai_project.py` | `test_render_pdf_pages_converts_pdf_pages_to_compressed_jpeg_images` | 已覆盖 |
| R-AI-FAIL-01 | API 超时或失败返回中文错误且不创建项目 | `tests/test_ai_project.py` | `test_ai_api_failure_returns_controlled_error_without_project` | 已覆盖 |
| R-AI-FAIL-02 | AI 客户端可返回可行动失败原因且不创建项目 | `tests/test_ai_project.py` | `test_ai_api_failure_can_show_actionable_client_error_without_project` | 已覆盖 |
| R-AI-LOG-01 | 日志脱敏，不输出 API key 或完整输入 | `tests/test_ai_project.py` | `test_ai_log_redaction_hides_api_key_and_full_input` | 已覆盖 |
| R-AI-DOC-01 | 测试计划明确不真实调用外部 API | `tests/test_ai_project.py` | `test_ai_test_plan_documents_no_real_external_api_calls` | 已覆盖 |

## 3. 后续新增 xfail 的条件

- 只有当需求已经确认、但实现暂未完成时，才允许新增严格 `xfail`。
- 每个 `xfail` 必须写明退出条件，并在对应需求实现后立即移除。
- 用例仍应只依赖 fake client 或 fake PDF extractor，不依赖真实外部 AI/API、真实凭据、本机固定路径或外网。

## 4. 执行命令

```powershell
uv run pytest tests/test_ai_project.py
uv run pytest
```

当前测试结果：2026-05-26 执行 `uv run pytest tests/test_ai_project.py -q`，36 项通过；执行 `uv run pytest tests/test_web.py -q`，62 项通过；执行 `uv run pytest -q`，141 项通过。AI 开关与 `.env` 写入测试归入主 Web 测试计划。
