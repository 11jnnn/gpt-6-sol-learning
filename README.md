# 个人 AI 助手（学习项目）

一个最小可运行的个人 AI 助手：用 **FastAPI + LangChain + DeepSeek** 实现普通聊天 + 只读工具（列出项目/文件、读文件、搜代码）+「提出修改 → 用户确认 → 写入 → 检查 → 修正」的基本 AI coding 闭环，并用一个原生网页展示。

本项目的目标是「先跑通一个小功能，再逐步扩展」，因此刻意保持简单：
- 单 Agent，没有 LangGraph 自定义图 / 数据库 / 长期记忆 / 向量检索 / 心跳 / 定时任务 / 多 Agent / 复杂权限。
- 前端用原生 HTML/CSS/JS，不引入 Vue、React 等框架。
- 模型通过环境变量配置，实际使用 DeepSeek（助手不会声称自己是 GPT-6-Sol）。
- 写入必须由用户点击确认后由后端执行，模型不能自行写文件；不运行任意 shell 命令。

---

## 一、环境准备

- Windows（本项目在 Windows 11 上开发）或 macOS / Linux 均可。
- Python 3.10+（本项目在 3.13 上验证）。
- 一个 DeepSeek API Key，申请地址：https://platform.deepseek.com

## 二、依赖安装

建议使用虚拟环境：

```bash
cd D:\Projects\gpt6-sol-learning
python -m venv .venv
# Windows（Git Bash / PowerShell 激活方式二选一）
source .venv/Scripts/activate        # Git Bash
# .venv\Scripts\activate             # PowerShell / CMD

pip install -r requirements.txt
```

依赖清单（见 `requirements.txt`）：
- `fastapi`、`uvicorn[standard]` —— Web 服务与运行。
- `langchain` —— 组装 Agent（使用 `create_agent`）。
- `langchain-deepseek` —— DeepSeek 官方适配包。
- `python-dotenv` —— 加载 `.env`。

## 三、.env 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写三项：

```ini
DEEPSEEK_API_KEY=你的真实密钥
DEEPSEEK_MODEL=deepseek-chat
PROJECTS_DIR=D:\Projects
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | DeepSeek 密钥，只在服务端使用，不会发给前端 |
| `DEEPSEEK_MODEL` | 否 | 模型名，默认 `deepseek-chat` |
| `PROJECTS_DIR` | 是 | 工具要列出的本地目录，默认 `D:\Projects` |

> 没有 API Key 时，访问 `/chat` 会返回清晰的配置提示（见「错误处理」）。

## 四、启动命令

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

默认监听 `127.0.0.1`（仅本机可访问）。启动后打开浏览器访问：

```
http://127.0.0.1:8000
```

## 五、使用示例

**示例 1 —— 普通聊天**

在网页输入框输入：

```
你好，简单介绍一下你能做什么。
```

助手会基于系统提示词介绍自己的定位和能力。

**示例 2 —— 工具调用**

输入：

```
列出我本地的项目。
```

助手会调用 `list_local_projects` 工具，读取 `D:\Projects` 下的直接子目录并返回。网页上会同时显示一条「工具调用」记录，展示工具名、参数和真实返回结果。

> 返回的项目列表来自真实文件系统读取，与实际目录一致；没有任何硬编码列表或模拟成功。

**示例 3 —— 列出项目文件**

输入：

```
看看 gpt6-sol-learning 里有哪些文件。
```

助手先调用 `list_project_files`（必要时先列项目），返回项目根目录下的直接文件和文件夹。

**示例 4 —— 读取文件并解释**

输入：

```
读取这个项目的 agent.py，解释它的作用。
```

助手调用 `read_project_file` 读取 `agent.py`（带行号），然后解释其作用。

**示例 5 —— 搜索代码**

输入：

```
搜索这个项目哪里注册了 list_local_projects。
```

助手调用 `search_project_code`，返回匹配的文件相对路径、行号和匹配行。

**示例 6 —— 连续读取多个文件分析**

输入：

```
读取 agent.py 和 tools.py，解释工具调用流程。
```

助手会依次读取两个文件，再综合解释工具调用流程。

**示例 7 —— 检查代码问题**

输入：

```
检查代码有没有问题，指出位置和原因。
```

助手会先列目录/搜索定位、再读取相关代码，最后区分「已确认的问题」和「待验证的疑点」，并注明位置与原因。

## 六、各文件职责

```
gpt6-sol-learning/
├── main.py                    # FastAPI 路由：/、/chat、/health + 确认/拒绝/恢复/授权接口
├── agent.py                   # 接入 DeepSeek、注册工具、执行 Agent 流程，返回待确认信息
├── tools.py                   # 7 个 @tool（4 只读 + 2 提出修改 + 1 运行检查）
├── fsutil.py                  # 共享路径/文件边界校验与常量（各模块复用，避免规则重复）
├── changes.py                 # 待确认修改、任务状态、应用/恢复/备份（内存实现）
├── system_prompt.txt          # 从原始提示词提炼出的系统提示词（含编码闭环规则）
├── static/
│   └── index.html             # 聊天网页（原生 HTML/CSS/JS，展示差异/确认/授权）
├── reference/
│   ├── GPT-6-Sol_Prompts.txt  # 原始提示词（原文，未改动）
│   ├── GPT-6-Sol_Tools.json   # 原始工具定义（原文，未改动）
│   ├── list_local_projects_adapted.json  # 本地适配后的工具定义
│   └── adaptation.md          # 原始资料与本地适配的对应说明
├── .env.example               # 环境变量模板
├── .gitignore                 # 忽略 .env 与 .agent_backups/
├── requirements.txt           # 依赖清单
└── README.md                  # 本说明
```

七个工具（4 只读 + 2 提出修改 + 1 运行检查）：

| 工具 | 作用 | 来源 |
| --- | --- | --- |
| `list_local_projects` | 列出 `PROJECTS_DIR` 下的直接子目录 | 由原始 `list_projects` 适配 |
| `list_project_files` | 列出项目内指定目录的直接文件与文件夹 | 本项目新增 |
| `read_project_file` | 读取项目内文本文件的指定行范围 | 本项目新增 |
| `search_project_code` | 在项目内按字面文字搜索 | 本项目新增 |
| `create_project_file` | 提出新建文本文件（只登记待确认） | 本项目新增 |
| `replace_project_text` | 提出精确文本替换（只登记待确认） | 本项目新增 |
| `run_project_check` | 运行 `python_syntax` / `pytest` 检查 | 本项目新增 |

> `apply` / `reject` / `restore` / pytest 授权**不是模型工具**，只能由用户在前端点按钮后由后端执行。

### 三个代码工具的用法与安全边界

**`list_project_files(project_name, relative_dir="")`**
- `project_name`：`PROJECTS_DIR` 下的项目目录名（例如 `gpt6-sol-learning`）。
- `relative_dir`：项目内的相对目录，默认空表示项目根目录。
- 只列出一层内容，返回名称、类型（文件/目录）和相对路径；最多 200 条，超出会注明截断。

**`read_project_file(project_name, file_path, start_line=1, max_lines=150)`**
- 读取项目内文本文件的指定行范围，返回带行号的内容。
- `max_lines` 上限 300；文件超过 1MB、是二进制、或无法解码时会明确提示。
- 返回是否还有后续内容，模型可分次读取，但不会把部分读取说成已读完。

**`search_project_code(project_name, query)`**
- 按字面文字搜索（不是正则），优先调用 `rg`，不可用时回退 Python 文本搜索。
- 返回相对路径、行号和匹配行，最多 30 条，超出会注明；无结果明确返回「未找到」。

三个工具共用同一套路径与文件校验（`tools.py`），保证搜索不能绕过读取限制：

- 只能访问 `PROJECTS_DIR` 下指定项目内的内容；用解析后的真实路径校验边界，拒绝绝对路径、越界的 `..` 路径、指向项目外的符号链接。
- 跳过 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`。
- 不读取/搜索 `.env`、`.env.*`、私钥、证书密钥及明确的凭据文件。
- 只处理常见代码/文本格式（`.py/.js/.ts/.html/.css/.md/.txt/.json/.toml/.yaml/.yml`），并限制文件数量、单个文件大小和结果长度。

### 修改与检查（编码闭环）

助手按以下顺序工作：**确认目标项目 → 列目录/搜索/读取 → 说明问题并提出最小修改 → 生成差异等待确认 → 用户确认后写入 → 运行语法检查（有授权时 pytest）→ 失败则提出下一次修改**。最多 3 轮「检查失败后修正」。

关键机制：

- **提出不落盘**：`create_project_file` / `replace_project_text` 只生成 unified diff、`change_id` 和原文件哈希，存入内存字典，不写文件。
- **用户确认才写入**：`apply_change` 不注册为模型工具；只有前端点「确认并应用」后，后端 `POST /change/apply` 才真正写盘。模型不能输出「用户已同意」来绕过。
- **应用前再校验**：路径仍在校验范围内、当前文件哈希与提出时一致、新建目标仍不存在；否则拒绝并要求重新读取。
- **备份与恢复**：替换类修改在写入前把原文件备份到助手目录下独立的 `.agent_backups/`（加入工具排除规则和 `.gitignore`，不保存密钥文件）；网页应用后会显示「恢复此修改」按钮，恢复前校验当前哈希等于修改后哈希。新建文件暂不支持恢复。
- **检查**：`run_project_check` 支持 `python_syntax`（`compile()` 编译检查，不执行、不生成 pyc）和 `pytest`（`python -m pytest`，参数列表 + `shell=False`，最小环境变量、不继承密钥，设置超时与输出上限；超时先 `taskkill /T` 杀进程树）。pytest 首次执行某项目前需用户授权。
- **任务按 task_id 隔离**：授权、修正轮数、待确认修改都按 task_id 隔离，多标签页/多任务互不共享；「新任务」会向后端申请全新 task_id。状态在内存，服务重启后丢失，这**不是**可恢复的任务系统。

## 七、从用户输入到模型回答的完整流程

以「列出我本地的项目」为例：

1. **前端**：网页把用户输入和历史记录 `POST` 到 `/chat`。
2. **路由**：`main.py` 的 `chat_endpoint` 收到请求，交给 `agent.chat`。
3. **组装**：`agent.py` 读取 `system_prompt.txt`（系统提示词），把历史 + 本次消息组装成消息列表。
4. **建 Agent**：`create_agent(model, tools, system_prompt)` 创建 Agent——
   - 提示词作为 SystemMessage 注入；
   - 七个工具的 name/description/参数被转成模型可读的「工具定义」。
5. **模型决策**：DeepSeek 看到消息和工具定义，判断「这个问题需要真实数据」，决定调用某个工具（列项目 / 列文件 / 读文件 / 搜索 / 提出修改 / 运行检查）。
6. **执行工具**：LangChain 执行 `tools.py` 里对应的 Python 函数；提出修改只登记、运行检查会真正执行（有授权时）。
7. **结果回传**：工具结果作为 ToolMessage 交回模型。
8. **生成回答**：模型基于工具结果生成最终回答。
9. **返回网页**：`agent.py` 返回 `{reply, tool_calls, pending_changes, pending_check, task}`，`main.py` 以 JSON 返回，前端渲染回答、工具记录、修改差异与确认按钮。

## 八、原始提示词与工具的适配说明

详见 `reference/adaptation.md`，要点：

- **提示词**：`GPT-6-Sol_Prompts.txt` 是包含多种运行模式与专用模板的大型文件，未整体拼接。提炼的规则写入 `system_prompt.txt`：表达直接、在任务范围内主动完成、必要时用工具、不编造信息或工具结果、「代码阅读与分析」规则，以及「修改与检查」规则（提出修改需用户确认、最多 3 轮修正、未授权不声称已验证等）。
- **身份**：原资料中有「You are Codex, an agent based on GPT-6」等声明，本项目**未采用**，助手定位为 DeepSeek 驱动。
- **工具**：原始工具 `list_projects` 面向 Codex 桌面端的「项目」概念（返回 projectId + isGitRepository）；本项目将其适配为 `list_local_projects`。原始 JSON 中不存在代码读取/搜索/修改/检查工具，`list_project_files`、`read_project_file`、`search_project_code`、`create_project_file`、`replace_project_text`、`run_project_check` 均为**本项目新增**。详见 `reference/adaptation.md`。

## 九、错误处理

| 场景 | 行为 |
| --- | --- |
| 未配置 `DEEPSEEK_API_KEY` | `/chat` 返回「未配置 DEEPSEEK_API_KEY…」提示 |
| 模型连接失败 | `/chat` 返回「调用模型失败：…」并附带真实错误 |
| `PROJECTS_DIR` 目录不存在 | 工具返回「目录不存在：…」的明确说明 |
| 目录读取失败 | 工具返回真实错误信息（如权限不足） |
| 项目 / 文件不存在 | 工具返回「项目不存在 / 文件不存在：…」 |
| 路径越界或指向项目外 | 工具返回「拒绝访问：…」（绝对路径、`..`、外部符号链接/junction） |
| 进入被排除的目录（`.git`/`.venv` 等） | 读取、列目录、搜索均拒绝或跳过 |
| 敏感文件（`.env`、密钥、凭据） | 工具返回「拒绝读取 / 搜索时跳过」 |
| 非白名单文件类型（如 `.ini`） | 读取/搜索拒绝 |
| 文件过大、二进制、无法解码 | 工具返回明确提示 |
| 结果过多 / 单行过长 / 输出过长 | 截断到上限并注明 |
| 搜索 `rg` 不可用 | 回退 Python 文本搜索 |
| 备用搜索达到扫描上限 | 无论是否命中都说明「后续文件未扫描」 |
| 工具调用次数达到上限 | `/chat` 返回「为避免无限循环已停止」 |
| 提出修改（`old_text` 找不到/多次） | 返回明确错误，要求先读取补充上下文 |
| 替换后内容超过大小上限 | 提出与应用时都拒绝 |
| 新建覆盖已有文件 / 空 `old_text` | 拒绝 |
| 应用时原文件已被外部修改 | 拒绝应用，要求重新读取生成建议 |
| 未确认 / 已拒绝 / 已过期 / 重复应用 | 不写入 |
| pytest 首次执行未授权 | 返回「待确认」，等待用户授权 |
| 缺少 pytest 依赖 | 明确提示，不自动安装 |
| 子进程超时 / 输出过长 | 终止并注明截断 |
| 修正次数达到上限 | 停止提出新修改 |

## 十、验证情况（诚实说明）

### 本地工具与边界测试（不调用模型）

- 列出项目文件：`list_project_files` 正确返回 `D:\Projects\gpt6-sol-learning` 下的一级内容。
- 读取带行号：`read_project_file` 读 `agent.py` 返回正确行号。
- 搜索：`search_project_code` 搜 `create_agent` 返回 `agent.py` 中的真实位置。
- 连续读取：依次读 `agent.py`、`tools.py` 均成功。
- 不存在的项目/文件：返回「项目不存在 / 文件不存在」。
- 路径越界：绝对路径、`..` 越界、非法项目名均返回「拒绝访问」。
- 项目外链接：用 junction 指向项目外，读取工具与备用搜索都拒绝访问、不泄露外部内容。
- 排除目录：`list_project_files` 传 `relative_dir=".venv"` 被拒绝；读取 `.venv`/`settings.ini` 被拒绝。
- 敏感文件：`.env`、`.env.example` 无法通过读取或搜索获取。
- 大文件 / 大量结果：超过 1MB 的文件提示「文件过大」；50 条匹配只显示前 30 条并注明截断。
- 超长行：约 20 万字符的单行，读取与搜索返回都截断到几百字符。
- `rg` 兜底：模拟 `rg` 不可用，Python 备用搜索正常命中；达到扫描上限时无论是否命中都说明「后续文件未扫描」。

### 编码闭环测试（不调用真实模型，临时目录 + 虚构内容）

- 提出修改不落盘：`create_project_file` / `replace_project_text` 只登记待确认，文件未变。
- `old_text` 找不到 / 出现多次 / 空 `old_text` 被拒；新建覆盖已有文件被拒。
- 用户确认后写入成功且生成备份；未确认 / 拒绝 / 过期 / 重复应用均不写入。
- 原文件被外部修改后，旧建议无法应用。
- 恢复成功；恢复时检测到后续修改则拒绝；新建文件恢复被拒。
- 绝对路径、`..` 越界、junction 指向项目外、排除目录、敏感文件在提出修改时均被拒。
- 语法检查 `python_syntax` 能报出真实的语法错误（`compile()` 不执行代码）。
- pytest 未授权返回「待确认」；授权后缺少 pytest 返回「缺少 pytest 依赖」；用 mock 验证退出码 0/1/5/2 的解释与状态更新。
- 子进程环境变量不继承 `DEEPSEEK_API_KEY` 等密钥。
- 修正轮数达到上限（3 轮）后停止提出新修改。
- HTTP 接口 `apply/restore/authorize/reset` 经 TestClient 走通。
- 假模型：`create_agent` 正确把工具调用路由到 `create_project_file`，`chat()` 返回 `pending_changes` 且文件未落盘。

### 真实 DeepSeek 端到端测试

- 「搜索这个项目哪里注册了 list_local_projects」→ 模型依次调用 `list_local_projects → search_project_code → read_project_file ×2`，回答正确引用 `agent.py:66-75`、`tools.py:134-135` 的行号。
- 「看看 gpt6-sol-learning 里有哪些文件」→ 模型调用 `list_project_files`，返回正确的一级目录列表。

### 尚未验证 / 限制

- 真实 DeepSeek 驱动的完整「提出修改→确认→写入→检查→修正」多轮闭环未用真实模型跑（本地工具链路 + 假模型链路已验证）。
- 本机未安装 pytest，因此真实 pytest 的「成功/失败/未收集测试/超时」未实测；退出码解释与授权/状态逻辑用 mock 验证，依赖缺失已实测。
- 符号链接用 junction 验证（Windows 符号链接需管理员权限）。

说明：本机系统环境变量中已存在 `DEEPSEEK_API_KEY`，因此「真实 DeepSeek」测试直接可用。若换一台机器，需通过 `.env` 提供密钥。

## 十一、限制与后续扩展建议

当前限制（诚实说明）：
- 待确认修改、任务状态、pytest 授权都在内存，**服务重启后丢失**；不是可恢复的任务系统。
- pytest 只在用户信任的项目上运行，**不是操作系统级沙箱**。
- 首版仅支持严格 UTF-8 文本；替换匹配采用精确文本（自动尝试 CRLF/LF 变体）。
- 新建文件的恢复暂不支持（不通过恢复删除新建文件）。
- Windows 下超时清理尽力而为（`taskkill /T`），派生进程清理不保证彻底。

可在此基础上逐步加入：流式输出、会话历史持久化、更多检查类型、更完善的沙箱隔离等。当前阶段刻意不提前扩展。
