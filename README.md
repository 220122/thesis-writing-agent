# 🎓 毕业论文写作智能体助手

一个在本地浏览器运行的本科毕业论文写作辅助系统，围绕 **合规设计（Compliance Design）**、
**人在回路（Human-in-the-Loop）**、**审计日志（Audit Logs）** 三大理念构建。

> ⚠️ **它不是“一键成文”工具。** 系统刻意不提供“全文自动生成”入口：每个环节生成后都会
> 进入暂停点，必须由你修改 / 重新生成 / 明确批准，才会解锁下一步。
> AI 生成内容必须由你逐字核对并承担学术责任。

---

## 一、目录结构

```
project writing agent/
├── ① 首次安装（双击我）.bat      # 装依赖 + 引导输入 API Key（Windows 双击即用）
├── ② 启动项目（双击我）.bat      # 启动本地网页应用并打开浏览器
├── ③ 停止项目（双击我）.bat      # 停止应用（按 PID 精确结束，不影响其它程序）
├── scripts/                     # 上面 3 个 bat 的实际实现（PowerShell）
│   ├── install.ps1              # 检测 Python / 建虚拟环境 / 装依赖 / 写入 .env / 自检
│   ├── start.ps1                # 选空闲端口启动、写入 .run\app.pid
│   └── stop.ps1                 # PID → 端口 → 进程 三重查找后安全结束
├── app.py                       # 主应用（全部逻辑：多智能体、规则引擎、状态机、UI、审计日志）
├── requirements.txt             # 依赖清单
├── smoke_test.py                # 自检脚本（不需要 API Key，67 项检查）
├── README.md                    # 本文件
├── .env.example                 # 环境变量示例（复制为 .env 填 Key）
├── .gitignore                   # 已排除 .env / data/ / .venv 等敏感与临时文件
└── data/                        # 首次运行自动创建
    ├── thesis_agent.db          # SQLite：审计日志 / 项目进度 / 稿件版本历史
    └── last_session.json        # 上次会话指针，用于刷新后自动恢复进度
```

> 💡 只想快速用起来：**双击 ① 首次安装 → 双击 ② 启动项目**，其余不用管。
> 下面第二节是等价的手动命令行方式，适合想自己控制环境或非 Windows 的同学。

---

## 一点五、Windows 双击即用（推荐）

| 脚本 | 它做了什么 | 什么时候用 |
| --- | --- | --- |
| **① 首次安装（双击我）.bat** | 检测 Python 3.9+ → 若当前 Python 已装好依赖就直接复用（否则建 `.venv` 并安装）→ **提示你粘贴 DeepSeek API Key**（写入 `.env`，可回车跳过进入离线演示模式）→ 可选跑一次自检 | 第一次使用时跑一次 |
| **② 启动项目（双击我）.bat** | 自动挑选空闲端口（8501 起）、启动服务、自动打开浏览器；窗口保持打开即代表服务在运行 | 每次写论文时 |
| **③ 停止项目（双击我）.bat** | 按 `.run\app.pid` → 端口占用 → 进程命令行 三重定位，安全结束本项目进程 | 关掉服务 |

脚本可传参数（在 cmd 里跟在 bat 后面即可）：

```bat
② 启动项目（双击我）.bat -Port 8600      :: 换端口
② 启动项目（双击我）.bat -NoBrowser      :: 不自动打开浏览器
① 首次安装（双击我）.bat -ForceVenv      :: 强制创建独立虚拟环境
```

> 需要把代码备份到 GitHub 时，在项目目录手动执行即可：
> `git add -A` → `git status`（确认清单里没有 `.env`）→ `git commit -m "更新"` → `git push`。

**关于 API Key**：① 首次安装会问你一次，输入的内容不回显，写入项目根目录的 `.env`：

```
DEEPSEEK_API_KEY=sk-你的Key
```

`.env` 已在 `.gitignore` 中，**不会被提交到 Git 仓库**。想换 Key 就重跑一次 ①，或直接编辑 `.env`。
直接回车跳过也可以——应用会进入**离线演示模式**，流程与审计日志照样能完整体验。

---

## 二、本地运行步骤（手动命令行方式）

### 1. 安装依赖（Python 3.9+，推荐 3.10 ~ 3.12）

```bash
cd "project writing agent"
pip install -r requirements.txt
```

建议使用虚拟环境：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置 DeepSeek API Key

Key 从环境变量 `DEEPSEEK_API_KEY` 读取（**代码中不需要填任何 Key**）：

```bash
# Windows CMD（当前窗口有效）
set DEEPSEEK_API_KEY=sk-你的Key

# Windows PowerShell（当前窗口有效）
$env:DEEPSEEK_API_KEY="sk-你的Key"

# Windows 永久生效
setx DEEPSEEK_API_KEY "sk-你的Key"

# macOS / Linux
export DEEPSEEK_API_KEY=sk-你的Key
```

也可以复制 `.env.example` 为 `.env`，在文件中填写（已安装 `python-dotenv` 时自动加载）：

```
DEEPSEEK_API_KEY=sk-你的Key
```

接口地址默认 `https://api.deepseek.com`，模型默认 `deepseek-chat`，
如需修改可设置环境变量 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`。

> 💡 **未配置 Key 也能运行**：系统自动进入 **离线演示模式**，用本地模板模拟模型流式输出，
> 可完整跑通“大纲 → 审查 → 批准 → 逐章撰写 → 审计日志”流程（输出会明确标注
> `[演示模式输出 · 未调用 DeepSeek API]`）。适合先验证环境与流程。

### 3. 启动

```bash
streamlit run app.py
```

浏览器会自动打开 <http://localhost:8501>。若未自动打开，手动访问该地址即可。

### 4. （可选）先跑自检

```bash
python smoke_test.py
```

不需要 API Key，覆盖合规规则引擎、HITL 门禁、SQLite 审计日志与哈希链、
以及基于 Streamlit `AppTest` 的完整 UI 端到端流程（离线演示模式）。

---

## 三、使用流程（人在回路）

| 步骤 | 你在界面上的动作 | 系统的门禁 |
| --- | --- | --- |
| ① 新建项目 | 侧边栏填写题目、专业方向、目标字数、章节数、著录格式 | — |
| ② 生成大纲 | 【规划师 · 大纲】页点击“让规划师生成大纲” | 生成后进入 **HITL 暂停点**（待审阅） |
| ③ 修改/驳回 | 直接编辑文本框保存、或填写意见重新生成 | 编辑或重生成会**使旧审查结论失效**，必须重审 |
| ④ 合规审查 | 点击“运行合规审查（大纲）” | 规则引擎 + 大模型双层校验 |
| ⑤ 批准大纲 | 勾选学术诚信核对声明 → 点击“批准通过大纲” | 🛑 存在阻断项时按钮**硬性禁用**；未批准不能写任何正文 |
| ⑥ 逐章撰写 | 【撰写员 · 章节】页选择章节 → “生成第N章初稿” | 🔒 第 N 章未批准，第 N+1 章无法撰写 |
| ⑦ 逐章批准 | 编辑 / 重生成 → 合规审查 → 勾选声明 → 批准 | 同上，阻断项未清除不能批准 |
| ⑧ 定稿导出 | 【导出】页下载 Markdown 全文、项目快照、版本历史 | 未全部批准会给出初稿警告 |

其它页面：

- **🛡️ 合规审查**：维护项目参考文献库（自动合并撰写员生成的文献、一键去重重新编号）、
  全篇格式/结构/交叉引用检查、全局自查重（跨章节 + 自定义比对源）。
- **🧾 审计日志**：表格视图 / 时间线视图 / 哈希链完整性校验 / JSON 与 CSV 导出。
- **📋 概览与流程**：进度看板与六步门禁状态一览。

---

## 四、三大核心理念的实现位置（便于答辩说明）

### 1. 合规设计 —— `RuleEngine` 类 + `ComplianceAgent`

**本地规则引擎（确定性、零 token，负责硬拦截）**

| 规则 | 说明 | 级别 |
| --- | --- | --- |
| 引用不存在 | 正文 `[n]` 在参考文献表中不存在 | 🛑 阻断 |
| 占位符/未完成 | 含 `TODO`、`待补充`、`xxx` 等 | 🛑 阻断 |
| 数据来源不明 | “调查/统计/实验结果(显示/表明)…”但该句无引用标注 | 🛑 阻断 |
| 引用密度不足 | 文献综述类章节含引用段落占比 < 50% 或引用 < 3 处 | 🛑 阻断 |
| 疑似抄袭/重复 | 与比对源连续相同 ≥ 13 个汉字 / ≥ 8 个英文单词（经典查重口径） | 🛑 阻断 |
| 著录格式 | GB/T 7714 缺文献类型标识/年份/卷期页；APA 缺“(年份).” | ⚠️ 警告 |
| 文献未引用 | 文献表列了但正文未引 | ⚠️ 警告 |
| 学术诚信表述 | 模糊归因（“大量研究表明”）、绝对化断言（“毋庸置疑”）、个人经验充当论据 | ⚠️ 警告 |
| 格式规范 | 中英文标点混用、标题层级跳级、超长段落、引用标注位置 | ⚠️ 警告/提示 |
| 字数偏离 | 低于目标 60% 或高于 160% | ⚠️ 警告/提示 |
| 逻辑一致性 | 章节标题与已批准大纲不一致、大纲小节大面积缺失 | ⚠️ 警告 |

**大模型审查（`ComplianceAgent`）**：在规则引擎之后做语义层复核，
要求以 `[BLOCKER]/[WARNING]/[INFO] 类别：问题｜证据：片段｜建议：改法` 的固定格式输出，
解析后与规则结论合并；解析失败也不会丢失内容（整体降级保留）。

> 说明：系统**不进行全网查重**，也无法替代知网/维普等官方查重系统。
> 它做的是「引用规范 + 引用覆盖率 + 连续文本重复 + 风险表述」的**自查**，
> 用于在提交前发现明显问题。

### 2. 人在回路 —— 状态机与门禁

工作流状态：`INIT → OUTLINE_DRAFT → OUTLINE_APPROVED → WRITING → DONE`（`sync_state()` 单一数据源刷新）。

关键机制：

- **一次只生成一个环节**：没有任何“全部生成”入口，每个动作都由用户按钮触发。
- **审查结论带版本号**：`reviews[key]["version"]` 必须等于稿件当前 `version`，
  否则视为过期（`review_is_fresh()`）——**改稿后必须重审**，这是最常见的“假批准”漏洞。
- **批准双条件**：① 无 BLOCKER（`review_blockers()`）；② 用户勾选学术诚信核对声明。
  两者缺一，批准按钮 `disabled=True`，且声明勾选状态写入审计日志。
- **逐章解锁**：第 N 章批准是第 N+1 章生成按钮可用的前置条件。
- 支持**撤回批准**（后续章节重新锁定，留痕）、**驳回重写**、**人工直接编辑**。

### 3. 审计日志 —— `LogKeeperAgent` + SQLite

- 每一次 **Prompt 输入 / AI 原始输出 / 用户修改 / 合规结论 / 批准驳回** 都写入 `audit_logs` 表，
  字段：`ts, project_id, actor, actor_role(HUMAN|AGENT|SYSTEM), event_type, stage, target,
  prompt, ai_output, user_edit, status, detail`。
- 用户修改以 **unified diff** 形式记录（`difflib.unified_diff`），可回溯“AI 写了什么、你改了什么”。
- **哈希链防篡改**：每条记录含 `prev_hash` 与 `record_hash = sha256(记录字段 + prev_hash)`，
  审计页可一键校验；直接改数据库会被检出（`verify_hash_chain()`）。
- 支持按角色 / 事件类型 / 关键字筛选，表格与时间线两种视图，导出 JSON / CSV。
- 项目进度与稿件版本历史同样持久化在 SQLite，刷新浏览器或重启应用后可**断点续写**。

### 多智能体角色对照

| Agent | 代码位置 | 职责 |
| --- | --- | --- |
| 规划师 Agent | `PlannerAgent` | 与用户沟通题目与思路，生成/修订大纲 |
| 撰写员 Agent | `WriterAgent` | 依已批准大纲逐章撰写正文 |
| 合规审查员 Agent | `ComplianceAgent` + `RuleEngine` | 引用格式、学术诚信、逻辑一致性审查 |
| 日志记录员 Agent | `LogKeeperAgent` | 确定性写入审计日志（含哈希链），不调用大模型 |

---

## 五、`st.session_state` 与流式输出的实现要点

- **项目状态单一来源**：`st.session_state["pstate"]` 是一个 dict，包含
  `meta / state / outline / chapters / references / reviews / history`；
  任何变更后调用 `persist()` → `sync_state()` → 写入 SQLite，因此刷新、重启、切换项目都不丢进度。
- **流式输出**：`LLMClient.stream()` 使用 OpenAI SDK 的 `stream=True` 逐块 yield；
  `stream_to_ui()` 用 `st.empty()` 占位符边收边 `markdown()` 渲染（末尾加 `▌` 光标），
  结束后一次性落库 + 写审计日志 + `st.rerun()` 刷新 UI。同步生成器在 Streamlit 主线程中消费，
  因此“先渲染、后落库、再重跑”的顺序是安全的。
- **异常处理**：调用失败统一抛 `LLMError`，UI 层捕获后展示排查建议，
  并写一条 `LLM_ERROR` 审计记录，不会污染稿件版本。
- **控件 key 与版本绑定**：编辑框 key 形如 `chapter_edit_3_2`（章节号_版本号），
  版本变化即生成新控件，避免旧值残留覆盖新稿。
- **数据库连接**：`@st.cache_resource` 缓存 `sqlite3` 连接（`check_same_thread=False` + 线程锁），
  避免每次重跑重建连接。

---

## 六、常见问题

**Q：启动报错 `ModuleNotFoundError: No module named 'streamlit'`**
A：没有安装依赖，或没有激活对应的虚拟环境。执行 `pip install -r requirements.txt`。

**Q：页面提示“离线演示模式”**
A：未检测到 `DEEPSEEK_API_KEY`。设置环境变量后**重启** `streamlit run`（环境变量在进程启动时读取）。

**Q：调用失败：401 / 402 / 连接超时**
A：401 检查 Key 是否正确；402 检查账户余额；超时检查网络能否访问 `https://api.deepseek.com`。

**Q：批准按钮一直是灰色**
A：三种原因——① 还没运行合规审查；② 存在 🛑 阻断项（按提示修改后重审）；
③ 没有勾选学术诚信核对声明。鼠标悬停在按钮上会有提示。

**Q：想重新开始**
A：侧边栏「删除项目」会连同该项目的审计日志一起删除；或直接删掉 `data/` 目录。

**Q：端口被占用**
A：`streamlit run app.py --server.port 8502`；用脚本的话：`② 启动项目（双击我）.bat -Port 8600`
（不指定端口时脚本会自动往后找 8502/8503/8510/8520）。

**Q：双击 ① 之后，Key 输在哪里？粘贴不上去怎么办？**
A：脚本会提示“请粘贴你的 DeepSeek API Key”，提供 **4 种**方式，总有一种能用：
① 鼠标**右键**粘贴（传统控制台最可靠）；② `Ctrl+V`；
③ **先把 Key 复制到剪贴板，然后直接按回车** —— 脚本会自动读取剪贴板（推荐，绕开所有粘贴问题）；
④ 输入 `E` 回车 —— 用记事本手动填写 `.env`。
粘贴整行 `DEEPSEEK_API_KEY=sk-xxx` 或夹在别的话里也能自动识别出 Key。
直接回车跳过则进入离线演示模式，之后重跑 ① 就能补填。

**Q：双击 ② 后窗口闪一下就没了 / 提示启动失败**
A：脚本会打印具体原因。最常见的是没先跑 ①（缺依赖）；其次是用了非交互环境导致 Streamlit
首次运行的邮箱询问没写入——脚本已自动处理，若仍遇到可加 `-NoBrowser` 参数重试。

**Q：我的 API Key 会被提交到 Git 仓库吗？**
A：不会。`.env`、`data/`、`.venv/`、`.python_path` 等都已写进 `.gitignore`。
手动提交前用 `git status` 扫一眼清单即可；本项目已开启忽略规则，`git add -A` 也不会带上它们。

---

## 七、学术诚信声明

1. 本系统为**写作辅助工具**，不提供一键成文服务，所有内容必须经作者本人核对、修改。
2. 系统内的合规检查基于规则与模型启发式判断，**不能替代**学校指定的查重系统与导师审核。
3. 引用他人观点必须规范标注；禁止编造文献、数据与实验结果。
4. 全部操作已记录在审计日志中，可随时导出备查。

祝顺利毕业 🎓
