# 运维项目管理工具

本工具用于个人管理运维项目台账、跨年度执行预算、合同起止日期、验收付款状态和 PDF 附件归档。

## 启动

```powershell
.\scripts\start.ps1
```

启动后访问：

- 本机：`http://127.0.0.1:8765`
- 另一台电脑：`http://<这台电脑的局域网或 Tailscale IP>:8765`

默认账号密码均为 `admin`。正式使用前建议在 PowerShell 中设置环境变量：

```powershell
$env:PROJMAN_USERNAME="admin"
$env:PROJMAN_PASSWORD="你的密码"
$env:PROJMAN_SECRET_KEY="一段随机字符串"
.\scripts\start.ps1
```

如果另一台电脑无法访问，优先检查：

- 两台电脑是否在同一个 Tailscale tailnet 或同一局域网。
- Windows 防火墙是否允许 Python/端口 `8765` 入站。
- 启动脚本是否仍在运行。

## 功能范围

- 年度执行看板：项目总数、已立项、未签合同、未验收、未付款。
- 续采提醒：合同到期前默认 60 天提醒启动下一期采购。
- 项目台账：年度、项目名称、预算金额、合同金额、合同起止日期、验收日期、付款日期、备注。
- 台账筛选与导出：按年度、关键词、状态筛选，并按当前筛选导出 Excel。
- 附件归档：采购依据、合同审签 PDF、盖章合同扫描件、验收单、发票、其他附件，支持预览、下载和删除。
- 删除清理：删除项目时同步清理跨年度执行信息、数据库附件记录和实际 PDF 文件。
- 设置：调整续采提醒提前天数。

## 数据位置

运行时数据保存在 `data/`：

- `data/app.db`：SQLite 数据库。
- `data/attachments/`：项目级 PDF 附件。
- `data/annual_attachments/`：年度执行 PDF 附件。

`data/` 中真实数据不会提交到 git。

## 开发验证

```powershell
uv run pytest
```
