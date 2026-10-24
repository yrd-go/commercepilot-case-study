# 源码审阅说明

这是 CommercePilot 的公开源码快照，用于代码审阅，不包含私有数据和可用于生产环境的凭据。

## 架构

项目分为四层：

1. `src/saviland/web/` 负责 Starlette 应用、登录边界、API、买家端页面与浏览器端后台界面。
2. `src/saviland/domain/` 负责商品、订单、库存与广告诊断等纯业务逻辑。
3. `src/saviland/pipelines/` 负责商品内容生成、合规检查、广告周报和 HTML、Excel、Markdown 导出。
4. `src/saviland/render/` 负责用 Pillow 生成确定性的图表和主图变体。

`src/saviland/rag_bridge.py` 把商品内容链路接入检索。检索命中会保留来源文件和行号，便于把生成文案追溯到证据。

## 建议阅读顺序

- 先从 `src/saviland/web/server.py` 和 `web/api.py` 看应用边界。
- 阅读 `web/auth.py`，了解会话模型和明确标注的演示级限制。
- 阅读 `pipelines/listing.py`，看检索、结构化生成与合规拦截。
- 阅读 `pipelines/weekly.py`，看确定性诊断如何先于 LLM 归因。
- 阅读 `runs.py`，看运行档案、降级状态和产物记录。
- 阅读 `tests/test_saviland.py`，看面向行为的回归用例。

## 数据与安全边界

公开快照不包含运行数据库、抓取来源文件、商品图片素材、日志、`.env`、API Key、用户记录或本地工作文档。源码只从环境变量读取密钥，不会把密钥写入仓库。

认证部分明确限定为演示级：提供登录和会话隔离，但不包含 RBAC、口令哈希、限流或生产环境加固。这些属于真实部署层的工作。

## 复现说明

部分演示和集成测试依赖未公开的私有目录数据与图片素材，因此干净克隆不一定能复现录制视频中的每一份产物。单元逻辑和模块结构仍可直接审阅，本项目不对“无需私有数据即可完整复现”作承诺。
