# backend/deps — 服务产物挂载目录

`backend/` 是独立的 Flask 后端模块，其 `storage` / `models` / `auth` /
`services` 与 `markdown_service` 包由仓库内的独立服务产出。服务源码与构建
脚本见各自服务目录；本目录用于挂载构建产物，`backend/__init__.py` 会按以下
约定把产物加入 `sys.path`（目录缺失时自动跳过，不影响仓库导入与打包）：

| 服务 | 挂载路径 | 提供包 |
|---|---|---|
| csv_data_layer | `csv_data_layer/output/backend/` | `backend.storage`、`backend.models` |
| auth_service | `auth_service/output/output/backend/` | `backend.auth` |
| stats_service | `stats_service/output/output/backend/` | `backend.services` |
| markdown_service | `markdown_service/output/` | `markdown_service` |

路径全部为项目相对路径，clone 到任何位置无需修改配置。
