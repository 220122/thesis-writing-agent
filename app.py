# -*- coding: utf-8 -*-
"""
================================================================================
 毕业论文写作智能体助手  (Thesis Writing Agent Assistant)
 基于 Streamlit + DeepSeek(OpenAI 兼容接口) 的本地网页应用
================================================================================

【三大核心理念】
  1. 合规设计 (Compliance Design)
     - 内置规则引擎 + 大模型审查双层校验：参考文献格式(GB/T 7714 / APA)、
       正文引用与文献表交叉核对、学术诚信风险表述、数据来源缺失、
       连续文本重复(自查重)、格式规范。
     - 命中 BLOCKER（阻断级）问题时，"批准通过"按钮被硬性禁用，
       必须由用户修改或重新生成并重新审查通过后才能继续。
  2. 人在回路 (Human-in-the-Loop, HITL)
     - 系统绝不提供"一键写完"入口：每次只生成一个环节，生成后进入
       PENDING_REVIEW 状态并暂停，等待用户【修改 / 编辑 / 重新生成 / 批准】。
     - 硬门禁：大纲未批准 → 不能写任何章节；第 N 章未批准 → 不能写第 N+1 章。
     - 批准必须由用户勾选"学术诚信核对声明"后才能点击，行为写入审计日志。
  3. 审计日志 (Audit Logs)
     - 每一次 Prompt 输入、AI 输出、用户修改(diff)、合规结论、批准/驳回
       都写入 SQLite，字段包含 时间戳/操作角色/输入/输出/用户修改/最终状态。
     - 每条日志带 prev_hash + record_hash 组成哈希链，可一键校验是否被篡改。
     - 支持表格视图 / 时间线视图 / JSON / CSV 导出。

【多智能体角色】
  - 规划师 Agent (PlannerAgent)        : 生成与修改论文大纲
  - 撰写员 Agent (WriterAgent)          : 按大纲逐章生成初稿
  - 合规审查员 Agent (ComplianceAgent)  : 规则引擎 + 大模型双重合规审查
  - 日志记录员 Agent (LogKeeperAgent)   : 确定性写审计日志(含哈希链)，不调用大模型

【目录结构】
  project writing agent/
  ├── app.py                 # 主应用（本文件，全部逻辑）
  ├── requirements.txt       # 依赖
  ├── README.md              # 使用说明
  ├── smoke_test.py          # 无需 API Key 的自检脚本
  ├── .env.example           # 环境变量示例
  └── data/                  # 运行时自动创建
      ├── thesis_agent.db    # SQLite：审计日志 / 项目进度 / 稿件版本
      └── last_session.json  # 上次会话，用于刷新后自动恢复

【运行】
  pip install -r requirements.txt
  set DEEPSEEK_API_KEY=sk-xxxx      (Windows CMD)
  streamlit run app.py

  说明：未配置 DEEPSEEK_API_KEY 时自动进入"离线演示模式"，
        用本地模板模拟模型输出，便于先跑通 HITL 流程与审计日志。
================================================================================
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import literature as lit          # 真实文献检索（OpenAlex / Crossref，免费无 Key）
except Exception:                      # pragma: no cover - 模块缺失时降级为手工录入
    lit = None

# ---- 尽早加载项目根目录的 .env（若安装了 python-dotenv）----
#   ① 首次安装脚本会把 DEEPSEEK_API_KEY 写进 .env；
#   使用 utf-8-sig 是为了兼容「记事本另存为 UTF-8」时带 BOM 的情况。
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                encoding="utf-8-sig")
except Exception:
    pass

# =============================================================================
# 一、全局配置
# =============================================================================

APP_NAME = "毕业论文写作智能体助手"
APP_VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录默认在项目下的 data/；可通过环境变量 THESIS_AGENT_DATA_DIR 覆盖（便于测试隔离）
DATA_DIR = os.getenv("THESIS_AGENT_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "thesis_agent.db")
LAST_SESSION_FILE = os.path.join(DATA_DIR, "last_session.json")

# ---- DeepSeek 配置（如需改动，可设置同名环境变量覆盖，无需改代码）----
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

MAX_TOKENS_OUTLINE = 3000
MAX_TOKENS_CHAPTER = 6000
MAX_TOKENS_REVIEW = 2500

CITATION_STYLES = [
    "GB/T 7714-2015（顺序编码制）",
    "APA 第7版",
]

# 工作流状态机
ST_INIT = "INIT"                        # 仅创建了项目基本信息
ST_OUTLINE_DRAFT = "OUTLINE_DRAFT"      # 大纲已生成，等待用户处理
ST_OUTLINE_APPROVED = "OUTLINE_APPROVED"  # 大纲已批准，可开始写章节
ST_WRITING = "WRITING"                  # 章节写作中
ST_DONE = "DONE"                        # 全部章节已批准

STATE_LABELS = {
    ST_INIT: "① 项目初始化",
    ST_OUTLINE_DRAFT: "② 大纲待审阅（HITL 暂停点）",
    ST_OUTLINE_APPROVED: "③ 大纲已批准 · 可开始撰写",
    ST_WRITING: "④ 章节撰写与逐章审批中",
    ST_DONE: "⑤ 全部章节已批准 · 可导出定稿",
}

LEVEL_BLOCKER = "BLOCKER"
LEVEL_WARNING = "WARNING"
LEVEL_INFO = "INFO"
LEVEL_ICON = {LEVEL_BLOCKER: "🛑", LEVEL_WARNING: "⚠️", LEVEL_INFO: "ℹ️"}
LEVEL_ORDER = {LEVEL_BLOCKER: 0, LEVEL_WARNING: 1, LEVEL_INFO: 2}

# 便于在 Streamlit 新旧版本间兼容
def _rerun() -> None:
    """触发页面重跑（兼容不同 Streamlit 版本）。"""
    fn = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
    if fn:
        fn()


# =============================================================================
# 二、通用工具函数
# =============================================================================

def now_ts() -> str:
    """本地时间戳，精确到毫秒，用于审计日志。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short(text: Optional[str], limit: int = 120) -> str:
    """截断展示用文本。"""
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + " …"


def count_words(text: str) -> int:
    """中文字符数 + 英文单词数，作为论文字数统计口径。"""
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z]+", text))
    return cn + en


def split_sentences(text: str) -> List[str]:
    """粗粒度分句，用于逐句合规检查。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def strip_markdown(text: str) -> str:
    """去掉 Markdown 标记，得到纯文本（用于字数统计与查重比对）。"""
    if not text:
        return ""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)          # 代码块
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)       # 标题
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)                    # 粗体
    t = re.sub(r"\*(.+?)\*", r"\1", t)                        # 斜体
    t = re.sub(r"`(.+?)`", r"\1", t)                          # 行内代码
    t = re.sub(r"^\s*[-*>]\s+", "", t, flags=re.M)            # 列表/引用
    return t


def normalize_for_similarity(text: str) -> Tuple[str, List[str]]:
    """
    归一化文本用于重复度比对。
    返回 (中文字符串, 英文词序列)。
    """
    plain = strip_markdown(text).lower()
    cn = re.sub(r"[^\u4e00-\u9fff]", "", plain)
    en_words = re.findall(r"[a-z]{2,}", plain)
    return cn, en_words


def unified_diff(old: str, new: str, name: str) -> str:
    """生成 AI 初稿 → 用户修改稿 的 unified diff，用于审计日志。"""
    diff = difflib.unified_diff(
        (old or "").splitlines(),
        (new or "").splitlines(),
        fromfile=name + " · AI初稿",
        tofile=name + " · 用户修改稿",
        lineterm="",
        n=1,
    )
    out = "\n".join(diff)
    return out if out.strip() else "（无实质改动）"


def longest_common_run(cn_a: str, cn_b: str) -> str:
    """返回两段中文文本的最长连续公共子串（自查重核心）。"""
    if not cn_a or not cn_b:
        return ""
    sm = difflib.SequenceMatcher(None, cn_a, cn_b, autojunk=False)
    match = sm.find_longest_match(0, len(cn_a), 0, len(cn_b))
    return cn_a[match.a: match.a + match.size]

def longest_common_word_run(words_a: List[str], words_b: List[str]) -> List[str]:
    """英文按词比对，返回最长连续公共词序列。"""
    if not words_a or not words_b:
        return []
    sm = difflib.SequenceMatcher(None, words_a, words_b, autojunk=False)
    match = sm.find_longest_match(0, len(words_a), 0, len(words_b))
    return words_a[match.a: match.a + match.size]


def safe_json_loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


# =============================================================================
# 三、SQLite 持久化层（审计日志 / 项目进度 / 稿件版本）
# =============================================================================

_DB_LOCK = threading.Lock()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    title        TEXT,
    major        TEXT,
    state        TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    state_json   TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_uid  TEXT,
    ts          TEXT,
    project_id  TEXT,
    actor       TEXT,      -- 操作角色：用户 / 规划师Agent / ...
    actor_role  TEXT,      -- 角色类型：HUMAN / AGENT / SYSTEM
    event_type  TEXT,      -- PROMPT / AI_OUTPUT / USER_EDIT / COMPLIANCE_CHECK / APPROVE ...
    stage       TEXT,      -- outline / chapter / format / system
    target      TEXT,      -- 目标对象：outline / chapter-1 / document ...
    prompt      TEXT,      -- 输入 Prompt
    ai_output   TEXT,      -- AI 原始输出
    user_edit   TEXT,      -- 用户修改内容（含 diff）
    status      TEXT,      -- 最终状态：success / blocked / approved / rejected ...
    detail      TEXT,      -- 扩展信息 JSON
    prev_hash   TEXT,      -- 前一条日志哈希（哈希链）
    record_hash TEXT       -- 本条日志哈希
);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    project_id    TEXT,
    kind          TEXT,    -- outline / chapter / references / final
    chapter_index INTEGER,
    version       INTEGER,
    status        TEXT,    -- draft / approved / superseded
    content       TEXT,
    note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_logs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_artifact_project ON artifacts(project_id, kind, chapter_index);
"""


@st.cache_resource(show_spinner=False)
def get_conn() -> sqlite3.Connection:
    """获取全局 SQLite 连接（Streamlit 会缓存该资源，避免每次重跑重建）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def init_db() -> None:
    conn = get_conn()
    with _DB_LOCK:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def db_execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    conn = get_conn()
    with _DB_LOCK:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def db_query(sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    conn = get_conn()
    with _DB_LOCK:
        cur = conn.execute(sql, params)
        return cur.fetchall()


# ------------------------------ 项目进度持久化 ------------------------------

def save_project_state(pstate: Dict[str, Any]) -> None:
    """把当前项目状态整体写入 SQLite（刷新/重启后可恢复）。"""
    meta = pstate.get("meta", {})
    pstate["meta"]["updated_at"] = today()
    db_execute(
        """
        INSERT INTO projects (project_id, title, major, state, created_at, updated_at, state_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id) DO UPDATE SET
            title=excluded.title,
            major=excluded.major,
            state=excluded.state,
            updated_at=excluded.updated_at,
            state_json=excluded.state_json
        """,
        (
            pstate["project_id"],
            meta.get("title", ""),
            meta.get("major", ""),
            pstate.get("state", ST_INIT),
            meta.get("created_at", today()),
            today(),
            json.dumps(pstate, ensure_ascii=False),
        ),
    )


def load_project_state(project_id: str) -> Optional[Dict[str, Any]]:
    rows = db_query("SELECT state_json FROM projects WHERE project_id = ?", (project_id,))
    if not rows:
        return None
    data = safe_json_loads(rows[0]["state_json"])
    return data if isinstance(data, dict) else None


def list_projects() -> List[sqlite3.Row]:
    return db_query(
        "SELECT project_id, title, major, state, updated_at FROM projects ORDER BY updated_at DESC"
    )


def delete_project(project_id: str) -> None:
    db_execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    db_execute("DELETE FROM audit_logs WHERE project_id = ?", (project_id,))
    db_execute("DELETE FROM artifacts WHERE project_id = ?", (project_id,))


def save_artifact(project_id: str, kind: str, chapter_index: int, version: int,
                  status: str, content: str, note: str = "") -> None:
    """保存稿件版本快照（每次生成/修改都会新增一条，形成版本历史）。"""
    db_execute(
        """INSERT INTO artifacts (ts, project_id, kind, chapter_index, version, status, content, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_ts(), project_id, kind, int(chapter_index), int(version), status, content, note),
    )


def list_artifacts(project_id: str, kind: Optional[str] = None) -> List[sqlite3.Row]:
    if kind:
        return db_query(
            "SELECT * FROM artifacts WHERE project_id=? AND kind=? ORDER BY id DESC",
            (project_id, kind),
        )
    return db_query("SELECT * FROM artifacts WHERE project_id=? ORDER BY id DESC", (project_id,))


# ------------------------------ 会话记忆（刷新恢复） ------------------------------

def remember_last_pid(pid: Optional[str]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LAST_SESSION_FILE, "w", encoding="utf-8") as fp:
            json.dump({"project_id": pid, "ts": now_ts()}, fp, ensure_ascii=False)
    except OSError:
        pass


def read_last_pid() -> Optional[str]:
    try:
        with open(LAST_SESSION_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data.get("project_id")
    except (OSError, ValueError):
        return None


# =============================================================================
# 四、日志记录员 Agent（确定性组件，不调用大模型）
# =============================================================================

HASH_FIELDS = ["record_uid", "ts", "project_id", "actor", "event_type", "stage",
               "target", "prompt", "ai_output", "user_edit", "status", "detail"]


class LogKeeperAgent:
    """
    日志记录员 Agent —— 专职把多智能体的全部交互落到 SQLite。

    设计要点：
      * 纯确定性逻辑（不依赖大模型，保证审计不丢失、不漂移）；
      * 每条记录写入 prev_hash 与 record_hash，构成哈希链，可校验篡改；
      * 同时写入 st.session_state 缓存，便于页面即时展示。
    """

    NAME = "日志记录员Agent"
    ROLE = "AGENT"
    MAX_FIELD_LEN = 60000  # 单字段存储上限，超出截断并标注

    @staticmethod
    def _clip(value: Optional[str]) -> str:
        if value is None:
            return ""
        value = str(value)
        if len(value) > LogKeeperAgent.MAX_FIELD_LEN:
            return value[: LogKeeperAgent.MAX_FIELD_LEN] + "\n...[已截断，完整内容见稿件版本表]"
        return value

    @staticmethod
    def _hash(record: Dict[str, Any], prev_hash: str) -> str:
        payload = "|".join(str(record.get(k, "")) for k in HASH_FIELDS) + "|" + prev_hash
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def log(self, *, actor: str, event_type: str, actor_role: str = "AGENT",
            stage: str = "", target: str = "", prompt: str = "", ai_output: str = "",
            user_edit: str = "", status: str = "", detail: Optional[Dict[str, Any]] = None,
            project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = project_id or "unknown"
        detail_json = json.dumps(detail or {}, ensure_ascii=False)

        rows = db_query(
            "SELECT record_hash FROM audit_logs WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)
        )
        prev_hash = rows[0]["record_hash"] if rows else "GENESIS"

        record = {
            "record_uid": uuid.uuid4().hex,
            "ts": now_ts(),
            "project_id": pid,
            "actor": actor,
            "event_type": event_type,
            "stage": stage,
            "target": target,
            "prompt": self._clip(prompt),
            "ai_output": self._clip(ai_output),
            "user_edit": self._clip(user_edit),
            "status": status,
            "detail": detail_json,
        }
        record_hash = self._hash(record, prev_hash)

        db_execute(
            """INSERT INTO audit_logs
               (record_uid, ts, project_id, actor, actor_role, event_type, stage, target,
                prompt, ai_output, user_edit, status, detail, prev_hash, record_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record["record_uid"], record["ts"], pid, actor, actor_role, event_type,
                stage, target, record["prompt"], record["ai_output"], record["user_edit"],
                status, detail_json, prev_hash, record_hash,
            ),
        )
        record["prev_hash"] = prev_hash
        record["record_hash"] = record_hash
        return record


LOG_KEEPER = LogKeeperAgent()


def fetch_logs(project_id: Optional[str] = None, limit: int = 2000) -> List[sqlite3.Row]:
    if project_id:
        return db_query(
            "SELECT * FROM audit_logs WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
    return db_query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))


def verify_hash_chain(project_id: str) -> Tuple[bool, List[int]]:
    """
    校验哈希链完整性：重算每条记录的 record_hash，
    任何字段被直接改库都会导致校验失败。
    """
    rows = db_query("SELECT * FROM audit_logs WHERE project_id=? ORDER BY id ASC", (project_id,))
    prev = "GENESIS"
    broken: List[int] = []
    for row in rows:
        rec = {k: row[k] for k in HASH_FIELDS}
        rec["detail"] = row["detail"] or "{}"
        expected = LogKeeperAgent._hash(rec, prev)
        if expected != row["record_hash"] or row["prev_hash"] != prev:
            broken.append(int(row["id"]))
        prev = row["record_hash"]
    return (len(broken) == 0 and len(rows) > 0), broken


def logs_to_json_bytes(rows: Sequence[sqlite3.Row]) -> bytes:
    data = [dict(r) for r in rows]
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def logs_to_csv_bytes(rows: Sequence[sqlite3.Row]) -> bytes:
    buf = io.StringIO()
    fields = ["id", "ts", "project_id", "actor", "actor_role", "event_type", "stage",
              "target", "status", "prompt", "ai_output", "user_edit", "detail",
              "prev_hash", "record_hash"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (r[k] if r[k] is not None else "") for k in fields})
    return buf.getvalue().encode("utf-8-sig")


# =============================================================================
# 五、大模型调用层（DeepSeek / OpenAI 兼容接口 + 离线演示模式）
# =============================================================================

class LLMError(RuntimeError):
    """大模型调用异常，UI 层捕获后给出友好提示并写入审计日志。"""


# --------- 离线演示模板（未配置 API Key 时使用，保证流程可跑通）---------

_MOCK_CHAPTER_TITLES = [
    "绪论", "文献综述", "理论基础与概念界定", "研究设计与方法",
    "数据来源与变量说明", "实证分析与结果讨论", "对策与建议", "结论与展望",
]

_MOCK_SECTIONS = ["研究背景与问题提出", "核心概念界定", "国内外研究现状述评", "本章小结"]

_MOCK_REFERENCES = """## 参考文献
[1] 张三, 李四. 人工智能在教育领域的应用研究[J]. 中国电化教育, 2022, 43(5): 12-19.
[2] 王五. 深度学习导论[M]. 北京: 电子工业出版社, 2021: 45-60.
[3] SMITH J, BROWN A. A survey of large language models[J]. Journal of Artificial Intelligence Research, 2023, 12(3): 100-125.
[4] 中国互联网络信息中心. 第52次中国互联网络发展状况统计报告[R]. 北京: 中国互联网络信息中心, 2023.
[5] 教育部. 关于加强高等学校毕业论文（设计）管理工作的通知[EB/OL]. (2021-03-01)[2024-05-01]. https://www.moe.gov.cn/.
[6] 赵六. 高校毕业论文质量管理研究[D]. 上海: 华东师范大学, 2020: 33-40.
"""


def _mock_stream(task: str, payload: Dict[str, Any]) -> Generator[str, None, None]:
    """离线演示：用本地模板模拟模型流式输出（会有 '[演示模式输出]' 明确标注）。"""
    title = payload.get("title") or "未命名论文"
    major = payload.get("major") or "相关专业"

    if task == "outline":
        n = int(payload.get("chapter_count") or 5)
        lines = ["[演示模式输出 · 未调用 DeepSeek API，仅用于跑通 HITL 流程]\n"]
        for i in range(1, n + 1):
            name = _MOCK_CHAPTER_TITLES[(i - 1) % len(_MOCK_CHAPTER_TITLES)]
            if i > len(_MOCK_CHAPTER_TITLES):
                name = "拓展研究章节%d" % (i - len(_MOCK_CHAPTER_TITLES))
            lines.append("## 第%d章 %s" % (i, name))
            for j, sec in enumerate(_MOCK_SECTIONS[:3], start=1):
                lines.append("### %d.%d %s" % (i, j, sec))
            lines.append("")
        text = "\n".join(lines)
        for chunk in re.findall(r".{1,80}", text, flags=re.S):
            yield chunk
            time.sleep(0.005)
        return

    if task == "chapter":
        idx = int(payload.get("chapter_index") or 1)
        chapter_title = payload.get("chapter_title") or ("第%d章" % idx)
        target = int(payload.get("target_words") or 800)
        sections = payload.get("sections") or _MOCK_SECTIONS[:3]
        body = ["[演示模式输出 · 未调用 DeepSeek API]\n"]
        body.append("## %s\n" % chapter_title)
        per = max(150, target // max(1, len(sections)))
        for j, sec in enumerate(sections, start=1):
            body.append("### %d.%d %s\n" % (idx, j, sec))
            para = (
                "围绕《%s》这一主题，%s领域的研究者已从多个角度展开讨论[1]。"
                "就%s而言，既有研究普遍认为理论框架的选取会直接影响结论的稳健性[2][3]。"
                "有调查结果显示，超过六成的受访对象认可该路径的可行性[2]。"
                "综合而言，现有文献为本研究提供了方法借鉴，但在样本覆盖与情境适用性上仍存在讨论空间[4]。"
                % (title, major, sec)
            )
            body.append(para + "\n")
            filler = (
                "进一步而言，%s层面的差异会通过中介机制作用于最终结果，"
                "因此在研究设计中需要控制相关干扰变量，以保证结论的可解释性[1][3]。"
                % sec
            )
            body.append(filler + "\n")
        if idx == 1:
            body.append(_MOCK_REFERENCES)
        text = "\n".join(body)
        for chunk in re.findall(r".{1,80}", text, flags=re.S):
            yield chunk
            time.sleep(0.005)
        return

    if task == "compliance":
        text = (
            "[演示模式输出 · 未调用 DeepSeek API]\n"
            "[INFO] 逻辑一致性：章节结构与大纲条目基本对应，未发现明显结构错位。\n"
            "[WARNING] 论证充分性：部分论点的支撑材料偏薄，建议补充近三年文献。\n"
        )
        for chunk in re.findall(r".{1,60}", text, flags=re.S):
            yield chunk
            time.sleep(0.005)
        return

    # generic
    text = "[演示模式输出] 当前为离线演示模式，请配置 DEEPSEEK_API_KEY 以启用真实模型。"
    for chunk in re.findall(r".{1,40}", text, flags=re.S):
        yield chunk


class LLMClient:
    """
    DeepSeek 客户端（OpenAI SDK，base_url=https://api.deepseek.com，model=deepseek-chat）。
    API Key 从环境变量 DEEPSEEK_API_KEY 读取；未配置时自动进入离线演示模式。
    """

    def __init__(self) -> None:
        # ⚠️ 用户自行填写位置：环境变量 DEEPSEEK_API_KEY
        #    Windows CMD : set DEEPSEEK_API_KEY=sk-xxxx
        #    PowerShell  : $env:DEEPSEEK_API_KEY="sk-xxxx"
        #    或在项目根目录建 .env 文件（见 .env.example）
        self.api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        self.offline = not bool(self.api_key)
        self._client = None

    # ---- 惰性初始化，避免离线模式下强依赖 openai 包 ----
    @property
    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI  # 延迟导入
            except ImportError as exc:  # pragma: no cover
                raise LLMError("未安装 openai 包，请执行：pip install -r requirements.txt") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
        return self._client

    def stream(self, messages: List[Dict[str, str]], *, temperature: float = 0.6,
               max_tokens: int = 3000, task: str = "generic",
               mock_payload: Optional[Dict[str, Any]] = None) -> Generator[str, None, None]:
        """
        流式输出。Streamlit 侧边收边渲染，避免长时间白屏。
        出错时抛出 LLMError，由 UI 捕获并记入审计日志。
        """
        if self.offline:
            yield from _mock_stream(task, mock_payload or {})
            return

        try:
            resp = self.client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            for chunk in resp:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
        except LLMError:
            raise
        except Exception as exc:  # 网络/鉴权/额度等
            raise LLMError(
                "调用 DeepSeek 失败：%s\n\n排查建议：1) 确认环境变量 DEEPSEEK_API_KEY 正确；"
                "2) 确认可访问 https://api.deepseek.com；3) 确认账户额度充足。" % exc
            ) from exc

    def complete(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """非流式（内部仍走流式再拼接，逻辑统一）。"""
        return "".join(self.stream(messages, **kwargs)).strip()


# =============================================================================
# 六、合规规则引擎（本地确定性检查，不消耗 token）
# =============================================================================

REF_HEAD_RE = re.compile(r"^\s*#{0,6}\s*(参考文献|references|引用文献)\s*$", re.I | re.M)
REF_ENTRY_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)\s*[.、])\s*(.+)$")
REF_TYPE_RE = re.compile(
    r"\[(?:EB/OL|DB/OL|J/OL|M/OL|C/OL|N/OL|R/OL|S/OL|P/OL|A/OL|G/OL|DB|CP/DK|J|M|D|C|N|R|S|P|A|G|Z)\]"
)
REF_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
CITE_RE = re.compile(r"\[(\d+(?:\s*[,\-–]\s*\d+)*)\]")
APA_HEAD_RE = re.compile(r"^[^()]{2,120}\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)\s*[.．]")
HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")

# 学术诚信风险表述规则（require_no_cite=True 表示仅当该句没有任何引用标注时才命中）
INTEGRITY_RULES: List[Dict[str, Any]] = [
    {
        "id": "placeholder",
        "pattern": r"(TODO|待补充|待完善|待填|此处省略|略去|XXX|xxx|【\s*】|\?\?\?|\bTBD\b)",
        "level": LEVEL_BLOCKER,
        "category": "未完成内容",
        "message": "检测到占位符或未完成内容，不得作为定稿提交。",
        "suggestion": "请补全该部分内容后重新提交合规审查。",
        "require_no_cite": False,
    },
    {
        "id": "fabrication",
        "pattern": r"((统计|调查|数据|实验|问卷|访谈|测试)\s*(结果|数据)?\s*(显示|表明|证明|发现|说明))",
        "level": LEVEL_BLOCKER,
        "category": "数据来源不明",
        "message": "出现数据性论断但该句未标注引用来源，疑似数据来源不明或数据造假。",
        "suggestion": "补充数据出处与文献引用（如 [1]）；如数据为本人实验所得，请注明样本量、时间与获取方式。",
        "require_no_cite": True,
    },
    {
        "id": "fake_precision",
        "pattern": r"(\d+(?:\.\d+)?\s*%|百分之[一二三四五六七八九十百零\d]+)",
        "level": LEVEL_WARNING,
        "category": "数据来源不明",
        "message": "出现百分比数据但该句未标注来源，请核实真实性并可溯源。",
        "suggestion": "标注数据来源文献或调研说明，避免使用无法溯源的精确数字。",
        "require_no_cite": True,
    },
    {
        "id": "overclaim",
        "pattern": r"(毋庸置疑|毫无疑问|不言而喻|众所周知|完全证明|彻底解决|绝对正确|史上首次|填补了?空白|必然导致)",
        "level": LEVEL_WARNING,
        "category": "过度断言",
        "message": "存在绝对化/夸张化表述，学术写作应留有讨论余地。",
        "suggestion": "改为审慎表述，如“在一定程度上”“现有证据表明”。",
        "require_no_cite": False,
    },
    {
        "id": "vague_attribution",
        "pattern": r"((大量|众多|许多|无数|部分)\s*(研究|学者|文献|专家)\s*(表明|显示|指出|证明|认为))",
        "level": LEVEL_WARNING,
        "category": "模糊归因",
        "message": "使用模糊归因（未指向具体文献），属于学术规范高风险表述。",
        "suggestion": "改为“张三等[1]认为……”，把归因落到具体文献上。",
        "require_no_cite": False,
    },
    {
        "id": "personal",
        "pattern": r"(据(?:我|笔者)(?:的)?(?:观察|经验|了解|实践|调研))",
        "level": LEVEL_WARNING,
        "category": "非学术证据",
        "message": "出现个人经验性表述，不能作为学术论据。",
        "suggestion": "改为引用文献或说明数据来源。",
        "require_no_cite": False,
    },
    {
        "id": "emotion",
        "pattern": r"(非常|极其|超级|太)(好|差|重要|厉害)",
        "level": LEVEL_INFO,
        "category": "口语化表达",
        "message": "存在口语化/情绪化表达。",
        "suggestion": "替换为客观的学术表述。",
        "require_no_cite": False,
    },
]

# 正式论文关键结构（整篇检查用）
DOC_REQUIRED_PARTS = [
    ("摘要", r"(摘要|abstract)"),
    ("关键词", r"(关键词|keywords?)"),
    ("参考文献", r"(参考文献|references)"),
]


def split_reference_section(text: str) -> Tuple[str, str]:
    """把正文与参考文献部分拆开。返回 (正文, 参考文献文本)。"""
    if not text:
        return "", ""
    matches = list(REF_HEAD_RE.finditer(text))
    if not matches:
        return text, ""
    last = matches[-1]
    return text[: last.start()], text[last.start():]


def parse_reference_entries(refs_text: str) -> List[Dict[str, Any]]:
    """解析参考文献条目，返回 [{no, text}]。"""
    entries: List[Dict[str, Any]] = []
    if not refs_text:
        return entries
    current: Optional[Dict[str, Any]] = None
    for raw in refs_text.splitlines():
        line = raw.strip()
        if not line or REF_HEAD_RE.match(line):
            continue
        m = REF_ENTRY_RE.match(line)
        if m:
            if current:
                entries.append(current)
            no = m.group(1) or m.group(2)
            current = {"no": int(no) if no and no.isdigit() else None, "text": m.group(3).strip()}
        elif current:
            current["text"] += " " + line  # 条目换行续写
    if current:
        entries.append(current)
    return entries


def extract_intext_citations(body_text: str) -> List[int]:
    """提取正文中的顺序编码制引用编号（如 [1]、[2-4]、[5,7]）。"""
    nums: List[int] = []
    for m in CITE_RE.finditer(body_text or ""):
        for part in re.split(r"[,，\-–]", m.group(1)):
            part = part.strip()
            if part.isdigit():
                nums.append(int(part))
    return nums


class RuleEngine:
    """
    本地合规规则引擎（确定性、零 token 消耗）。
    输出 findings: [{level, category, message, evidence, suggestion, source}]
    """

    # ---------------- 参考文献格式 ----------------

    @staticmethod
    def check_references(refs_text: str, style: str) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        entries = parse_reference_entries(refs_text)
        is_gbt = "GB/T" in style or "7714" in style

        if not refs_text.strip():
            return findings

        if not entries:
            findings.append({
                "level": LEVEL_WARNING, "category": "参考文献格式",
                "message": "检测到“参考文献”章节，但未能解析出任何规范条目（应为 [1] 作者. 题名[文献类型]. … 形式）。",
                "evidence": short(refs_text, 80), "suggestion": "按所选著录格式规范逐条重排参考文献。",
                "source": "规则引擎",
            })
            return findings

        for e in entries:
            txt = e["text"]
            where = "[%s] %s" % (e["no"] if e["no"] is not None else "?", short(txt, 60))

            if len(txt) < 15:
                findings.append({
                    "level": LEVEL_WARNING, "category": "参考文献格式",
                    "message": "参考文献条目过短，可能缺少必要著录项。",
                    "evidence": where, "suggestion": "补齐作者、题名、来源、年份等著录项。",
                    "source": "规则引擎",
                })
                continue

            if not REF_YEAR_RE.search(txt):
                findings.append({
                    "level": LEVEL_WARNING, "category": "参考文献格式",
                    "message": "参考文献缺少出版/发布年份。",
                    "evidence": where, "suggestion": "补充 4 位年份，如 2023。",
                    "source": "规则引擎",
                })

            if is_gbt:
                if not REF_TYPE_RE.search(txt):
                    findings.append({
                        "level": LEVEL_WARNING, "category": "参考文献格式",
                        "message": "GB/T 7714 要求标注文献类型标识（如 [J] 期刊、[M] 专著、[D] 学位论文、[EB/OL] 电子资源）。",
                        "evidence": where, "suggestion": "在题名后补充文献类型标识。",
                        "source": "规则引擎",
                    })
                if "[J]" in txt and not re.search(r"\d+\s*\(\s*\d+\s*\)", txt):
                    findings.append({
                        "level": LEVEL_INFO, "category": "参考文献格式",
                        "message": "期刊论文建议著录“年, 卷(期): 起止页码”。",
                        "evidence": where, "suggestion": "补全卷(期)与页码。",
                        "source": "规则引擎",
                    })
            else:
                if not APA_HEAD_RE.match(txt):
                    findings.append({
                        "level": LEVEL_WARNING, "category": "参考文献格式",
                        "message": "APA 格式要求条目以“作者 (年份).”开头。",
                        "evidence": where, "suggestion": "调整为 APA 第7版著录格式。",
                        "source": "规则引擎",
                    })
                if "(" not in txt and "（" not in txt:
                    findings.append({
                        "level": LEVEL_WARNING, "category": "参考文献格式",
                        "message": "APA 格式的年份需置于括号内。",
                        "evidence": where, "suggestion": "改为 (2023) 形式。",
                        "source": "规则引擎",
                    })
        return findings

    # ---------------- 引用交叉核对 ----------------

    @staticmethod
    def check_citations(body_text: str, refs_text: str, report_uncited: bool = True) -> List[Dict[str, Any]]:
        """
        正文引用 ↔ 文献表 交叉核对。
        report_uncited：只有整篇检查时才报告「文献表有但正文未引用」——
        单章检查时只引用了文献库的一部分，报这个是误报。
        """
        findings: List[Dict[str, Any]] = []
        cited = extract_intext_citations(body_text)
        entries = parse_reference_entries(refs_text)
        ref_nos = {e["no"] for e in entries if e["no"] is not None}

        if cited and not ref_nos:
            findings.append({
                "level": LEVEL_WARNING, "category": "引用核对",
                "message": "正文使用了顺序编码制引用标注，但当前尚无参考文献列表，无法完成引用核对。",
                "evidence": "首次出现：[%d]" % cited[0],
                "suggestion": "请生成或粘贴参考文献列表，再执行合规审查。",
                "source": "规则引擎",
            })
            return findings

        if not cited and not ref_nos:
            return findings

        # 正文引用 → 文献表 必须存在
        missing = sorted({n for n in cited if n not in ref_nos})
        if missing:
            findings.append({
                "level": LEVEL_BLOCKER, "category": "引用不存在",
                "message": "正文引用了参考文献列表中不存在的编号：%s。属于学术规范硬性问题，已拦截。"
                           % ", ".join("[%d]" % n for n in missing),
                "evidence": "缺失编号：%s" % ", ".join(str(n) for n in missing),
                "suggestion": "补齐对应文献条目，或修正正文引用编号。",
                "source": "规则引擎",
            })

        # 文献表 → 正文引用（列而未引）：仅整篇检查时才有意义
        uncited = sorted(n for n in ref_nos if n not in set(cited))
        if uncited and report_uncited:
            findings.append({
                "level": LEVEL_WARNING, "category": "文献未引用",
                "message": "参考文献列表中存在未被正文引用的条目：%s。" % ", ".join("[%d]" % n for n in uncited),
                "evidence": "未引用编号：%s" % ", ".join(str(n) for n in uncited),
                "suggestion": "在正文相应位置引用，或从文献表中删除。",
                "source": "规则引擎",
            })
        return findings

    # ---------------- 引用密度（文献综述章节） ----------------

    @staticmethod
    def check_citation_density(body_text: str, kind: str, target_name: str = "") -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        if kind != "chapter":
            return findings
        words = count_words(body_text)
        if words < 300:
            return findings

        paragraphs = [p for p in re.split(r"\n\s*\n", body_text) if count_words(p) >= 40]
        if not paragraphs:
            return findings

        cited_paras = sum(1 for p in paragraphs if CITE_RE.search(p))
        ratio = cited_paras / len(paragraphs)
        n_cites = len(extract_intext_citations(body_text))

        is_review = any(k in (target_name or "") for k in ("综述", "研究现状", "文献"))
        if is_review and (ratio < 0.5 or n_cites < 3):
            findings.append({
                "level": LEVEL_BLOCKER, "category": "引用密度不足",
                "message": "文献综述类章节引用密度过低（含引用段落占比 %.0f%%，引用 %d 处），"
                           "高度疑似未标注引用来源，已拦截。" % (ratio * 100, n_cites),
                "evidence": "段落数 %d，含引用段落 %d" % (len(paragraphs), cited_paras),
                "suggestion": "为每一处观点补充具体文献引用（GB/T 7714 顺序编码制）。",
                "source": "规则引擎",
            })
        elif ratio < 0.25:
            findings.append({
                "level": LEVEL_WARNING, "category": "引用密度不足",
                "message": "含引用段落占比仅 %.0f%%，论证支撑偏弱。" % (ratio * 100),
                "evidence": "引用共 %d 处" % n_cites,
                "suggestion": "补充文献支撑，避免出现无来源的观点陈述。",
                "source": "规则引擎",
            })
        return findings

    # ---------------- 学术诚信风险表述 ----------------

    @staticmethod
    def check_integrity(text: str) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        seen = set()
        for sentence in split_sentences(text):
            has_cite = bool(CITE_RE.search(sentence))
            for rule in INTEGRITY_RULES:
                m = re.search(rule["pattern"], sentence)
                if not m:
                    continue
                if rule["require_no_cite"] and has_cite:
                    continue
                key = (rule["id"], m.group(0))
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "level": rule["level"],
                    "category": rule["category"],
                    "message": rule["message"],
                    "evidence": short(sentence, 100),
                    "suggestion": rule["suggestion"],
                    "source": "规则引擎",
                })
        return findings

    # ---------------- 重复度（自查重 / 抄袭倾向） ----------------

    @staticmethod
    def check_similarity(text: str, compare_sources: Optional[List[Tuple[str, str]]] = None,
                         cn_threshold: int = 13, en_threshold: int = 8) -> List[Dict[str, Any]]:
        """
        连续重复检测：中文连续 >= 13 字 / 英文连续 >= 8 词 完全相同 → 疑似抄袭（阻断）。
        compare_sources: [(来源名称, 文本)]，可为已发表文献节选或本人其他章节。
        """
        findings: List[Dict[str, Any]] = []
        if not compare_sources:
            return findings
        cn_a, en_a = normalize_for_similarity(text)

        for name, other in compare_sources:
            if not other or not other.strip():
                continue
            cn_b, en_b = normalize_for_similarity(other)
            run = longest_common_run(cn_a, cn_b)
            if len(run) >= cn_threshold:
                findings.append({
                    "level": LEVEL_BLOCKER, "category": "疑似抄袭/重复",
                    "message": "与【%s】存在 %d 个汉字的连续相同片段，超过查重警戒线（%d 字），已拦截。"
                               % (name, len(run), cn_threshold),
                    "evidence": run[:60],
                    "suggestion": "对该段进行实质性改写并规范标注引用；直接引用须加引号并注明出处。",
                    "source": "规则引擎",
                })
                continue
            words = longest_common_word_run(en_a, en_b)
            if len(words) >= en_threshold:
                findings.append({
                    "level": LEVEL_BLOCKER, "category": "疑似抄袭/重复",
                    "message": "与【%s】存在 %d 个连续相同英文单词，超过查重警戒线（%d 词），已拦截。"
                               % (name, len(words), en_threshold),
                    "evidence": " ".join(words[:20]),
                    "suggestion": "改写该片段并规范标注引用。",
                    "source": "规则引擎",
                })
        return findings

    # ---------------- 论文章节内自我重复 ----------------

    @staticmethod
    def check_self_repeat(chapters: Sequence[Dict[str, Any]], current_index: int) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        cur = next((c for c in chapters if c["index"] == current_index), None)
        if not cur or not cur.get("text"):
            return findings
        cn_a, en_a = normalize_for_similarity(cur["text"])
        for c in chapters:
            if c["index"] >= current_index or not c.get("text"):
                continue
            cn_b, en_b = normalize_for_similarity(c["text"])
            run = longest_common_run(cn_a, cn_b)
            if len(run) >= 40:  # 章节间自我重复阈值放宽到 40 字
                findings.append({
                    "level": LEVEL_WARNING, "category": "章节间重复",
                    "message": "与《%s》存在 %d 字连续重复，注意避免自我重复。" % (c["title"], len(run)),
                    "evidence": run[:60],
                    "suggestion": "合并重复论述，改为交叉引用前文（如“如前文 3.2 节所述”）。",
                    "source": "规则引擎",
                })
            words = longest_common_word_run(en_a, en_b)
            if len(words) >= 25:
                findings.append({
                    "level": LEVEL_WARNING, "category": "章节间重复",
                    "message": "与《%s》存在 %d 个连续相同英文词。" % (c["title"], len(words)),
                    "evidence": " ".join(words[:20]),
                    "suggestion": "改写或改为交叉引用。",
                    "source": "规则引擎",
                })
        return findings

    # ---------------- 格式规范 ----------------

    @staticmethod
    def check_format(text: str, kind: str = "chapter") -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        body, _ = split_reference_section(text)

        # 1) 中英文标点混用
        mixed = re.search(r"[\u4e00-\u9fff]\s*[,;:]\s*[\u4e00-\u9fff]", body)
        if mixed:
            findings.append({
                "level": LEVEL_WARNING, "category": "标点规范",
                "message": "中文语境中混用了半角标点（, ; :），应使用全角标点（，；：）。",
                "evidence": short(mixed.group(0), 40),
                "suggestion": "统一全文标点：中文使用全角，英文与数字使用半角。",
                "source": "规则引擎",
            })
        half_paren = re.search(r"[\u4e00-\u9fff]\([^()]{1,30}[\u4e00-\u9fff]\)", body)
        if half_paren:
            findings.append({
                "level": LEVEL_INFO, "category": "标点规范",
                "message": "中文内容使用了半角括号，建议改为全角括号（）。",
                "evidence": short(half_paren.group(0), 40),
                "suggestion": "统一为全角括号。",
                "source": "规则引擎",
            })

        # 2) 标题层级连续性
        levels: List[int] = []
        for line in text.splitlines():
            m = HEADING_RE.match(line)
            if m:
                levels.append(len(m.group(1)))
        for prev, cur in zip(levels, levels[1:]):
            if cur - prev > 1:
                findings.append({
                    "level": LEVEL_WARNING, "category": "结构规范",
                    "message": "标题层级出现跳级（H%d → H%d），不符合论文层级规范。" % (prev, cur),
                    "evidence": "H%d → H%d" % (prev, cur),
                    "suggestion": "按 章(H2) → 节(H3) → 目(H4) 逐级递进。",
                    "source": "规则引擎",
                })
                break

        # 3) 超长段落
        for para in re.split(r"\n\s*\n", body):
            if count_words(para) > 600:
                findings.append({
                    "level": LEVEL_INFO, "category": "可读性",
                    "message": "存在超长段落（约 %d 字），建议拆分以提升可读性。" % count_words(para),
                    "evidence": short(para, 50),
                    "suggestion": "按论证层次拆分为 2-3 段。",
                    "source": "规则引擎",
                })
                break

        # 4) 引用标注位置：不应紧贴中文（[1]前应有标点或文字边界）
        bad_cite = re.search(r"[\u4e00-\u9fff]\[\d+\]", body)
        if bad_cite:
            findings.append({
                "level": LEVEL_INFO, "category": "引用规范",
                "message": "引用标注紧贴汉字，建议置于标点之后或作为上标处理。",
                "evidence": short(bad_cite.group(0), 30),
                "suggestion": "调整为“……结论[1]。”或使用上标形式。",
                "source": "规则引擎",
            })

        # 5) 整篇结构完整性
        if kind == "document":
            for name, pattern in DOC_REQUIRED_PARTS:
                if not re.search(pattern, text, flags=re.I):
                    findings.append({
                        "level": LEVEL_WARNING, "category": "结构完整性",
                        "message": "定稿缺少必要组成部分：%s。" % name,
                        "evidence": "未检索到“%s”" % name,
                        "suggestion": "补充%s内容后再导出定稿。" % name,
                        "source": "规则引擎",
                    })
        return findings

    # ---------------- 与大纲的逻辑一致性 ----------------

    @staticmethod
    def check_outline_consistency(text: str, chapter: Dict[str, Any],
                                  outline_chapters: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        target = next((c for c in outline_chapters if c["index"] == chapter["index"]), None)
        if not target:
            return findings

        heads = [HEADING_RE.match(l).group(2).strip()
                 for l in text.splitlines() if HEADING_RE.match(l) and len(HEADING_RE.match(l).group(1)) == 2]
        if heads and target["title"] not in heads[0]:
            findings.append({
                "level": LEVEL_WARNING, "category": "逻辑一致性",
                "message": "章节标题与已批准大纲不一致（大纲：%s）。" % target["title"],
                "evidence": short(heads[0], 40),
                "suggestion": "保持与已批准大纲一致，或在规划师页重新修订并批准大纲。",
                "source": "规则引擎",
            })

        # 大纲中的小节是否覆盖
        wanted = target.get("sections") or []
        missing = [s for s in wanted if s and s not in text]
        if wanted and len(missing) > len(wanted) // 2:
            findings.append({
                "level": LEVEL_WARNING, "category": "逻辑一致性",
                "message": "初稿未覆盖大纲中多数小节（缺失 %d/%d）。" % (len(missing), len(wanted)),
                "evidence": "缺失小节：" + "；".join(missing[:3]),
                "suggestion": "补写缺失小节，或返回大纲页调整结构。",
                "source": "规则引擎",
            })
        return findings

    # ---------------- 字数 ----------------

    @staticmethod
    def check_word_count(text: str, target_words: int) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        if not target_words or target_words <= 0:
            return findings
        n = count_words(text)
        if n < target_words * 0.6:
            findings.append({
                "level": LEVEL_WARNING, "category": "字数偏离",
                "message": "当前约 %d 字，低于目标 %d 字（达成率 %.0f%%）。" % (n, target_words, n * 100.0 / target_words),
                "evidence": "字数 %d / 目标 %d" % (n, target_words),
                "suggestion": "补充论证细节、案例与文献支撑。",
                "source": "规则引擎",
            })
        elif n > target_words * 1.6:
            findings.append({
                "level": LEVEL_INFO, "category": "字数偏离",
                "message": "当前约 %d 字，明显超出目标 %d 字。" % (n, target_words),
                "evidence": "字数 %d / 目标 %d" % (n, target_words),
                "suggestion": "精简冗余表述，保持结构紧凑。",
                "source": "规则引擎",
            })
        return findings

    # ---------------- 参考文献来源可追溯性 ----------------

    @staticmethod
    def check_reference_provenance(refs_text: str) -> List[Dict[str, Any]]:
        """
        没有 DOI / 原文链接的参考文献无法核验真实性，必须提示人工确认。
        这是「防编造文献」的最后一道提示：真实文献一律能给出 DOI 或可访问链接。
        """
        entries = parse_reference_entries(refs_text)
        if not entries:
            return []
        no_src = [e for e in entries
                  if not re.search(r"(10\.\d{4,9}/|doi|https?://)", e["text"], re.I)]
        if not no_src:
            return []
        return [{
            "level": LEVEL_WARNING, "category": "来源不可追溯",
            "message": "有 %d 条参考文献没有 DOI 或原文链接，系统无法核验其真实性，请逐条确认后再提交。"
                       % len(no_src),
            "evidence": "涉及编号：" + "、".join("[%s]" % e["no"] for e in no_src[:8]),
            "suggestion": "到知网 / 谷歌学术找到原文并补上 DOI 或链接；"
                          "如发现是编造的文献，请立即删除——编造参考文献属于学术不端。",
            "source": "规则引擎",
        }]

    # ---------------- 汇总入口 ----------------

    @classmethod
    def run_all(cls, *, text: str, kind: str, style: str, target_name: str = "",
                target_words: int = 0, project_references: str = "",
                compare_sources: Optional[List[Tuple[str, str]]] = None,
                chapters: Optional[Sequence[Dict[str, Any]]] = None,
                outline_chapters: Optional[Sequence[Dict[str, Any]]] = None,
                chapter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        统一执行全部本地规则。
        kind: outline / chapter / document / references
        """
        body, local_refs = split_reference_section(text or "")
        refs = project_references.strip() or local_refs

        findings: List[Dict[str, Any]] = []
        findings += cls.check_integrity(text or "")
        findings += cls.check_format(text or "", kind=kind)

        if kind in ("chapter", "document", "references"):
            findings += cls.check_references(refs, style)
            findings += cls.check_reference_provenance(refs)
            findings += cls.check_citations(body, refs, report_uncited=(kind == "document"))
            findings += cls.check_citation_density(body, kind, target_name)
            findings += cls.check_similarity(text or "", compare_sources)
            if kind == "chapter" and chapters is not None and chapter is not None:
                findings += cls.check_self_repeat(chapters, chapter["index"])
            if kind == "chapter" and outline_chapters and chapter:
                findings += cls.check_outline_consistency(text or "", chapter, outline_chapters)
            if target_words:
                findings += cls.check_word_count(body, target_words)

        # 去重 + 排序（BLOCKER 在前）
        uniq: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for f in findings:
            key = (f["category"], f["message"][:40])
            if key not in uniq:
                uniq[key] = f
        result = list(uniq.values())
        result.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), x["category"]))
        return result


# =============================================================================
# 七、多智能体定义
# =============================================================================

class BaseAgent:
    """智能体基类：统一封装 system prompt 构造与流式调用。"""

    key = "base"
    name = "智能体"
    role = "AGENT"

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    @property
    def system_prompt(self) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def build_messages(self, user_prompt: str) -> List[Dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def stream(self, user_prompt: str, *, temperature: float = 0.6,
               max_tokens: int = 3000, task: str = "generic",
               mock_payload: Optional[Dict[str, Any]] = None) -> Generator[str, None, None]:
        return self.llm.stream(
            self.build_messages(user_prompt), temperature=temperature,
            max_tokens=max_tokens, task=task, mock_payload=mock_payload,
        )

    def complete(self, user_prompt: str, **kwargs) -> str:
        return "".join(self.stream(user_prompt, **kwargs)).strip()


COMMON_RULES = """
【写作底线（必须遵守）】
1. 严禁编造文献：只使用我提供的参考文献；如需引用，必须使用列表中的编号。
2. 严禁编造数据：任何统计数据都要标注来源编号；没有来源就不要写数字结论。
3. 严禁抄袭：不得整段照搬他人文字；直接引用须加引号并标注编号。
4. 全部输出使用简体中文（专业术语可保留英文），Markdown 格式。
5. 不要输出“作为AI”“以下是”等元话语，直接给正文。
"""


class PlannerAgent(BaseAgent):
    """规划师 Agent：与学生沟通论文题目，生成 / 修订大纲。"""

    key = "planner"
    name = "规划师Agent"
    role = "AGENT"

    @property
    def system_prompt(self) -> str:
        return (
            "你是一位资深本科毕业论文指导教师（规划师）。你的职责是与学生协作确定论文题目、"
            "研究思路与章节大纲，帮助学生把宽泛的兴趣收敛为可执行的研究问题。\n"
            "你只负责【大纲层面】的结构设计，不撰写章节正文。\n"
            "输出要求：\n"
            "  - 使用 Markdown；每章以 '## 第N章 章名' 起一行；每章下的小节以 '### N.M 小节名' 起一行；\n"
            "  - 章数严格等于我要求的章数，每章 3-5 个小节；\n"
            "  - 不要输出字数说明、不要输出任何额外解释文字。\n"
            + COMMON_RULES
        )

    def generate_outline(self, meta: Dict[str, Any], extra: str = "",
                         feedback: str = "") -> Generator[str, None, None]:
        prompt = (
            "【论文基本信息】\n"
            "- 题目：%s\n- 专业方向：%s\n- 目标总字数：%s 字\n- 参考文献格式：%s\n"
            "- 章节数量：%s 章\n- 研究类型：%s\n"
            "%s"
            "请生成完整论文大纲。"
            % (
                meta.get("title"), meta.get("major"), meta.get("target_words"),
                meta.get("citation_style"), meta.get("chapter_count"),
                meta.get("research_type") or "未指定",
                ("- 其他要求：%s\n" % extra) if extra else "",
            )
        )
        if feedback:
            prompt += (
                "\n【学生对本轮的修改意见（务必吸收）】\n%s\n"
                "请在保持整体学术结构的前提下，针对上述意见重写大纲。\n" % feedback
            )
        mock = {
            "title": meta.get("title"), "major": meta.get("major"),
            "chapter_count": meta.get("chapter_count"),
        }
        return self.stream(prompt, temperature=0.55, max_tokens=MAX_TOKENS_OUTLINE,
                           task="outline", mock_payload=mock)


class WriterAgent(BaseAgent):
    """撰写员 Agent：依据已批准大纲逐章撰写正文。"""

    key = "writer"
    name = "撰写员Agent"
    role = "AGENT"

    @property
    def system_prompt(self) -> str:
        return (
            "你是一位学术写作能力扎实的本科毕业论文撰写员。你的职责是严格按照【已批准大纲】"
            "撰写指定章节的初稿，做到论证有层次、语言客观、术语统一。\n"
            "输出要求：\n"
            "  - 第一行为 '## 第N章 章名'；小节使用 '### N.M 小节名'，必须与大纲一致；\n"
            "  - 每个小节至少两个自然段，段落之间空一行；\n"
            "  - 使用顺序编码制引用时写作 [1]、[2]，禁止编造不存在的编号；\n"
            "  - 只输出该章正文，不要输出其他章节，不要输出解释性文字。\n"
            + COMMON_RULES
        )

    def write_chapter(self, meta: Dict[str, Any], outline_text: str, chapter: Dict[str, Any],
                      target_words: int, references: str = "",
                      extra: str = "", feedback: str = "") -> Generator[str, None, None]:
        heading = chapter_display_title(chapter)
        prompt = (
            "【论文信息】题目：%s ｜ 专业方向：%s ｜ 文献格式：%s\n\n"
            "【已批准大纲（全文，仅供定位上下文）】\n%s\n\n"
            "【本次要写的章节】%s\n"
            "【本章需覆盖的小节】\n%s\n"
            "【本章目标字数】约 %d 字\n\n"
            "【可用参考文献（只能引用这些编号）】\n%s\n"
            % (
                meta.get("title"), meta.get("major"), meta.get("citation_style"),
                short(outline_text, 4000), heading,
                "\n".join("- " + s for s in (chapter.get("sections") or ["（自行组织小节）"])),
                target_words, references or "（暂无，可在论述中先不标注编号）",
            )
        )
        if extra:
            prompt += "\n【学生附加要求】\n%s\n" % extra
        if feedback:
            prompt += (
                "\n【学生对上一稿的修改意见（必须逐条落实）】\n%s\n" % feedback
            )
        prompt += ("\n请以 '## %s' 作为本章第一行标题（不要重复书写“第N章”），"
                   "然后开始撰写正文。" % heading)
        mock = {
            "title": meta.get("title"), "major": meta.get("major"),
            "chapter_index": chapter["index"], "chapter_title": heading,
            "target_words": target_words, "sections": chapter.get("sections") or [],
        }
        return self.stream(prompt, temperature=0.7, max_tokens=MAX_TOKENS_CHAPTER,
                           task="chapter", mock_payload=mock)


class ComplianceAgent(BaseAgent):
    """
    合规审查员 Agent：本地规则引擎 + 大模型深度审查 双层结构。
    本地规则负责硬拦截（BLOCKER），大模型负责语义层面的逻辑一致性建议。
    """

    key = "compliance"
    name = "合规审查员Agent"
    role = "AGENT"

    @property
    def system_prompt(self) -> str:
        return (
            "你是一位严格的高校学术合规审查员，负责审查本科毕业论文的学术规范性。\n"
            "审查维度：① 引用与参考文献规范（{style}）② 学术诚信风险（抄袭倾向、数据来源不明、"
            "模糊归因、过度断言）③ 论证与大纲的逻辑一致性 ④ 格式规范。\n"
            "【严重程度判定的硬性要求——务必遵守】\n"
            "  [BLOCKER] 只用于**学术诚信与引用真实性**问题：编造或无法核实的引用编号、"
            "编造数据/文献、疑似抄袭、正文引用了文献表中不存在的文献。\n"
            "  其它一切问题（论证不充分、逻辑瑕疵、缺少统计检验、术语不统一、格式细节、"
            "写作质量）一律用 [WARNING] 或 [INFO]，**不得**标为 BLOCKER。\n"
            "输出要求：每一行一个问题，格式严格为：\n"
            "  [BLOCKER] 类别：问题描述 ｜ 证据：原文片段 ｜ 建议：修改建议\n"
            "  [WARNING] 类别：问题描述 ｜ 证据：原文片段 ｜ 建议：修改建议\n"
            "  [INFO] 类别：问题描述 ｜ 建议：修改建议\n"
            "没有发现问题的层级可以省略。最后一行输出 '[SUMMARY] 一句话总体结论'。\n"
            "只报告确实存在的问题，不要为了凑数而编造问题。不要输出其他内容。"
        )

    def llm_review(self, *, text: str, kind: str, style: str, outline_text: str = "",
                   target_name: str = "", references: str = "") -> Generator[str, None, None]:
        sys_prompt = self.system_prompt.replace("{style}", style)
        user_prompt = (
            "【审查对象】%s：%s\n【著录格式要求】%s\n\n"
            "【已批准大纲（用于判断逻辑一致性）】\n%s\n\n"
            "【可核验的参考文献列表（正文引用编号应对应此表；表为空时说明尚未提供文献）】\n%s\n\n"
            "【待审文本】\n%s\n"
            % (
                "大纲" if kind == "outline" else "章节正文", target_name or "-", style,
                short(outline_text, 2000) or "（无）",
                short(references, 6000) or "（本次未提供参考文献列表，请勿据此判定引用造假，"
                                            "只提示「未提供文献列表，无法核验」即可）",
                short(text, 12000),
            )
        )
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}]
        return self.llm.stream(messages, temperature=0.2, max_tokens=MAX_TOKENS_REVIEW,
                               task="compliance", mock_payload={})

    # ---- 大模型审查结果解析（容错：解析不了就整体作为 INFO 保留）----
    @staticmethod
    def parse_llm_findings(text: str) -> Tuple[List[Dict[str, Any]], str]:
        findings: List[Dict[str, Any]] = []
        summary = ""
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("[SUMMARY]"):
                summary = line.replace("[SUMMARY]", "").strip()
                continue
            m = re.match(r"^\[(BLOCKER|WARNING|INFO)\]\s*(.+)$", line)
            if not m:
                continue
            level = m.group(1)
            body = m.group(2)
            evidence = ""
            suggestion = ""
            parts = re.split(r"[｜|]", body)
            category = parts[0].split("：", 1)[0].strip() if parts else "模型审查"
            desc = parts[0].split("：", 1)[1].strip() if "：" in parts[0] else parts[0].strip()
            for p in parts[1:]:
                if p.strip().startswith("证据"):
                    evidence = p.split("：", 1)[-1].strip()
                elif p.strip().startswith("建议"):
                    suggestion = p.split("：", 1)[-1].strip()
            findings.append({
                "level": level, "category": "模型审查·%s" % category,
                "message": desc, "evidence": evidence, "suggestion": suggestion,
                "source": "大模型审查",
            })
        return findings, summary


# =============================================================================
# 八、工作流状态机（HITL 门禁的核心）
# =============================================================================

def new_project_state(meta: Dict[str, Any]) -> Dict[str, Any]:
    """初始化项目状态。"""
    pid = uuid.uuid4().hex[:12]
    now = today()
    meta = dict(meta)
    meta.setdefault("created_at", now)
    meta["updated_at"] = now
    return {
        "project_id": pid,
        "meta": meta,
        "state": ST_INIT,
        "mode": "hitl",          # hitl = 逐步人工模式；auto = 一键生成模式
        "outline": {"text": "", "version": 0, "status": "empty",
                    "approved_at": "", "approved_by": "", "feedback": ""},
        "chapters": [],
        "references": "",        # 参考文献 Markdown（喂给大模型的就是它）
        "ref_records": [],       # 结构化文献库（带 DOI/URL，用于溯源校验与再导出）
        "reviews": {},           # {"outline": {...}, "chapter_1": {...}, "document": {...}}
        "history": [],           # 轻量事件流（供概览页展示）
    }


def get_ref_records(ps: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取结构化文献库；若只有文本（用户手工编辑过），则即时解析回结构化。"""
    recs = ps.get("ref_records")
    if isinstance(recs, list) and recs:
        return recs
    text = ps.get("references") or ""
    if text.strip() and lit is not None:
        parsed = lit.parse_reference_text(text)
        if parsed:
            ps["ref_records"] = parsed
            return parsed
    return []


def set_ref_records(ps: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> None:
    """更新结构化文献库，并同步生成 Markdown 文本（两者的唯一入口，避免不一致）。"""
    recs = list(records)
    ps["ref_records"] = recs
    if recs and lit is not None:
        style = ps.get("meta", {}).get("citation_style", CITATION_STYLES[0])
        ps["references"] = lit.build_reference_list(recs, style)
    elif not recs:
        ps["references"] = ""


def reference_records_block(ps: Dict[str, Any]) -> str:
    """给大模型看的文献清单（带 [n] 编号，与 ps['references'] 的编号一一对应）。"""
    recs = get_ref_records(ps)
    if recs and lit is not None:
        return lit.records_to_text_block(recs)
    return ps.get("references", "")


def get_review(ps: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    return (ps.get("reviews") or {}).get(key)


def review_is_fresh(ps: Dict[str, Any], key: str, version: int) -> bool:
    """审查结论必须对应当前版本，否则视为过期（HITL：改稿必须重审）。"""
    rv = get_review(ps, key)
    return bool(rv) and int(rv.get("version", -1)) == int(version)


def review_blockers(ps: Dict[str, Any], key: str, version: int) -> List[Dict[str, Any]]:
    if not review_is_fresh(ps, key, version):
        return [{"level": LEVEL_BLOCKER, "category": "审查缺失",
                 "message": "当前版本尚未通过合规审查（或修改后审查结论已过期），不能批准。",
                 "evidence": "版本 v%s" % version, "suggestion": "请点击“运行合规审查”。",
                 "source": "系统"}]
    rv = get_review(ps, key) or {}
    return [f for f in rv.get("findings", []) if f.get("level") == LEVEL_BLOCKER]


def parse_outline(text: str, fallback_count: int = 5) -> List[Dict[str, Any]]:
    """
    把大纲 Markdown 解析为结构化章节列表。
    支持 '## 第N章 标题' 与 '### N.M 小节'；解析失败时按章数生成占位结构。
    """
    chapters: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in (text or "").splitlines():
        m = HEADING_RE.match(line.strip())
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2).strip()
        if level == 2:
            current = {"index": len(chapters) + 1, "title": title, "sections": []}
            chapters.append(current)
        elif level == 3 and current is not None:
            current["sections"].append(re.sub(r"^\d+(\.\d+)*\s*", "", title))

    if not chapters:
        for i in range(1, max(1, int(fallback_count or 5)) + 1):
            chapters.append({"index": i, "title": "第%d章（待学生补充标题）" % i, "sections": []})
    return chapters


def chapter_display_title(chapter: Dict[str, Any]) -> str:
    """章节显示标题：若大纲标题中已含“第N章”，则不重复拼接。"""
    title = (chapter.get("title") or "").strip()
    if re.match(r"^第\s*[0-9一二三四五六七八九十百]+\s*章", title):
        return title
    return "第%d章 %s" % (chapter.get("index", 0), title)


def allocate_word_targets(total_words: int, chapters: Sequence[Dict[str, Any]]) -> List[int]:
    """按小节点数加权分配各章目标字数。"""
    if not chapters:
        return []
    weights = [max(1, len(c.get("sections") or [])) for c in chapters]
    total_w = sum(weights)
    targets = [max(200, int(total_words * w / total_w)) for w in weights]
    return targets


def compute_workflow(ps: Dict[str, Any]) -> Dict[str, Any]:
    """计算当前流程状态与所有门禁开关（UI 与审计共用）。"""
    outline = ps.get("outline") or {}
    chapters = ps.get("chapters") or []
    out_version = int(outline.get("version", 0))
    out_approved = outline.get("status") == "approved"

    approved_idx = [c["index"] for c in chapters if c.get("status") == "approved"]
    next_pending = next((c for c in chapters if c.get("status") != "approved"), None)

    return {
        "state": ps.get("state", ST_INIT),
        "state_label": STATE_LABELS.get(ps.get("state", ST_INIT), ps.get("state")),
        "outline_approved": out_approved,
        "outline_review_fresh": review_is_fresh(ps, "outline", out_version),
        "outline_blockers": review_blockers(ps, "outline", out_version) if out_version else [],
        "chapter_count": len(chapters),
        "approved_count": len(approved_idx),
        "next_index": next_pending["index"] if next_pending else None,
        "all_approved": bool(chapters) and len(approved_idx) == len(chapters),
    }


def sync_state(ps: Dict[str, Any]) -> None:
    """根据稿件实际状态刷新流程状态机（单一数据源，避免状态错乱）。"""
    wf = compute_workflow(ps)
    if not ps.get("outline", {}).get("text"):
        ps["state"] = ST_INIT
    elif not wf["outline_approved"]:
        ps["state"] = ST_OUTLINE_DRAFT
    elif not wf["chapter_count"]:
        ps["state"] = ST_OUTLINE_APPROVED
    elif wf["all_approved"]:
        ps["state"] = ST_DONE
    else:
        ps["state"] = ST_WRITING if wf["approved_count"] > 0 else ST_OUTLINE_APPROVED


def push_history(ps: Dict[str, Any], text: str) -> None:
    ps.setdefault("history", []).append({"ts": now_ts(), "text": text})
    ps["history"] = ps["history"][-80:]


# =============================================================================
# 九、会话状态与持久化
# =============================================================================

SS = st.session_state


def persist(ps: Optional[Dict[str, Any]] = None, remember: bool = True) -> None:
    """保存项目进度到 SQLite（每个 HITL 动作后都必须调用）。"""
    ps = ps or SS.get("pstate")
    if not ps:
        return
    sync_state(ps)
    save_project_state(ps)
    if remember:
        SS["active_pid"] = ps["project_id"]
        remember_last_pid(ps["project_id"])


def audit(*, actor: str, event_type: str, actor_role: str = "AGENT", stage: str = "",
          target: str = "", prompt: str = "", ai_output: str = "",
          user_edit: str = "", status: str = "", detail: Optional[Dict[str, Any]] = None) -> None:
    """写审计日志的统一入口（自动带上当前项目 id）。"""
    ps = SS.get("pstate") or {}
    LOG_KEEPER.log(
        actor=actor, actor_role=actor_role, event_type=event_type, stage=stage,
        target=target, prompt=prompt, ai_output=ai_output, user_edit=user_edit,
        status=status, detail=detail, project_id=ps.get("project_id") or "unknown",
    )


def init_session() -> None:
    SS.setdefault("pstate", None)
    SS.setdefault("active_pid", None)
    SS.setdefault("llm", LLMClient())
    init_db()
    if SS.get("pstate") is None:
        pid = read_last_pid()
        if pid:
            loaded = normalize_state(load_project_state(pid))
            if loaded:
                SS["pstate"] = loaded
                SS["active_pid"] = pid
    else:
        SS["pstate"] = normalize_state(SS.get("pstate"))


def normalize_state(ps: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """兼容旧版本项目存档：补齐后来新增的字段，避免打开老项目时报 KeyError。"""
    if not isinstance(ps, dict):
        return None
    ps.setdefault("mode", "hitl")
    ps.setdefault("references", "")
    ps.setdefault("ref_records", [])
    ps.setdefault("reviews", {})
    ps.setdefault("history", [])
    ps.setdefault("chapters", [])
    ps.setdefault("outline", {"text": "", "version": 0, "status": "empty",
                              "approved_at": "", "approved_by": "", "feedback": ""})
    ps.get("meta", {}).setdefault("keywords", "")
    ps.get("meta", {}).setdefault("year_from", 0)
    return ps


def switch_project(pid: str) -> None:
    ps = normalize_state(load_project_state(pid))
    if ps:
        SS["pstate"] = ps
        SS["active_pid"] = pid
        remember_last_pid(pid)
        _rerun()


def require_project() -> Optional[Dict[str, Any]]:
    ps = SS.get("pstate")
    if not ps:
        st.info("👈 请在左侧边栏**新建项目**，开始你的毕业论文写作流程。")
        return None
    return ps


# =============================================================================
# 十、UI 组件与页面
# =============================================================================

def render_findings(findings: Sequence[Dict[str, Any]], title: str = "合规审查结论") -> None:
    if not findings:
        st.success("✅ 未发现合规问题（本地规则引擎已全部通过）。")
        return
    n_block = sum(1 for f in findings if f["level"] == LEVEL_BLOCKER)
    n_warn = sum(1 for f in findings if f["level"] == LEVEL_WARNING)
    n_info = sum(1 for f in findings if f["level"] == LEVEL_INFO)
    st.markdown(
        "**%s** — 🛑 阻断 %d ｜ ⚠️ 警告 %d ｜ ℹ️ 提示 %d"
        % (title, n_block, n_warn, n_info)
    )
    for f in findings:
        text = "%s **[%s] %s** — %s" % (
            LEVEL_ICON.get(f["level"], "•"), f["level"], f.get("category", ""), f.get("message", "")
        )
        if f.get("evidence"):
            text += "\n\n&nbsp;&nbsp;&nbsp;&nbsp;↳ 证据：`%s`" % short(str(f["evidence"]), 120)
        if f.get("suggestion"):
            text += "\n\n&nbsp;&nbsp;&nbsp;&nbsp;↳ 建议：%s" % f["suggestion"]
        if f.get("source"):
            text += "\n\n&nbsp;&nbsp;&nbsp;&nbsp;↳ 来源：%s" % f["source"]
        if f["level"] == LEVEL_BLOCKER:
            st.error(text)
        elif f["level"] == LEVEL_WARNING:
            st.warning(text)
        else:
            st.info(text)


def stream_to_ui(gen: Generator[str, None, None], label: str = "🤖 AI 正在生成…") -> Optional[str]:
    """
    流式渲染到页面：边收边显示，结束后返回完整文本。
    出错返回 None（调用方据此决定是否落库）。
    """
    box = st.empty()
    box.markdown("_%s_" % label + " ▌")
    buf: List[str] = []
    try:
        for chunk in gen:
            buf.append(chunk)
            box.markdown("".join(buf) + " ▌")
    except LLMError as exc:
        box.empty()
        st.error(str(exc))
        audit(actor="系统", actor_role="SYSTEM", event_type="LLM_ERROR",
              status="error", detail={"error": str(exc)})
        return None
    text = "".join(buf).strip()
    box.markdown(text if text else "_（模型无输出）_")
    return text


def run_compliance_check(ps: Dict[str, Any], *, target_key: str, text: str, kind: str,
                         target_name: str, target_words: int = 0,
                         use_llm: bool = True, compare_sources: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """
    执行一次完整合规审查 = 本地规则引擎 + （可选）大模型深度审查。
    结果写入 ps['reviews'][target_key] 并记审计日志。
    """
    meta = ps["meta"]
    version = 0
    chapter = None
    if target_key == "outline":
        version = int(ps["outline"].get("version", 0))
    elif target_key.startswith("chapter_"):
        idx = int(target_key.split("_")[1])
        chapter = next((c for c in ps["chapters"] if c["index"] == idx), None)
        version = int(chapter.get("version", 0)) if chapter else 0

    findings = RuleEngine.run_all(
        text=text, kind=kind, style=meta.get("citation_style", CITATION_STYLES[0]),
        target_name=target_name, target_words=target_words,
        project_references=ps.get("references", ""),
        compare_sources=compare_sources,
        chapters=ps.get("chapters"),
        outline_chapters=ps.get("chapters"),
        chapter=chapter,
    )

    llm_text, summary = "", ""
    if use_llm:
        st.caption("第二步：调用合规审查员 Agent 进行语义层深度审查（大模型）…")
        agent = ComplianceAgent(SS["llm"])
        # 审查大纲时不要把大纲自身当成「已批准大纲」参照，否则模型只会回一句“与自身完全一致”
        outline_ref = "" if kind == "outline" else ps["outline"].get("text", "")
        llm_text = stream_to_ui(
            agent.llm_review(text=text, kind=kind,
                             style=meta.get("citation_style", CITATION_STYLES[0]),
                             outline_text=outline_ref,
                             target_name=target_name,
                             references=ps.get("references", "")),
            label="🛡️ 合规审查员 Agent 审查中…",
        ) or ""
        llm_findings, summary = ComplianceAgent.parse_llm_findings(llm_text)
        findings = findings + llm_findings
        findings.sort(key=lambda x: (LEVEL_ORDER.get(x["level"], 9), x["category"]))

    review = {
        "version": version, "ts": now_ts(), "kind": kind, "target": target_name,
        "findings": findings, "llm_text": llm_text, "summary": summary,
        "blockers": sum(1 for f in findings if f["level"] == LEVEL_BLOCKER),
        "warnings": sum(1 for f in findings if f["level"] == LEVEL_WARNING),
    }
    ps.setdefault("reviews", {})[target_key] = review

    audit(actor=ComplianceAgent.name, actor_role="AGENT", event_type="COMPLIANCE_CHECK",
          stage=kind, target=target_key,
          prompt="审查对象：%s（版本 v%d，著录格式 %s）" % (target_name, version, meta.get("citation_style")),
          ai_output=llm_text, status="blocked" if review["blockers"] else "passed",
          detail={"findings": findings, "summary": summary, "blockers": review["blockers"]})
    persist(ps)
    return review


# ------------------------------ 侧边栏 ------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("## 🎓 %s" % APP_NAME)
        st.caption("合规设计 · 人在回路 · 审计日志　v%s" % APP_VERSION)

        llm: LLMClient = SS["llm"]
        if llm.offline:
            st.warning("当前为**离线演示模式**（未检测到环境变量 `DEEPSEEK_API_KEY`）。\n\n"
                       "将使用本地模板模拟模型输出，用于跑通 HITL 流程与审计日志。\n\n"
                       "配置 API Key 后重启即可接入 `deepseek-chat`。")
        else:
            st.success("已连接 DeepSeek：`%s`\n\n模型：`%s`" % (DEEPSEEK_BASE_URL, DEEPSEEK_MODEL))

        st.divider()
        st.markdown("### 📁 项目管理")

        with st.expander("➕ 新建项目", expanded=(SS.get("pstate") is None)):
            with st.form("create_project", clear_on_submit=False):
                title = st.text_input("论文题目 *", placeholder="例如：短视频使用对大学生注意力的影响研究")
                major = st.text_input("专业方向", placeholder="例如：教育技术学 / 计算机科学与技术")
                c1, c2 = st.columns(2)
                target_words = c1.number_input("目标字数", min_value=3000, max_value=100000,
                                               value=12000, step=1000)
                chapter_count = c2.number_input("章节数", min_value=3, max_value=12, value=5, step=1)
                citation_style = st.selectbox("参考文献格式", CITATION_STYLES, index=0)
                research_type = st.selectbox(
                    "研究类型", ["实证研究（问卷/实验）", "案例研究", "文献研究", "设计开发类", "其他"],
                    index=0,
                )
                student = st.text_input("学生姓名（写入审计日志）", placeholder="选填")
                advisor = st.text_input("指导教师", placeholder="选填")
                keywords = st.text_input("文献检索关键词（留空则用论文题目）",
                                         placeholder="例如：短视频 注意力 大学生")
                year_from = st.number_input("文献起始年份（0=不限）", min_value=0, max_value=2030,
                                            value=2018, step=1)
                submitted = st.form_submit_button("创建项目并进入规划师环节", use_container_width=True)

            if submitted:
                if not title.strip():
                    st.error("请先填写论文题目。")
                else:
                    meta = {
                        "title": title.strip(), "major": major.strip() or "未填写",
                        "target_words": int(target_words), "chapter_count": int(chapter_count),
                        "citation_style": citation_style, "research_type": research_type,
                        "student": student.strip(), "advisor": advisor.strip(),
                        "keywords": keywords.strip(), "year_from": int(year_from),
                    }
                    ps = new_project_state(meta)
                    SS["pstate"] = ps
                    persist(ps)
                    audit(actor=student.strip() or "用户", actor_role="HUMAN",
                          event_type="PROJECT_CREATE", stage="system", target=ps["project_id"],
                          prompt=json.dumps(meta, ensure_ascii=False), status="success")
                    st.success("项目已创建，请前往【🧭 规划师 · 大纲】生成大纲。")
                    _rerun()

        rows = list_projects()
        if rows:
            label_map = {r["project_id"]: "%s（%s · %s）"
                         % (r["title"][:26] or "未命名", r["project_id"][:6], r["updated_at"][5:16])
                         for r in rows}
            pid_of_label = {v: k for k, v in label_map.items()}
            current = SS.get("active_pid")
            default_pos = list(label_map.keys()).index(current) if current in label_map else 0
            picked_label = st.selectbox("打开已有项目", list(pid_of_label.keys()), index=default_pos,
                                        key="project_switch_select")
            picked = pid_of_label[picked_label]
            c1, c2 = st.columns(2)
            if c1.button("切换/载入", use_container_width=True):
                if picked != SS.get("active_pid"):
                    audit(actor="用户", actor_role="HUMAN", event_type="PROJECT_SWITCH",
                          stage="system", target=picked, status="success")
                    switch_project(picked)
                else:
                    st.toast("已经是当前项目。")
            if c2.button("删除项目", use_container_width=True):
                delete_project(picked)
                if SS.get("active_pid") == picked:
                    SS["pstate"] = None
                    SS["active_pid"] = None
                    remember_last_pid(None)
                st.toast("项目及其审计日志已删除。")
                _rerun()

        ps = SS.get("pstate")
        if ps:
            st.divider()
            wf = compute_workflow(ps)
            st.markdown("### 📊 当前进度")
            st.progress(wf["approved_count"] / max(1, wf["chapter_count"]),
                        text="已批准章节 %d / %d" % (wf["approved_count"], wf["chapter_count"]))
            st.caption("流程状态：%s" % wf["state_label"])
            if wf["next_index"]:
                st.caption("下一个待办：第 %d 章（需先批准上一章）" % wf["next_index"])

        with st.expander("ℹ️ 设计理念说明"):
            st.markdown(
                "- **合规设计**：规则引擎先做硬拦截（引用不存在、占位符、数据无来源、连续重复），"
                "大模型再做语义层审查。\n"
                "- **人在回路**：一次只生成一个环节，未批准不得进入下一步；改稿后审查结论自动失效，必须重审。\n"
                "- **审计日志**：所有 Prompt/输出/修改/结论落库 SQLite，并用哈希链防篡改。\n"
                "- ⚠️ 本系统不替代学校查重系统与导师审核，"
                "所有 AI 生成内容必须由作者本人核对、修改并承担学术责任。"
            )


# ------------------------------ 概览页 ------------------------------

def render_overview(ps: Dict[str, Any]) -> None:
    meta = ps["meta"]
    wf = compute_workflow(ps)
    draft_words = sum(count_words(c.get("text", "")) for c in ps["chapters"])
    outline_words = count_words(ps["outline"].get("text", ""))

    st.markdown("### 📋 项目概览")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("目标字数", "%s 字" % meta.get("target_words"))
    c2.metric("已形成初稿", "%s 字" % (draft_words + outline_words))
    c3.metric("已批准章节", "%d / %d" % (wf["approved_count"], wf["chapter_count"]))
    c4.metric("流程状态", wf["state_label"].split(" ")[-1])

    st.markdown(
        "**题目**：%s　｜　**专业方向**：%s　｜　**文献格式**：%s　｜　**章节数**：%s　｜　**研究类型**：%s"
        % (meta.get("title"), meta.get("major"), meta.get("citation_style"),
           meta.get("chapter_count"), meta.get("research_type"))
    )
    if meta.get("student"):
        st.caption("学生：%s　指导教师：%s" % (meta.get("student"), meta.get("advisor") or "—"))

    st.divider()
    st.markdown("### 🚦 流程门禁（HITL）")
    steps = [
        ("① 生成大纲", bool(ps["outline"].get("text")), "规划师 Agent 生成结构草案"),
        ("② 审查大纲", wf["outline_review_fresh"], "合规审查员 Agent 检查结构与规范"),
        ("③ 批准大纲", wf["outline_approved"], "必须由你人工确认（硬门禁：未批准不能写正文）"),
        ("④ 逐章撰写", wf["approved_count"] > 0, "撰写员 Agent 一次只写一章"),
        ("⑤ 逐章批准", wf["all_approved"], "未批准第 N 章，无法解锁第 N+1 章"),
        ("⑥ 定稿导出", wf["state"] == ST_DONE, "全部章节批准后方可导出"),
    ]
    for name, done, desc in steps:
        st.markdown("%s **%s** — %s" % ("✅" if done else "⬜", name, desc))

    st.divider()
    st.markdown("### 🕒 最近操作")
    logs = fetch_logs(ps["project_id"], limit=8)
    if not logs:
        st.caption("暂无记录。")
    for r in logs:
        st.markdown("- `%s` **%s**（%s）→ %s" % (r["ts"], r["actor"], r["event_type"], r["status"] or "—"))


# ------------------------------ 规划师页 ------------------------------

def render_planner_tab(ps: Dict[str, Any]) -> None:
    meta = ps["meta"]
    outline = ps["outline"]
    st.markdown("### 🧭 规划师 Agent · 研究大纲")
    st.caption("规划师负责把题目收敛为可执行的章节结构。AI 生成后进入 **HITL 暂停点**，"
               "由你决定：修改 / 重新生成 / 批准通过。")

    with st.expander("① 生成 / 重新生成大纲", expanded=not outline.get("text")):
        extra = st.text_area("附加要求（可选）", key="planner_extra", height=80,
                             placeholder="例如：增加一节“研究局限性”；第三章侧重问卷设计；不要写对策建议章节")
        feedback = st.text_area("对上一版大纲的修改意见（重新生成时生效）", key="planner_feedback",
                                height=80, placeholder="例如：第二章拆成两章；把第4章改为案例研究法")
        if st.button("🪄 让规划师生成大纲" if not outline.get("text") else "🔁 按修改意见重新生成大纲",
                     type="primary", use_container_width=True):
            agent = PlannerAgent(SS["llm"])
            st.markdown("**Prompt 已提交（原文将完整写入审计日志）**：题目《%s》/ %s / %s 字 / %s 章"
                        % (meta["title"], meta["major"], meta["target_words"], meta["chapter_count"]))
            text = stream_to_ui(
                agent.generate_outline(meta, extra=extra, feedback=feedback),
                label="🧭 规划师 Agent 正在构思大纲…",
            )
            if text:
                outline["text"] = text
                outline["version"] = int(outline.get("version", 0)) + 1
                outline["status"] = "draft"
                outline["feedback"] = feedback
                ps["reviews"].pop("outline", None)  # 新版本 → 旧审查结论作废
                save_artifact(ps["project_id"], "outline", 0, outline["version"], "draft", text,
                              note="AI生成")
                audit(actor=PlannerAgent.name, actor_role="AGENT", event_type="AI_OUTPUT",
                      stage="outline", target="outline",
                      prompt="generate_outline | extra=%s | feedback=%s" % (extra, feedback),
                      ai_output=text, status="draft",
                      detail={"version": outline["version"], "words": count_words(text)})
                push_history(ps, "规划师生成大纲 v%d" % outline["version"])
                persist(ps)
                st.success("大纲已生成（v%d）。请审阅后执行合规审查并批准。" % outline["version"])
                _rerun()

    if not outline.get("text"):
        st.info("尚未生成大纲。请先点击上方按钮。")
        return

    st.divider()
    version = int(outline.get("version", 0))
    status = outline.get("status")
    badge = {"draft": "⏳ 待审阅", "approved": "✅ 已批准", "rejected": "↩️ 已驳回"}.get(status, status)
    st.markdown("#### ② 审阅大纲（当前 v%d，%s）" % (version, badge))

    with st.expander("查看 AI 生成的大纲全文", expanded=True):
        st.markdown(outline["text"])

    # ---- 用户编辑 ----
    st.markdown("#### ③ 人工修改（你的修改会被记录 diff）")
    edited = st.text_area("可直接在此编辑大纲 Markdown", value=outline["text"],
                          height=280, key="outline_edit_%d" % version,
                          help="修改后点击下方“保存我的修改”，系统会生成新版本并要求重新审查。")
    c1, c2 = st.columns([1, 1])
    if c1.button("💾 保存我的修改（生成新版本）", use_container_width=True):
        if edited.strip() == outline["text"].strip():
            st.toast("内容未变化。")
        else:
            diff = unified_diff(outline["text"], edited, "大纲 v%d" % version)
            outline["text"] = edited
            outline["version"] = version + 1
            outline["status"] = "draft"
            ps["reviews"].pop("outline", None)
            save_artifact(ps["project_id"], "outline", 0, outline["version"], "draft", edited,
                          note="用户修改")
            audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="USER_EDIT",
                  stage="outline", target="outline",
                  prompt="用户编辑大纲", ai_output="", user_edit=diff, status="draft",
                  detail={"from_version": version, "to_version": outline["version"]})
            push_history(ps, "用户修改大纲 → v%d" % outline["version"])
            persist(ps)
            st.success("已保存为用户修改版 v%d，请重新运行合规审查。" % outline["version"])
            _rerun()

    if c2.button("↩️ 驳回本版（要求 AI 重写）", use_container_width=True):
        outline["status"] = "rejected"
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="REJECT",
              stage="outline", target="outline", user_edit=outline.get("feedback", ""),
              status="rejected", detail={"version": version})
        persist(ps)
        st.warning("已驳回 v%d。请在上方填写修改意见并点击“按修改意见重新生成大纲”。" % version)
        _rerun()

    # ---- 合规审查 ----
    st.divider()
    st.markdown("#### ④ 合规审查（批准前必须执行）")
    use_llm = st.checkbox("同时启用大模型深度审查（额外消耗 token）", value=True, key="outline_use_llm")
    if st.button("🛡️ 运行合规审查（大纲）", use_container_width=True):
        run_compliance_check(ps, target_key="outline", text=outline["text"], kind="outline",
                             target_name="论文大纲", use_llm=use_llm)
        _rerun()

    rv = get_review(ps, "outline")
    if rv:
        fresh = review_is_fresh(ps, "outline", version)
        if not fresh:
            st.warning("⚠️ 审查结论已过期（大纲在审查后被修改），请重新运行合规审查。")
        render_findings(rv.get("findings", []), title="大纲审查结论（%s）" % rv["ts"])
        if rv.get("summary"):
            st.info("模型总体结论：%s" % rv["summary"])
    else:
        st.caption("尚未执行合规审查。")

    # ---- 批准 ----
    st.divider()
    st.markdown("#### ⑤ 人工批准（硬门禁）")
    blockers = review_blockers(ps, "outline", version)
    confirmed = st.checkbox(
        "我已逐条核对以上大纲内容，确认其为本人研究思路的真实表达，并对其学术合规性负责。",
        key="outline_confirm_%d" % version,
    )
    can_approve = (not blockers) and confirmed and outline["status"] != "approved"
    if blockers:
        st.error("🛑 存在 %d 项阻断问题，批准按钮已禁用。请修改或重新生成后再审查。" % len(blockers))
    if any(c.get("text") for c in ps.get("chapters") or []):
        st.warning("⚠️ 已有章节写了内容。重新批准大纲会按「章节序号 + 标题」尽量保留原有稿件，"
                   "标题被改动的章节将重建为空稿（原稿仍保留在稿件版本历史中，可导出找回）。")
    if st.button("✅ 批准通过大纲（解锁章节撰写）", type="primary",
                 disabled=not can_approve, use_container_width=True):
        # 解析大纲 → 重建章节结构 + 分配字数（尽量保留已写内容，避免覆盖用户成果）
        chapters = parse_outline(outline["text"], meta.get("chapter_count", 5))
        targets = allocate_word_targets(int(meta.get("target_words", 12000)), chapters)
        old_map = {(c["index"], c["title"]): c for c in (ps.get("chapters") or [])}
        new_chapters: List[Dict[str, Any]] = []
        kept = 0
        for i, c in enumerate(chapters):
            target_i = targets[i] if i < len(targets) else 1200
            prev_ch = old_map.get((c["index"], c["title"]))
            if prev_ch:
                kept += 1
                carried = dict(prev_ch)
                carried["sections"] = c["sections"] or prev_ch.get("sections") or []
                carried["target_words"] = target_i
                new_chapters.append(carried)
            else:
                new_chapters.append({
                    "index": c["index"], "title": c["title"], "sections": c["sections"],
                    "target_words": target_i, "text": "", "version": 0, "status": "pending",
                    "approved_at": "", "feedback": "",
                })
        ps["chapters"] = new_chapters
        outline["status"] = "approved"
        outline["approved_at"] = now_ts()
        outline["approved_by"] = meta.get("student") or "用户"
        save_artifact(ps["project_id"], "outline", 0, version, "approved", outline["text"],
                      note="用户批准")
        audit(actor=outline["approved_by"], actor_role="HUMAN", event_type="APPROVE",
              stage="outline", target="outline", user_edit="学术诚信核对声明：已勾选",
              status="approved",
              detail={"version": version, "chapters": [c["title"] for c in chapters],
                      "kept_existing_drafts": kept})
        push_history(ps, "用户批准大纲 v%d，解锁章节撰写（保留既有稿件 %d 章）" % (version, kept))
        persist(ps)
        st.success("大纲已批准%s。现在可以前往【✍️ 撰写员 · 章节】撰写第 1 章。"
                   % ("（已保留 %d 章既有稿件）" % kept if kept else ""))
        _rerun()


# ------------------------------ 撰写员页 ------------------------------

def render_writer_tab(ps: Dict[str, Any]) -> None:
    st.markdown("### ✍️ 撰写员 Agent · 章节初稿")
    outline = ps["outline"]
    chapters = ps["chapters"]

    if not chapters:
        st.info("🔒 请先在【🧭 规划师 · 大纲】中生成并**批准**大纲。"
                "未批准大纲前，系统不会生成任何正文（人在回路硬门禁）。")
        return

    meta = ps["meta"]
    labels = []
    for c in chapters:
        icon = "✅" if c["status"] == "approved" else ("📝" if c["text"] else "⬜")
        labels.append("%s %s" % (icon, chapter_display_title(c)))
    default_pos = next((i for i, c in enumerate(chapters) if c["status"] != "approved"), 0)
    picked = st.selectbox("选择章节", options=labels, index=default_pos,
                          key="writer_chapter_select")
    chapter = chapters[labels.index(picked)]
    version = int(chapter.get("version", 0))

    c1, c2, c3 = st.columns(3)
    c1.metric("本章目标字数", "%d 字" % chapter.get("target_words", 0))
    c2.metric("当前字数", "%d 字" % count_words(chapter.get("text", "")))
    c3.metric("状态", {"pending": "未撰写", "draft": "待审阅", "approved": "已批准"}.get(chapter["status"], chapter["status"]))

    # ---- 门禁判定 ----
    prev_unapproved = [c for c in chapters if c["index"] < chapter["index"] and c["status"] != "approved"]
    locked = bool(prev_unapproved)
    if locked:
        st.error(
            "🔒 第 %d 章尚未批准，无法撰写第 %d 章。请先完成并批准：%s"
            % (prev_unapproved[0]["index"], chapter["index"],
               "、".join("第%d章" % c["index"] for c in prev_unapproved))
        )
    if chapter["status"] == "approved":
        st.success("✅ 本章已批准。如需修改，请先点击下方“撤回批准”（会写入审计日志）。")

    if not (ps.get("references") or "").strip():
        st.warning(
            "⚠️ 当前**项目参考文献库为空**，请注意一个必然结果：\n\n"
            "撰写员 Agent 的底线是「绝不编造文献」，所以它在无文献可用时**不会写任何引用标注**；"
            "而合规审查员会把「全章无引用」判为 🛑 阻断项，于是你就无法批准本章——这不是 bug，"
            "而是系统在阻止你提交一篇没有文献支撑的论文。\n\n"
            "**建议做法**：先到【🛡️ 合规审查】页，把你从中国知网 / Google 学术找到的**真实文献**"
            "按 `[1] 作者. 题名[J]. 刊名, 年, 卷(期): 页码.` 的格式录入参考文献库，再回来生成章节。"
            "（本系统刻意不提供「AI 代写参考文献」功能，因为那正是编造文献的高发区。）"
        )

    # ---- 生成 ----
    st.divider()
    with st.expander("① 生成 / 重新生成本章初稿", expanded=not chapter.get("text")):
        extra = st.text_area("本章附加要求（可选）", key="writer_extra_%d" % chapter["index"], height=80,
                             placeholder="例如：多用 2022 年后的文献；增加一个对比表格；控制在 1500 字内")
        feedback = st.text_area("对上一稿的修改意见（重新生成时生效）",
                                key="writer_feedback_%d" % chapter["index"], height=80,
                                placeholder="例如：第二节论证太薄，补充两个实证案例；删掉口语化表达")
        disabled = locked or chapter["status"] == "approved"
        if st.button(("🖊️ 生成第%d章初稿" % chapter["index"]) if not chapter.get("text")
                     else ("🔁 按修改意见重新生成第%d章" % chapter["index"]),
                     type="primary", disabled=disabled, use_container_width=True,
                     key="writer_gen_%d" % chapter["index"]):
            agent = WriterAgent(SS["llm"])
            st.markdown("**Prompt 已提交**：第%d章《%s》｜目标 %d 字｜大纲 v%d｜参考文献 %d 条"
                        % (chapter["index"], chapter["title"], chapter.get("target_words", 0),
                           int(outline.get("version", 0)),
                           len(parse_reference_entries(ps.get("references", "")))))
            text = stream_to_ui(
                agent.write_chapter(meta, outline["text"], chapter,
                                    chapter.get("target_words", 1200),
                                    references=reference_records_block(ps),
                                    extra=extra, feedback=feedback),
                label="✍️ 撰写员 Agent 正在写作第%d章…" % chapter["index"],
            )
            if text:
                chapter["text"] = text
                chapter["version"] = version + 1
                chapter["status"] = "draft"
                chapter["feedback"] = feedback
                ps["reviews"].pop("chapter_%d" % chapter["index"], None)
                save_artifact(ps["project_id"], "chapter", chapter["index"],
                              chapter["version"], "draft", text, note="AI生成")
                # 若初稿自带参考文献章节，自动并入项目文献库，便于引用交叉核对
                _, local_refs = split_reference_section(text)
                merged_note = ""
                if local_refs.strip():
                    before = len(parse_reference_entries(ps.get("references", "")))
                    ps["references"] = merge_references(ps.get("references", ""), local_refs)
                    after = len(parse_reference_entries(ps["references"]))
                    merged_note = "；已自动合并参考文献（%d → %d 条）" % (before, after)
                audit(actor=WriterAgent.name, actor_role="AGENT", event_type="AI_OUTPUT",
                      stage="chapter", target="chapter_%d" % chapter["index"],
                      prompt="write_chapter | index=%d | extra=%s | feedback=%s"
                             % (chapter["index"], extra, feedback),
                      ai_output=text, status="draft",
                      detail={"version": chapter["version"], "words": count_words(text),
                              "merged_note": merged_note})
                push_history(ps, "撰写员生成第%d章初稿 v%d" % (chapter["index"], chapter["version"]))
                persist(ps)
                st.success("第%d章初稿已生成（v%d）%s" % (chapter["index"], chapter["version"], merged_note))
                _rerun()

    if not chapter.get("text"):
        st.info("本章尚未撰写。")
        return

    # ---- 展示 + 编辑 ----
    st.divider()
    st.markdown("#### ② 审阅初稿（v%d）" % version)
    with st.expander("查看 AI 初稿全文", expanded=True):
        st.markdown(chapter["text"])

    st.markdown("#### ③ 人工修改")
    edited = st.text_area("编辑本章内容", value=chapter["text"], height=360,
                          key="chapter_edit_%d_%d" % (chapter["index"], version))
    c1, c2, c3 = st.columns(3)
    if c1.button("💾 保存我的修改", use_container_width=True, key="save_ch_%d" % chapter["index"]):
        if edited.strip() == chapter["text"].strip():
            st.toast("内容未变化。")
        else:
            diff = unified_diff(chapter["text"], edited, "第%d章 v%d" % (chapter["index"], version))
            chapter["text"] = edited
            chapter["version"] = version + 1
            chapter["status"] = "draft"
            ps["reviews"].pop("chapter_%d" % chapter["index"], None)
            save_artifact(ps["project_id"], "chapter", chapter["index"], chapter["version"],
                          "draft", edited, note="用户修改")
            audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="USER_EDIT",
                  stage="chapter", target="chapter_%d" % chapter["index"],
                  prompt="用户编辑第%d章" % chapter["index"], user_edit=diff, status="draft",
                  detail={"from_version": version, "to_version": chapter["version"],
                          "words": count_words(edited)})
            push_history(ps, "用户修改第%d章 → v%d" % (chapter["index"], chapter["version"]))
            persist(ps)
            st.success("已保存为用户修改版 v%d，请重新运行合规审查。" % chapter["version"])
            _rerun()

    if c2.button("↩️ 驳回本版", use_container_width=True, key="reject_ch_%d" % chapter["index"]):
        chapter["status"] = "draft"
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="REJECT",
              stage="chapter", target="chapter_%d" % chapter["index"],
              user_edit=chapter.get("feedback", ""), status="rejected",
              detail={"version": version})
        persist(ps)
        st.warning("已驳回 v%d，请填写修改意见后重新生成。" % version)
        _rerun()

    if c3.button("🔓 撤回批准", use_container_width=True, disabled=chapter["status"] != "approved",
                 key="revoke_ch_%d" % chapter["index"]):
        chapter["status"] = "draft"
        chapter["approved_at"] = ""
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="REVOKE_APPROVAL",
              stage="chapter", target="chapter_%d" % chapter["index"],
              status="revoked", detail={"version": version})
        push_history(ps, "用户撤回第%d章批准" % chapter["index"])
        persist(ps)
        st.warning("已撤回第%d章的批准，后续章节将重新锁定。" % chapter["index"])
        _rerun()

    # ---- 合规审查 ----
    st.divider()
    st.markdown("#### ④ 合规审查（批准前必须执行）")
    src_text = st.text_area(
        "自查重比对源（可选，每行一条：来源名称 || 文本片段）",
        key="writer_sources_%d" % chapter["index"], height=80,
        placeholder="例：某期刊论文 || 短视频对注意力的影响已得到广泛证实……",
        help="把可获得的比对文本（已发表文献节选、自己其他章节、AI 生成的其他版本）粘贴进来，"
             "系统将检测连续重复（中文≥13字 / 英文≥8词 判定为疑似抄袭并拦截）。",
    )
    use_llm = st.checkbox("同时启用大模型深度审查（额外消耗 token）", value=True,
                          key="writer_use_llm_%d" % chapter["index"])
    if st.button("🛡️ 运行合规审查（本章）", use_container_width=True, key="check_ch_%d" % chapter["index"]):
        sources: List[Tuple[str, str]] = []
        for line in (src_text or "").splitlines():
            if "||" in line:
                name, body = line.split("||", 1)
                sources.append((name.strip() or "比对源", body.strip()))
        run_compliance_check(ps, target_key="chapter_%d" % chapter["index"],
                             text=chapter["text"], kind="chapter",
                             target_name=chapter_display_title(chapter),
                             target_words=chapter.get("target_words", 0),
                             use_llm=use_llm, compare_sources=sources or None)
        _rerun()

    rv = get_review(ps, "chapter_%d" % chapter["index"])
    if rv:
        if not review_is_fresh(ps, "chapter_%d" % chapter["index"], version):
            st.warning("⚠️ 审查结论已过期（本章在审查后被修改），请重新运行合规审查。")
        render_findings(rv.get("findings", []),
                        title="第%d章审查结论（%s）" % (chapter["index"], rv["ts"]))
        if rv.get("summary"):
            st.info("模型总体结论：%s" % rv["summary"])
    else:
        st.caption("本章尚未执行合规审查。")

    # ---- 批准 ----
    st.divider()
    st.markdown("#### ⑤ 人工批准（解锁下一章）")
    blockers = review_blockers(ps, "chapter_%d" % chapter["index"], version)
    confirmed = st.checkbox(
        "我已逐字核对本章内容，确认无抄袭、无编造数据与文献，并自愿承担相应学术责任。",
        key="chapter_confirm_%d_%d" % (chapter["index"], version),
    )
    can_approve = (not blockers) and confirmed and chapter["status"] != "approved"
    if blockers:
        st.error("🛑 存在 %d 项阻断问题，批准按钮已禁用：%s"
                 % (len(blockers), "；".join(f["message"][:40] for f in blockers[:3])))
    if st.button("✅ 批准通过第%d章" % chapter["index"], type="primary", disabled=not can_approve,
                 use_container_width=True, key="approve_ch_%d" % chapter["index"]):
        chapter["status"] = "approved"
        chapter["approved_at"] = now_ts()
        save_artifact(ps["project_id"], "chapter", chapter["index"], version, "approved",
                      chapter["text"], note="用户批准")
        nxt = next((c for c in chapters if c["index"] == chapter["index"] + 1), None)
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN", event_type="APPROVE",
              stage="chapter", target="chapter_%d" % chapter["index"],
              user_edit="学术诚信核对声明：已勾选", status="approved",
              detail={"version": version, "words": count_words(chapter["text"]),
                      "next_chapter": nxt["title"] if nxt else None})
        push_history(ps, "用户批准第%d章" % chapter["index"])
        persist(ps)
        if nxt:
            st.success("第%d章已批准✅ 现在可以撰写第%d章《%s》。" % (chapter["index"], nxt["index"], nxt["title"]))
        else:
            st.success("🎉 全部章节已批准！请前往【📤 导出】生成定稿。")
        _rerun()


def merge_references(old: str, new: str) -> str:
    """把新解析出的参考文献条目并入项目文献库（按内容去重，重新连续编号）。"""
    entries_old = parse_reference_entries(old)
    entries_new = parse_reference_entries(new)
    seen = set()
    merged: List[str] = []
    for e in entries_old + entries_new:
        key = re.sub(r"\s+", "", e["text"]).lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        merged.append(e["text"])
    if not merged:
        return old
    lines = ["## 参考文献"]
    for i, txt in enumerate(merged, start=1):
        lines.append("[%d] %s" % (i, txt))
    return "\n".join(lines) + "\n"


# ------------------------------ 合规审查页 ------------------------------

def render_compliance_tab(ps: Dict[str, Any]) -> None:
    st.markdown("### 🛡️ 合规审查员 Agent · 全局合规中心")
    st.caption("本页提供：① 真实文献库检索与维护 ② 全篇格式与结构检查 ③ 全局自查重")

    render_literature_panel(ps, key_prefix="complit")

    st.divider()
    st.markdown("#### ② 引用编号规范化（GB/T 7714 顺序编码制）")
    st.caption("顺序编码制要求参考文献按**正文首次出现**的顺序编号。一键生成会自动重排；"
               "逐步模式下如果你改过正文引用顺序，可以在这里手动重排（会同步改写所有章节的 [n] 标注）。")
    if st.button("🔢 按正文首次出现顺序重新编号", use_container_width=True):
        n_total, n_changed = renumber_references_by_appearance(ps)
        if not n_total:
            st.warning("文献库为空，无法重排。")
        elif n_changed == 0:
            st.success("编号已符合首次出现顺序，无需调整（共引用 %d 条）。" % n_total)
        else:
            audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_RENUMBER",
                  stage="references", target="references",
                  prompt="用户手动按首次出现顺序重排", status="success",
                  detail={"changed": n_changed, "cited": n_total})
            persist(ps)
            st.success("已重排 %d 处编号，相关章节的审查结论已作废，请重新运行合规审查。" % n_changed)
            _rerun()

    st.divider()
    st.markdown("#### ③ 全篇格式与结构检查")
    if st.button("🔍 运行全篇合规检查（格式 / 结构 / 引用）", type="primary", use_container_width=True):
        doc = build_full_document(ps)
        if not doc.strip():
            st.warning("暂无可检查的内容，请先生成大纲或章节。")
        else:
            run_compliance_check(ps, target_key="document", text=doc, kind="document",
                                 target_name="全篇定稿", use_llm=True)
            _rerun()
    rv = get_review(ps, "document")
    if rv:
        render_findings(rv.get("findings", []), title="全篇检查结论（%s）" % rv["ts"])

    st.divider()
    st.markdown("#### ④ 全局自查重（跨章节 / 自定义比对源）")
    sources_text = st.text_area(
        "比对源（每行一条：来源名称 || 文本片段）", height=120, key="global_sources",
        placeholder="已发表论文节选 || ………\n自己以前写过的段落 || ………",
    )
    if st.button("🧬 运行自查重扫描", use_container_width=True):
        sources: List[Tuple[str, str]] = []
        for line in (sources_text or "").splitlines():
            if "||" in line:
                name, body = line.split("||", 1)
                sources.append((name.strip() or "比对源", body.strip()))
        # a) 与用户提供的比对源比对（全文作为一个整体）
        findings = RuleEngine.check_similarity(build_full_document(ps), sources)
        # b) 章节之间的自我重复（逐章与更早章节比对）
        chs = [c for c in ps["chapters"] if c.get("text")]
        for c in chs:
            findings += RuleEngine.check_self_repeat(ps["chapters"], c["index"])
        if not sources and len(chs) < 2:
            st.info("未提供比对源，且已撰写章节不足 2 章，无法进行重复度比对。")
        render_findings(findings, title="自查重结论")
        audit(actor=ComplianceAgent.name, actor_role="AGENT", event_type="PLAGIARISM_SCAN",
              stage="document", target="document",
              prompt="比对源 %d 个" % len(sources),
              status="blocked" if any(f["level"] == LEVEL_BLOCKER for f in findings) else "passed",
              detail={"findings": findings})
        persist(ps)


# ------------------------------ 真实文献库面板 ------------------------------

LIT_SOURCES = {
    "OpenAlex 全球": "openalex",
    "OpenAlex 中文": "openalex_zh",
    "Crossref": "crossref",
}


def render_literature_panel(ps: Dict[str, Any], key_prefix: str = "lit") -> None:
    """
    真实文献库面板：检索（免费开放数据库）+ 手工导入（知网/万方引用）+ 库管理。
    所有文献都来自真实数据源并带 DOI，系统不用大模型编造任何文献。
    """
    st.markdown("#### 📚 真实文献库")
    st.caption(
        "文献只从**免费开放**的学术数据库检索：OpenAlex（全球库 / 中文库）与 Crossref，"
        "全部无需注册和 API Key，返回真实 DOI，可逐条点开核验。"
        "⚠️ 知网 / 万方 / 维普没有免费开放接口，请用下面「手工导入」把你复制到的引用粘贴进来。"
    )

    if lit is None:
        st.error("literature.py 未加载成功，暂时只能手工编辑参考文献。")
        return

    meta = ps["meta"]
    default_kw = meta.get("keywords") or meta.get("title") or ""
    c1, c2, c3 = st.columns([3, 1, 1])
    kw = c1.text_input("检索关键词（建议中文、英文各检索一次）", value=default_kw,
                       key="%s_kw_%s" % (key_prefix, ps["project_id"]))
    year_from = c2.number_input("起始年份（0=不限）", min_value=0, max_value=2030,
                                value=int(meta.get("year_from") or 0), step=1,
                                key="%s_year_%s" % (key_prefix, ps["project_id"]))
    limit = c3.number_input("检索条数", min_value=3, max_value=30, value=10, step=1,
                            key="%s_limit_%s" % (key_prefix, ps["project_id"]))
    picked_sources = st.multiselect(
        "数据源", list(LIT_SOURCES.keys()),
        default=["OpenAlex 全球", "OpenAlex 中文", "Crossref"],
        key="%s_src_%s" % (key_prefix, ps["project_id"]),
    )

    res_key = "lit_results_%s" % ps["project_id"]
    if st.button("🔍 检索真实文献", key="%s_search_%s" % (key_prefix, ps["project_id"]),
                 use_container_width=True):
        srcs = [LIT_SOURCES[s] for s in picked_sources]
        if not srcs:
            st.warning("请至少选择一个数据源。")
        elif not kw.strip():
            st.warning("请填写检索关键词。")
        else:
            with st.spinner("正在检索 OpenAlex / Crossref …"):
                try:
                    found = lit.search_literature(kw, limit=int(limit),
                                                  year_from=int(year_from) or None,
                                                  sources=srcs)
                    st.session_state[res_key] = found
                    audit(actor="系统", actor_role="SYSTEM", event_type="LITERATURE_SEARCH",
                          stage="references", target="references",
                          prompt="关键词：%s｜年份≥%s｜数据源：%s"
                                 % (kw, year_from or "不限", "、".join(picked_sources)),
                          ai_output="命中 %d 条真实文献" % len(found), status="success",
                          detail={"count": len(found),
                                  "titles": [r["title"] for r in found[:10]]})
                    persist(ps)
                except lit.LiteratureError as exc:
                    st.error("检索失败：%s" % exc)
                except Exception as exc:  # 网络异常等
                    st.error("检索出错：%s" % exc)

    results = st.session_state.get(res_key) or []
    if results:
        import pandas as pd
        st.markdown("**检索结果**（勾选后加入文献库）")
        rows = []
        for i, r in enumerate(results):
            rows.append({
                "选": False,
                "作者": short(lit.format_authors_gbt(r.get("authors") or [], 2), 24),
                "年份": r.get("year") or "",
                "标题": short(r.get("title") or "", 60),
                "来源": short(r.get("venue") or "", 30),
                "DOI": r.get("doi") or "（无，谨慎使用）",
                "引用数": r.get("cited_by") or 0,
                "数据源": r.get("source") or "",
            })
        edited = st.data_editor(pd.DataFrame(rows), hide_index=True,
                                use_container_width=True,
                                key="%s_table_%s" % (key_prefix, ps["project_id"]))
        cc1, cc2 = st.columns([1, 1])
        if cc1.button("➕ 把勾选的加入文献库", type="primary", use_container_width=True,
                      key="%s_add_%s" % (key_prefix, ps["project_id"])):
            chosen = [results[i] for i in range(len(results)) if bool(edited.iloc[i]["选"])]
            if not chosen:
                st.warning("还没有勾选任何文献。")
            else:
                merged = get_ref_records(ps) + chosen
                set_ref_records(ps, merged)
                audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_ADD",
                      stage="references", target="references",
                      prompt="从检索结果加入 %d 条" % len(chosen),
                      user_edit=lit.build_reference_list(chosen, meta.get("citation_style")),
                      status="success", detail={"total": len(merged)})
                persist(ps)
                st.success("已加入 %d 条，文献库共 %d 条。" % (len(chosen), len(merged)))
                st.session_state.pop(res_key, None)
                _rerun()
        if cc2.button("🤖 自动挑选相关性最高的 N 条加入", use_container_width=True,
                      key="%s_auto_%s" % (key_prefix, ps["project_id"])):
            n = int(limit)
            chosen = results[:n]
            merged = get_ref_records(ps) + chosen
            set_ref_records(ps, merged)
            audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_ADD",
                  stage="references", target="references",
                  prompt="自动挑选前 %d 条" % len(chosen),
                  user_edit=lit.build_reference_list(chosen, meta.get("citation_style")),
                  status="success", detail={"total": len(merged)})
            persist(ps)
            st.success("已加入 %d 条，文献库共 %d 条。" % (len(chosen), len(merged)))
            st.session_state.pop(res_key, None)
            _rerun()

    with st.expander("📋 手工导入：粘贴知网 / 万方 / 谷歌学术的「引用」文本", expanded=False):
        st.caption("在知网文献页点「引用」→ 复制 GB/T 7714 那一条 → 粘贴到这里，一行一条。"
                   "国内文献走这条路最稳（系统负责解析、编号、格式校验）。")
        paste = st.text_area("粘贴内容", height=140, key="%s_paste_%s" % (key_prefix, ps["project_id"]),
                             placeholder="[1] 张三. 大学生媒介使用行为研究[J]. 心理科学进展, 2023, 31(4): 612-625.")
        if st.button("解析并加入文献库", key="%s_paste_btn_%s" % (key_prefix, ps["project_id"]),
                     use_container_width=True):
            parsed = lit.parse_reference_text(paste)
            if not parsed:
                st.warning("没能解析出文献条目。请确认格式形如：`[1] 作者. 题名[J]. 刊名, 年, 卷(期): 页码.`")
            else:
                merged = get_ref_records(ps) + parsed
                set_ref_records(ps, merged)
                audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_IMPORT",
                      stage="references", target="references", prompt=paste,
                      user_edit=lit.build_reference_list(parsed, meta.get("citation_style")),
                      status="success", detail={"parsed": len(parsed), "total": len(merged)})
                persist(ps)
                st.success("成功解析 %d 条，文献库共 %d 条。请核对著录信息。" % (len(parsed), len(merged)))
                _rerun()

    records = get_ref_records(ps)
    st.markdown("**当前文献库：%d 条**" % len(records))
    if records:
        import pandas as pd
        untraceable = [r for r in records if not lit.has_provenance(r)]
        if untraceable:
            st.warning("有 %d 条文献**没有 DOI 或原文链接**，无法溯源。"
                       "建议在知网/谷歌学术确认后再用，合规审查会就此给出预警。" % len(untraceable))
        st.dataframe(pd.DataFrame([{
            "序号": i + 1,
            "作者": short(lit.format_authors_gbt(r.get("authors") or [], 2), 22),
            "年份": r.get("year") or "",
            "标题": short(r.get("title") or "", 56),
            "来源": short(r.get("venue") or "", 26),
            "可溯源": "✅" if lit.has_provenance(r) else "⚠️ 无DOI",
        } for i, r in enumerate(records)]), hide_index=True, use_container_width=True)
        cc1, cc2 = st.columns([1, 1])
        if cc1.button("🧹 去重并重新编号", use_container_width=True,
                      key="%s_dedup_%s" % (key_prefix, ps["project_id"])):
            seen, uniq = set(), []
            for r in records:
                k = (r.get("doi") or "").lower() or (r.get("title") or "")[:60]
                if k and k not in seen:
                    seen.add(k)
                    uniq.append(r)
            set_ref_records(ps, uniq)
            audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_DEDUP",
                  stage="references", target="references", status="success",
                  detail={"before": len(records), "after": len(uniq)})
            persist(ps)
            st.success("去重完成：%d → %d 条。" % (len(records), len(uniq)))
            _rerun()
        if cc2.button("🗑️ 清空文献库", use_container_width=True,
                      key="%s_clear_%s" % (key_prefix, ps["project_id"])):
            set_ref_records(ps, [])
            audit(actor="用户", actor_role="HUMAN", event_type="REFERENCE_CLEAR",
                  stage="references", target="references", status="success")
            persist(ps)
            _rerun()

    with st.expander("✏️ 手工编辑参考文献 Markdown（高级）", expanded=False):
        st.caption("直接改文本也可以：保存后系统会重新解析回结构化文献库。")
        refs_text = st.text_area("参考文献 Markdown", value=ps.get("references", ""),
                                 height=200, key="%s_text_%s" % (key_prefix, ps["project_id"]))
        if st.button("💾 保存文本并重新解析", key="%s_save_%s" % (key_prefix, ps["project_id"])):
            diff = unified_diff(ps.get("references", ""), refs_text, "参考文献库")
            ps["references"] = refs_text
            ps["ref_records"] = lit.parse_reference_text(refs_text)
            audit(actor=ps["meta"].get("student") or "用户", actor_role="HUMAN",
                  event_type="USER_EDIT", stage="references", target="references",
                  prompt="用户编辑参考文献 Markdown", user_edit=diff, status="success",
                  detail={"parsed": len(ps["ref_records"])})
            persist(ps)
            st.success("已保存，解析出 %d 条。" % len(ps["ref_records"]))
            _rerun()


# ------------------------------ 一键生成（快速模式） ------------------------------

def auto_search_references(ps: Dict[str, Any], want: int, keyword: str = "") -> int:
    """一键模式第一步：自动检索真实文献并入库，返回入库条数。"""
    if lit is None:
        return 0
    meta = ps["meta"]
    queries = []
    base = (keyword or meta.get("keywords") or "").strip()
    if base:
        queries.append(base)
    title = (meta.get("title") or "").strip()
    if title and title not in queries:
        queries.append(title)
    queries.append(meta.get("major") or "")
    queries = [q for q in queries if q]

    collected: List[Dict[str, Any]] = []
    for q in queries[:2]:
        for srcs in (("openalex_zh", "crossref"), ("openalex",)):
            try:
                collected += lit.search_literature(q, limit=want, year_from=meta.get("year_from") or None,
                                                   sources=srcs)
            except lit.LiteratureError:
                continue
        if len(collected) >= want:
            break

    # 去重 + 只保留可溯源（有 DOI/URL）的文献，宁缺毋滥
    seen, uniq = set(), []
    for r in collected:
        k = (r.get("doi") or "").lower() or (r.get("title") or "")[:60]
        if not k or k in seen:
            continue
        if not lit.has_provenance(r):
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: (-(r.get("year") or 0), -(r.get("cited_by") or 0)))

    if uniq:
        set_ref_records(ps, uniq[:want])
    audit(actor="系统", actor_role="SYSTEM", event_type="AUTO_LITERATURE",
          stage="references", target="references",
          prompt="一键模式自动检索：%s" % "；".join(queries[:2]),
          ai_output=lit.build_reference_list(uniq[:want], meta.get("citation_style")) if uniq else "",
          status="success" if uniq else "empty",
          detail={"count": len(uniq[:want]), "mode": "auto"})
    return len(uniq[:want])


def renumber_references_by_appearance(ps: Dict[str, Any]) -> Tuple[int, int]:
    """
    按正文首次出现顺序重排参考文献编号（GB/T 7714 顺序编码制的要求）。
    会同步改写各章正文里的 [n] 标注、重排文献库，并使相关审查结论作废（必须重审）。
    返回 (被引用的文献数, 编号发生变化的条数)。
    """
    records = get_ref_records(ps)
    if not records:
        return 0, 0

    order: List[int] = []
    for ch in ps["chapters"]:
        for n in extract_intext_citations(ch.get("text") or ""):
            if n not in order and 1 <= n <= len(records):
                order.append(n)
    for i in range(1, len(records) + 1):     # 未被引用的排在最后
        if i not in order:
            order.append(i)

    mapping = {old: new for new, old in enumerate(order, start=1)}
    changed = sum(1 for k, v in mapping.items() if k != v)
    if changed == 0:
        return len(order), 0

    def repl(m: "re.Match") -> str:
        nums = [int(x) for x in re.split(r"[,，\-–]", m.group(1)) if x.strip().isdigit()]
        if not nums:
            return m.group(0)
        new = sorted(mapping.get(n, n) for n in nums)
        return "[%d]" % new[0] if len(new) == 1 else "[%s]" % ",".join(str(x) for x in new)

    for ch in ps["chapters"]:
        if ch.get("text"):
            new_text = CITE_RE.sub(repl, ch["text"])
            if new_text != ch["text"]:
                ch["text"] = new_text
                ch["version"] = int(ch.get("version", 0)) + 1
                ps["reviews"].pop("chapter_%d" % ch["index"], None)

    set_ref_records(ps, [records[i - 1] for i in order])
    return len(order), changed


def run_auto_pipeline(ps: Dict[str, Any], *, want_refs: int, use_review: bool) -> None:
    """
    一键生成流水线：文献检索 → 大纲 → 逐章初稿 → 按首次出现顺序重排引用编号
    →（可选）逐章自动合规审查 → 收尾。
    全程写入审计日志（event_type 带 AUTO_ 前缀，mode=auto），便于事后追溯。
    """
    meta = ps["meta"]
    llm = SS["llm"]
    n_ch = max(1, int(meta.get("chapter_count") or 5))
    total_steps = 2 + n_ch + (n_ch if use_review else 0)
    done = 0
    progress = st.progress(0.0, text="准备中…")

    def step(label: str) -> None:
        nonlocal done
        done += 1
        progress.progress(min(1.0, done / max(1, total_steps)),
                          text="[%d/%d] %s" % (done, total_steps, label))

    ps["mode"] = "auto"

    # ---- 1) 真实文献 ----
    step("检索真实文献（OpenAlex / Crossref）")
    with st.expander("① 文献检索结果", expanded=False):
        st.write("正在检索真实文献…")
        n_refs = auto_search_references(ps, want_refs)
        if n_refs:
            st.success("已入库 %d 条真实文献（均带 DOI 可核验）。" % n_refs)
            st.markdown(ps.get("references", ""))
        else:
            st.warning("没有检索到可溯源的文献。后续正文将不含引用标注，"
                       "合规审查会因此给出阻断提示——这是系统的防造假设计，不是故障。")
    persist(ps)

    # ---- 2) 大纲 ----
    step("规划师生成大纲")
    with st.expander("② 论文大纲", expanded=True):
        planner = PlannerAgent(llm)
        outline_text = stream_to_ui(
            planner.generate_outline(meta, extra="请严格使用我提供的参考文献编号，不要编造文献。",
                                     feedback=""),
            label="🧭 规划师 Agent 正在生成大纲…")
        if not outline_text:
            st.error("大纲生成失败，一键流程已中止。")
            return
        ps["outline"]["text"] = outline_text
        ps["outline"]["version"] = int(ps["outline"].get("version", 0)) + 1
        ps["outline"]["status"] = "approved"      # 一键模式自动通过（记入审计）
        ps["outline"]["approved_at"] = now_ts()
        ps["outline"]["approved_by"] = "一键模式自动通过"
        save_artifact(ps["project_id"], "outline", 0, ps["outline"]["version"],
                      "approved", outline_text, note="AUTO 一键生成")
        audit(actor=PlannerAgent.name, actor_role="AGENT", event_type="AUTO_OUTLINE",
              stage="outline", target="outline",
              prompt="generate_outline（一键模式，自动批准）", ai_output=outline_text,
              status="approved", detail={"version": ps["outline"]["version"], "mode": "auto"})
        st.markdown(outline_text)

    chapters = parse_outline(ps["outline"]["text"], meta.get("chapter_count", 5))
    targets = allocate_word_targets(int(meta.get("target_words", 12000)), chapters)
    ps["chapters"] = [{
        "index": c["index"], "title": c["title"], "sections": c["sections"],
        "target_words": targets[i] if i < len(targets) else 1200,
        "text": "", "version": 0, "status": "pending",
        "approved_at": "", "feedback": "",
    } for i, c in enumerate(chapters)]
    persist(ps)

    # ---- 3) 逐章撰写（先全部写完，再统一重排编号，最后统一审查）----
    writer = WriterAgent(llm)
    ref_block = reference_records_block(ps)
    for ch in ps["chapters"]:
        step("撰写第%d章 %s" % (ch["index"], ch["title"]))
        with st.expander("③ 第%d章 %s" % (ch["index"], ch["title"]), expanded=True):
            text = stream_to_ui(
                writer.write_chapter(meta, ps["outline"]["text"], ch,
                                     ch.get("target_words", 1200),
                                     references=ref_block, extra="", feedback=""),
                label="✍️ 正在撰写第%d章…" % ch["index"])
            if not text:
                st.warning("第%d章生成失败，已跳过（可稍后在撰写页单独重试）。" % ch["index"])
                continue
            ch["text"] = text
            ch["version"] = 1
            ch["status"] = "draft"
            save_artifact(ps["project_id"], "chapter", ch["index"], 1, "draft", text,
                          note="AUTO 一键生成")
            audit(actor=WriterAgent.name, actor_role="AGENT", event_type="AUTO_CHAPTER",
                  stage="chapter", target="chapter_%d" % ch["index"],
                  prompt="write_chapter（一键模式）｜目标 %d 字" % ch.get("target_words", 0),
                  ai_output=text, status="draft",
                  detail={"version": 1, "words": count_words(text), "mode": "auto"})
            persist(ps)

    # ---- 4) 按正文首次出现顺序重排引用编号（GB/T 7714 顺序编码制）----
    step("按正文首次出现顺序重排引用编号")
    n_total, n_changed = renumber_references_by_appearance(ps)
    if n_changed:
        st.info("已按 GB/T 7714 顺序编码制要求重排引用编号：%d 处编号发生变化"
                "（正文标注与文献表已同步，未引用的文献排在最后）。" % n_changed)
        audit(actor="系统", actor_role="SYSTEM", event_type="REFERENCE_RENUMBER",
              stage="references", target="references",
              prompt="一键模式：按正文首次出现顺序重排", status="success",
              detail={"changed": n_changed, "cited": n_total, "mode": "auto"})
        persist(ps)

    # ---- 5) 逐章自动合规审查 ----
    if use_review:
        agent_c = ComplianceAgent(llm)
        for ch in ps["chapters"]:
            if not ch.get("text"):
                continue
            step("合规审查第%d章" % ch["index"])
            with st.expander("④ 第%d章合规审查" % ch["index"], expanded=False):
                rule_findings = RuleEngine.run_all(
                    text=ch["text"], kind="chapter",
                    style=meta.get("citation_style", CITATION_STYLES[0]),
                    target_name=chapter_display_title(ch), target_words=ch.get("target_words", 0),
                    project_references=ps.get("references", ""),
                    chapters=ps["chapters"], outline_chapters=ps["chapters"], chapter=ch)
                llm_findings: List[Dict[str, Any]] = []
                summary = ""
                review_text = ""
                try:
                    review_text = "".join(agent_c.llm_review(
                        text=ch["text"], kind="chapter",
                        style=meta.get("citation_style", CITATION_STYLES[0]),
                        outline_text=ps["outline"].get("text", ""),
                        target_name=chapter_display_title(ch),
                        references=ps.get("references", "")))
                    llm_findings, summary = ComplianceAgent.parse_llm_findings(review_text)
                except LLMError as exc:
                    st.warning("第%d章自动审查调用失败：%s" % (ch["index"], exc))
                findings = sorted(rule_findings + llm_findings,
                                  key=lambda x: (LEVEL_ORDER.get(x["level"], 9), x["category"]))
                ps["reviews"]["chapter_%d" % ch["index"]] = {
                    "version": int(ch.get("version", 1)), "ts": now_ts(), "kind": "chapter",
                    "target": chapter_display_title(ch), "findings": findings,
                    "llm_text": review_text, "summary": summary,
                    "blockers": sum(1 for f in findings if f["level"] == LEVEL_BLOCKER),
                    "warnings": sum(1 for f in findings if f["level"] == LEVEL_WARNING),
                }
                audit(actor=ComplianceAgent.name, actor_role="AGENT",
                      event_type="AUTO_COMPLIANCE_CHECK", stage="chapter",
                      target="chapter_%d" % ch["index"],
                      prompt="一键模式自动审查", ai_output=review_text,
                      status="blocked" if ps["reviews"]["chapter_%d" % ch["index"]]["blockers"] else "passed",
                      detail={"findings": findings, "mode": "auto"})
                render_findings(findings, title="第%d章自动审查结论" % ch["index"])
                persist(ps)

    step("汇编与收尾")
    push_history(ps, "一键生成完成：%d 章 / %d 条文献"
                 % (sum(1 for c in ps["chapters"] if c.get("text")),
                    len(get_ref_records(ps))))
    persist(ps)
    progress.progress(1.0, text="完成")
    st.success("🎉 一键生成完成！请到【✍️ 撰写员 · 章节】逐章核对修改，"
               "确认无误后在【📤 导出】生成定稿。")


def render_auto_tab(ps: Dict[str, Any]) -> None:
    st.markdown("### ⚡ 一键生成（快速模式）")
    st.warning(
        "该模式会**连续跑完**：检索真实文献 → 生成大纲 → 逐章撰写 →（可选）逐章合规审查。\n\n"
        "它不会再逐环节停下等你批准——**这是「快速出初稿」通道，不是「一键交论文」**。"
        "生成结束后，每一章仍是**待你逐字核对**的初稿，导出定稿前请在撰写页批准，"
        "全部操作都会写入审计日志（标记为 auto 模式）。"
    )

    if ps["outline"].get("text") or any(c.get("text") for c in ps["chapters"]):
        st.info("当前项目已有内容。一键生成会**覆盖**大纲与全部章节草稿"
                "（旧稿仍保留在稿件版本历史里，可在【导出】页找回）。")

    meta = ps["meta"]
    c1, c2, c3 = st.columns(3)
    want_refs = c1.slider("自动入库文献数", 5, 25, 12, key="auto_refs")
    c2.metric("章节数", meta.get("chapter_count"))
    c3.metric("目标总字数", meta.get("target_words"))
    use_review = st.checkbox("每章生成后自动做一次合规审查（推荐，会多消耗一些 token）",
                             value=True, key="auto_review")
    kw = st.text_input("文献检索关键词（留空则用论文题目）",
                       value=meta.get("keywords") or "", key="auto_kw")
    ok = st.checkbox(
        "我已了解：一键生成产出的是**初稿**，我必须逐字核对文献引用与数据表述后，"
        "才能作为本人论文提交，并自愿承担相应学术责任。", key="auto_confirm")

    if st.button("⚡ 开始一键生成", type="primary", disabled=not ok, use_container_width=True):
        if kw.strip():
            ps["meta"]["keywords"] = kw.strip()
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN",
              event_type="AUTO_START", stage="system", target="document",
              prompt="一键生成｜文献 %d 条｜自动审查 %s" % (want_refs, use_review),
              user_edit="已勾选初稿核对声明", status="started",
              detail={"mode": "auto"})
        persist(ps)
        run_auto_pipeline(ps, want_refs=int(want_refs), use_review=use_review)
        _rerun()

    st.divider()
    st.markdown("#### 生成后的快速收尾")
    st.caption("如果你已经通读过全文，可以一次性批准所有章节；系统会记录「用户声明已通读」。")
    st.markdown("**当前待批准章节**：%d / %d"
                % (sum(1 for c in ps["chapters"] if c.get("status") != "approved"),
                   len(ps["chapters"])))
    ok2 = st.checkbox("我已通读全部章节，确认无抄袭、无编造数据与文献", key="auto_batch_confirm")
    if st.button("✅ 一键批准全部章节", disabled=not ok2 or not ps["chapters"],
                 use_container_width=True):
        n = 0
        for ch in ps["chapters"]:
            if ch.get("text") and ch.get("status") != "approved":
                ch["status"] = "approved"
                ch["approved_at"] = now_ts()
                save_artifact(ps["project_id"], "chapter", ch["index"],
                              int(ch.get("version", 1)), "approved", ch["text"],
                              note="用户批量批准")
                n += 1
        audit(actor=meta.get("student") or "用户", actor_role="HUMAN",
              event_type="APPROVE", stage="chapter", target="all_chapters",
              user_edit="批量批准声明已勾选：我已通读全部章节", status="approved",
              detail={"approved": n, "mode": "auto"})
        push_history(ps, "用户批量批准 %d 章" % n)
        persist(ps)
        st.success("已批准 %d 章，现在可以到【📤 导出】生成定稿。" % n)
        _rerun()

    st.divider()
    st.markdown("#### 📚 文献库（两种模式共用）")
    render_literature_panel(ps, key_prefix="autolit")


# ------------------------------ 审计日志页 ------------------------------

def render_audit_tab(ps: Dict[str, Any]) -> None:
    st.markdown("### 🧾 审计日志（Audit Logs）")
    st.caption("由日志记录员 Agent 自动写入 SQLite。每条记录含时间戳、操作角色、输入 Prompt、"
               "AI 输出、用户修改（diff）、最终状态，并以哈希链防篡改。")

    rows = fetch_logs(ps["project_id"], limit=5000)
    c1, c2, c3 = st.columns([1, 1, 2])
    actors = sorted({r["actor"] for r in rows})
    types = sorted({r["event_type"] for r in rows})
    sel_actor = c1.multiselect("操作角色", actors, default=actors)
    sel_type = c2.multiselect("事件类型", types, default=types)
    keyword = c3.text_input("关键字检索（Prompt / 输出 / 修改内容）", "")

    def hit(r: sqlite3.Row) -> bool:
        if r["actor"] not in sel_actor or r["event_type"] not in sel_type:
            return False
        if keyword:
            hay = " ".join([r["prompt"] or "", r["ai_output"] or "", r["user_edit"] or "",
                            r["detail"] or "", r["target"] or ""])
            if keyword.lower() not in hay.lower():
                return False
        return True

    filtered = [r for r in rows if hit(r)]
    st.markdown("共 **%d** 条记录（当前筛选 %d 条）" % (len(rows), len(filtered)))

    view = st.radio("展示形式", ["📊 表格视图", "🕒 时间线视图", "🔐 完整性校验与导出"],
                    horizontal=True, key="audit_view")

    if view == "📊 表格视图":
        table = [{
            "ID": r["id"], "时间戳": r["ts"], "角色": r["actor"], "角色类型": r["actor_role"],
            "事件": r["event_type"], "阶段": r["stage"], "对象": r["target"],
            "状态": r["status"], "输入Prompt": short(r["prompt"], 60),
            "AI输出": short(r["ai_output"], 60), "用户修改": short(r["user_edit"], 60),
        } for r in filtered]
        st.dataframe(table, use_container_width=True, hide_index=True)
        with st.expander("查看某条完整记录（含完整 Prompt / 输出 / diff）"):
            if filtered:
                label_map = {"#%s · %s · %s" % (r["id"], r["event_type"], r["ts"][11:19]): r
                             for r in filtered}
                pick = st.selectbox("选择日志 ID", list(label_map.keys()),
                                    key="audit_log_pick")
                row = label_map[pick]
                st.markdown("**时间戳**：%s　**角色**：%s（%s）　**事件**：%s　**状态**：%s"
                            % (row["ts"], row["actor"], row["actor_role"], row["event_type"], row["status"]))
                st.markdown("**输入 Prompt**")
                st.code(row["prompt"] or "（无）", language="text")
                st.markdown("**AI 输出**")
                st.code(row["ai_output"] or "（无）", language="markdown")
                st.markdown("**用户修改（diff）**")
                st.code(row["user_edit"] or "（无）", language="diff")
                st.markdown("**扩展信息**")
                st.code(row["detail"] or "{}", language="json")

    elif view == "🕒 时间线视图":
        for r in filtered:
            icon = {"APPROVE": "✅", "REJECT": "↩️", "USER_EDIT": "✏️", "AI_OUTPUT": "🤖",
                    "COMPLIANCE_CHECK": "🛡️", "PROJECT_CREATE": "🆕"}.get(r["event_type"], "•")
            head = "%s `%s` **%s** · %s · %s" % (icon, r["ts"], r["actor"], r["event_type"], r["status"])
            with st.expander(head, expanded=False):
                st.markdown("**对象**：%s　**阶段**：%s　**角色类型**：%s"
                            % (r["target"] or "—", r["stage"] or "—", r["actor_role"]))
                if r["prompt"]:
                    st.markdown("**输入 Prompt**"); st.code(r["prompt"], language="text")
                if r["ai_output"]:
                    st.markdown("**AI 输出**"); st.code(r["ai_output"], language="markdown")
                if r["user_edit"]:
                    st.markdown("**用户修改**"); st.code(r["user_edit"], language="diff")
                if r["detail"] and r["detail"] != "{}":
                    st.markdown("**扩展信息**"); st.code(r["detail"], language="json")
                st.caption("哈希：%s… ｜ 前序哈希：%s…" % (r["record_hash"][:16], (r["prev_hash"] or "")[:16]))

    else:
        ok, broken = verify_hash_chain(ps["project_id"])
        if ok:
            st.success("🔐 哈希链校验通过：%d 条记录未被篡改。" % len(rows))
        else:
            st.error("🛑 哈希链校验失败！异常记录 ID：%s（日志可能被直接修改过）"
                     % ", ".join(str(b) for b in broken))
        c1, c2 = st.columns(2)
        c1.download_button("⬇️ 导出全部审计日志（JSON）", logs_to_json_bytes(rows),
                           file_name="audit_logs_%s.json" % ps["project_id"],
                           mime="application/json", use_container_width=True)
        c2.download_button("⬇️ 导出全部审计日志（CSV）", logs_to_csv_bytes(rows),
                           file_name="audit_logs_%s.csv" % ps["project_id"],
                           mime="text/csv", use_container_width=True)
        st.caption("导出行为本身同样会被记录进审计日志。")
        if st.button("📝 记录本次导出操作"):
            audit(actor=ps["meta"].get("student") or "用户", actor_role="HUMAN",
                  event_type="EXPORT_AUDIT_LOG", stage="system", target="audit_logs",
                  status="success", detail={"count": len(rows)})
            persist(ps)
            st.toast("已记录。")


# ------------------------------ 导出页 ------------------------------

def build_full_document(ps: Dict[str, Any]) -> str:
    """拼接定稿 Markdown（仅包含已批准的章节 + 大纲附注）。"""
    meta = ps["meta"]
    parts = ["# %s\n" % meta.get("title", "未命名论文"),
             "**专业方向**：%s　**参考文献格式**：%s\n" % (meta.get("major"), meta.get("citation_style"))]
    for c in ps["chapters"]:
        if c.get("text"):
            mark = "" if c["status"] == "approved" else "> ⚠️ 本章尚未批准，仅为初稿\n"
            parts.append(mark + c["text"].strip() + "\n")
    if ps.get("references", "").strip():
        parts.append(ps["references"].strip() + "\n")
    return "\n".join(parts)


def render_export_tab(ps: Dict[str, Any]) -> None:
    st.markdown("### 📤 导出与定稿")
    wf = compute_workflow(ps)
    if not wf["all_approved"]:
        st.warning("⚠️ 尚有 %d 个章节未获批准，当前导出属**初稿**，请勿直接提交学校。"
                   % (wf["chapter_count"] - wf["approved_count"]))
    else:
        st.success("✅ 全部章节已批准，导出的定稿已通过本地合规规则校验。"
                   "请仍须自行送检学校查重系统并由导师审阅。")

    doc = build_full_document(ps)
    st.caption("定稿字数约 %d 字（目标 %s 字）" % (count_words(doc), ps["meta"].get("target_words")))

    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇️ 论文全文（Markdown）", doc.encode("utf-8"),
                       file_name="%s.md" % ps["meta"].get("title", "thesis")[:30],
                       mime="text/markdown", use_container_width=True)
    c2.download_button("⬇️ 项目进度快照（JSON）",
                       json.dumps(ps, ensure_ascii=False, indent=2).encode("utf-8"),
                       file_name="project_%s.json" % ps["project_id"],
                       mime="application/json", use_container_width=True)
    c3.download_button("⬇️ 稿件版本历史（CSV）",
                       "\n".join(["id,ts,kind,chapter_index,version,status,note,words"] + [
                           "%d,%s,%s,%d,%d,%s,%s,%d" % (
                               r["id"], r["ts"], r["kind"], r["chapter_index"], r["version"],
                               r["status"], r["note"], count_words(r["content"] or ""))
                           for r in list_artifacts(ps["project_id"])
                       ]).encode("utf-8-sig"),
                       file_name="artifacts_%s.csv" % ps["project_id"],
                       mime="text/csv", use_container_width=True)

    with st.expander("预览定稿 Markdown"):
        st.markdown(doc)

    with st.expander("📜 学术诚信声明（请阅读）"):
        st.markdown(
            "1. 本系统为**写作辅助工具**，不提供一键成文服务，所有内容必须经作者本人核对与修改。\n"
            "2. 系统内的合规检查基于规则与模型启发式判断，**不能替代**学校指定的查重系统与导师审核。\n"
            "3. 引用他人观点必须规范标注，禁止编造文献、数据与实验结果。\n"
            "4. 全部操作已记录在审计日志中，可随时导出备查。\n"
        )


# =============================================================================
# 十一、主程序
# =============================================================================

def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="🎓", layout="wide",
                       initial_sidebar_state="expanded")

    init_session()
    render_sidebar()

    ps = require_project()
    if not ps:
        st.title("🎓 %s" % APP_NAME)
        st.markdown(
            "一个以 **合规设计 / 人在回路 / 审计日志** 为核心的毕业论文写作辅助系统。\n\n"
            "- 🤖 **规划师 Agent**：生成与修订大纲\n"
            "- ✍️ **撰写员 Agent**：按已批准大纲逐章写作\n"
            "- 🛡️ **合规审查员 Agent**：引用格式、学术诚信、逻辑一致性审查（命中阻断项即拦截）\n"
            "- 🧾 **日志记录员 Agent**：全程记录，哈希链防篡改，可导出\n\n"
            "**它不提供“一键写完”**：每个环节生成后都会暂停，等待你的修改与批准。\n\n"
            "👉 请在左侧边栏填写论文题目并创建项目。"
        )
        return

    meta = ps["meta"]
    st.title("🎓 %s" % meta.get("title", "未命名论文"))
    wf = compute_workflow(ps)
    st.caption("专业方向：%s ｜ 目标字数：%s ｜ 文献格式：%s ｜ 流程状态：%s"
               % (meta.get("major"), meta.get("target_words"), meta.get("citation_style"), wf["state_label"]))
    if SS["llm"].offline:
        st.info("ℹ️ 当前为离线演示模式（未配置 `DEEPSEEK_API_KEY`），生成的文本来自本地模板，"
                "仅用于演示 HITL 流程与审计日志。配置环境变量后重启应用即可调用 `deepseek-chat`。")

    tabs = st.tabs(["📋 概览与流程", "⚡ 一键生成", "🧭 规划师 · 大纲", "✍️ 撰写员 · 章节",
                    "🛡️ 合规审查", "🧾 审计日志", "📤 导出"])

    with tabs[0]:
        render_overview(ps)
    with tabs[1]:
        render_auto_tab(ps)
    with tabs[2]:
        render_planner_tab(ps)
    with tabs[3]:
        render_writer_tab(ps)
    with tabs[4]:
        render_compliance_tab(ps)
    with tabs[5]:
        render_audit_tab(ps)
    with tabs[6]:
        render_export_tab(ps)


if __name__ == "__main__":
    main()
