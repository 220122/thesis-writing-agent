# -*- coding: utf-8 -*-
"""
项目自检脚本 —— 无需 API Key 即可运行，用于验证安装是否成功、核心逻辑是否正确。

    python smoke_test.py

包含两部分：
  A. 纯逻辑单元自检：合规规则引擎 / 工作流状态机（HITL 门禁）/ SQLite 审计日志与哈希链
  B. 端到端 UI 自检：用 Streamlit AppTest 真实执行 app.py，模拟"创建项目 → 生成大纲 →
     合规审查 → 批准 → 写第 1 章 → 审查 → 批准 → 验证第 2 章仍被门禁锁住"的完整人工流程
     （离线演示模式，不消耗 API 额度）
"""

import os
import shutil
import sys
import tempfile

try:  # 让 Windows 控制台也能正确显示中文输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PATH = os.path.join(HERE, "app.py")

# 测试隔离：使用项目内的独立数据目录（.smoke_data/，已被 .gitignore 忽略），
# 并强制离线模式，避免自检时误耗 API 额度。
TMP_DIR = os.path.join(HERE, ".smoke_data")
shutil.rmtree(TMP_DIR, ignore_errors=True)
os.makedirs(TMP_DIR, exist_ok=True)
tempfile.tempdir = TMP_DIR  # 让测试期间的临时文件也落在项目内，便于清理
os.environ["THESIS_AGENT_DATA_DIR"] = TMP_DIR
# 关键：设为「空字符串」而不是删除。python-dotenv 默认不覆盖已存在的环境变量，
# 因此即使你跑过 ① 首次安装（项目里已生成 .env），自检也只会走本地演示模板，
# 不会真实调用 DeepSeek API、不会消耗额度。
os.environ["DEEPSEEK_API_KEY"] = ""

PASSED = []
FAILED = []


def check(name, condition, extra=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s %s" % (name, extra))
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, extra))


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# =============================================================================
# A. 纯逻辑单元自检
# =============================================================================

def part_a():
    section("A. 纯逻辑自检：规则引擎 / 状态机 / 审计日志")

    import app

    # Streamlit 运行时尚不存在时，绕开 st.cache_resource 包装
    raw = getattr(app.get_conn, "__wrapped__", None)
    if raw is not None:
        app.get_conn = raw
    app.init_db()

    # ------------------ A1. 学术诚信规则 ------------------
    print("\n[A1] 学术诚信风险表述检测")
    f = app.RuleEngine.check_integrity("调查结果显示，83.5% 的学生存在注意力下降问题。")
    check("数据性论断无来源 → 阻断", any(x["level"] == "BLOCKER" and x["category"] == "数据来源不明" for x in f),
          "命中 %d 条" % len(f))

    f = app.RuleEngine.check_integrity("调查结果显示，83.5% 的学生存在注意力下降问题[2]。")
    check("同一句标注引用后 → 不再误报数据来源",
          not any(x["category"] == "数据来源不明" and x["level"] == "BLOCKER" for x in f))

    f = app.RuleEngine.check_integrity("众所周知，该结论毋庸置疑。")
    check("绝对化/模糊化表述 → 警告", any(x["level"] == "WARNING" for x in f))

    f = app.RuleEngine.check_integrity("本节内容 TODO，后续补充。")
    check("占位符 → 阻断", any(x["level"] == "BLOCKER" and x["category"] == "未完成内容" for x in f))

    # ------------------ A2. 参考文献格式 ------------------
    print("\n[A2] 参考文献格式校验")
    bad_refs = "## 参考文献\n[1] 张三. 一篇没有类型标识也没有年份的文献.\n"
    f = app.RuleEngine.check_references(bad_refs, app.CITATION_STYLES[0])
    check("GB/T 7714 缺类型标识/年份 → 警告", len([x for x in f if x["level"] == "WARNING"]) >= 2,
          "命中 %d 条" % len(f))

    good_refs = ("## 参考文献\n"
                 "[1] 张三. 人工智能应用研究[J]. 中国电化教育, 2022, 43(5): 12-19.\n")
    f = app.RuleEngine.check_references(good_refs, app.CITATION_STYLES[0])
    check("规范条目 → 无警告", not [x for x in f if x["level"] == "WARNING"], "命中 %d 条" % len(f))

    apa_refs = "## 参考文献\n[1] 张三 (2022). 人工智能应用研究. 中国电化教育, 43(5), 12-19.\n"
    f = app.RuleEngine.check_references(apa_refs, app.CITATION_STYLES[1])
    check("APA 格式条目可识别", not [x for x in f if x["level"] == "WARNING"], "命中 %d 条" % len(f))

    # ------------------ A3. 引用交叉核对 ------------------
    print("\n[A3] 正文引用 ↔ 文献表 交叉核对")
    f = app.RuleEngine.check_citations("本研究参考了既有成果[1][2][3]。", good_refs)
    check("引用编号不存在 → 阻断",
          any(x["level"] == "BLOCKER" and x["category"] == "引用不存在" for x in f))

    f = app.RuleEngine.check_citations("本研究参考了既有成果[1]。", good_refs)
    check("引用与文献表一致 → 无阻断",
          not any(x["level"] == "BLOCKER" for x in f))

    # ------------------ A4. 自查重（连续重复） ------------------
    print("\n[A4] 自查重：连续重复检测")
    src = ("短视频的即时反馈机制显著削弱了大学生的持续注意能力，这一结论已在多项研究中得到验证，"
           "并逐渐成为教育技术领域的共识性判断。")
    body = "有学者指出：" + src[:40] + "，因此需要引起重视[1]。"
    f = app.RuleEngine.check_similarity(body, [("某已发表论文", src)])
    check("中文连续重复 ≥13 字 → 阻断",
          any(x["level"] == "BLOCKER" and x["category"] == "疑似抄袭/重复" for x in f))

    f = app.RuleEngine.check_similarity("这是一段完全原创的表述，与他人文本没有重合片段[1]。",
                                        [("某已发表论文", src)])
    check("无重复片段 → 通过", not f)

    # ------------------ A5. 格式规范 ------------------
    print("\n[A5] 格式规范检查")
    f = app.RuleEngine.check_format("中文句子,这里混用了半角逗号。\n\n## 一级标题\n\n#### 跳级标题\n")
    check("中英标点混用 → 警告", any(x["category"] == "标点规范" for x in f))
    check("标题层级跳级 → 警告", any(x["category"] == "结构规范" for x in f))

    f = app.RuleEngine.check_format("## 一\n### 二\n正文内容。", kind="chapter")
    check("层级正常 → 无结构警告", not any(x["category"] == "结构规范" for x in f))

    # ------------------ A6. 大纲解析与字数分配 ------------------
    print("\n[A6] 大纲解析 / 字数分配 / 状态机门禁")
    outline_text = ("## 第1章 绪论\n### 1.1 研究背景\n### 1.2 研究意义\n\n"
                    "## 第2章 文献综述\n### 2.1 国内研究\n### 2.2 国外研究\n### 2.3 研究述评\n\n"
                    "## 第3章 结论\n### 3.1 主要结论\n")
    chapters = app.parse_outline(outline_text, 5)
    check("大纲解析出 3 章", len(chapters) == 3, "实际 %d" % len(chapters))
    check("小节解析正确", chapters[0]["sections"] == ["研究背景", "研究意义"],
          str(chapters[0]["sections"]))
    targets = app.allocate_word_targets(12000, chapters)
    check("字数按小节点数加权分配", sum(targets) <= 12000 and targets[1] > targets[0],
          str(targets))

    meta = {"title": "测试论文", "major": "教育技术学", "target_words": 12000,
            "chapter_count": 3, "citation_style": app.CITATION_STYLES[0],
            "research_type": "实证研究（问卷/实验）", "student": "测试同学", "advisor": "测试导师"}
    ps = app.new_project_state(meta)
    ps["outline"]["text"] = outline_text
    ps["outline"]["version"] = 1
    app.sync_state(ps)
    check("未批准大纲 → 状态为待审阅", ps["state"] == app.ST_OUTLINE_DRAFT, ps["state"])

    wf = app.compute_workflow(ps)
    check("未审查 → 大纲存在阻断（禁止批准）", len(wf["outline_blockers"]) > 0)

    # 模拟一次通过的审查结论
    ps["reviews"]["outline"] = {"version": 1, "ts": app.now_ts(), "findings": [], "summary": ""}
    wf = app.compute_workflow(ps)
    check("审查通过 → 无阻断，可批准", len(wf["outline_blockers"]) == 0)

    ps["outline"]["status"] = "approved"
    ps["chapters"] = [dict(c, target_words=t, text="", version=0, status="pending",
                           approved_at="", feedback="") for c, t in zip(chapters, targets)]
    app.sync_state(ps)
    check("大纲批准后 → 状态进入撰写阶段", ps["state"] == app.ST_OUTLINE_APPROVED, ps["state"])

    # 第 2 章未被解锁（第 1 章未批准）
    ps["chapters"][0]["text"] = "第1章初稿"
    ps["chapters"][0]["status"] = "draft"
    prev_unapproved = [c for c in ps["chapters"] if c["index"] < 2 and c["status"] != "approved"]
    check("第1章未批准 → 第2章被门禁锁住", len(prev_unapproved) == 1)
    ps["chapters"][0]["status"] = "approved"
    prev_unapproved = [c for c in ps["chapters"] if c["index"] < 2 and c["status"] != "approved"]
    check("第1章批准后 → 第2章解锁", len(prev_unapproved) == 0)

    # 改稿使审查失效
    ps["reviews"]["chapter_1"] = {"version": 1, "ts": app.now_ts(), "findings": []}
    check("版本一致 → 审查有效", app.review_is_fresh(ps, "chapter_1", 1))
    check("版本变化 → 审查失效（必须重审）", not app.review_is_fresh(ps, "chapter_1", 2))

    # ------------------ A7. 参考文献合并 ------------------
    print("\n[A7] 参考文献库合并去重")
    merged = app.merge_references("", app._MOCK_REFERENCES)
    n1 = len(app.parse_reference_entries(merged))
    merged2 = app.merge_references(merged, app._MOCK_REFERENCES)
    n2 = len(app.parse_reference_entries(merged2))
    check("重复合并后条目数不变", n1 == n2 == 6, "%d → %d" % (n1, n2))

    # ------------------ A8. 审计日志 & 哈希链 ------------------
    print("\n[A8] 审计日志与哈希链防篡改")
    for i in range(3):
        app.LOG_KEEPER.log(actor="测试Agent", event_type="AI_OUTPUT", stage="outline",
                           target="outline", prompt="第%d次调用" % i,
                           ai_output="输出内容%d" % i, status="draft", project_id="SMOKE-TEST")
    rows = app.fetch_logs("SMOKE-TEST")
    check("审计日志写入 3 条", len(rows) == 3, "实际 %d" % len(rows))
    ok, broken = app.verify_hash_chain("SMOKE-TEST")
    check("哈希链校验通过", ok and not broken)

    app.db_execute("UPDATE audit_logs SET status='tampered' WHERE project_id='SMOKE-TEST' AND id=(SELECT MIN(id) FROM audit_logs WHERE project_id='SMOKE-TEST')")
    ok2, broken2 = app.verify_hash_chain("SMOKE-TEST")
    check("篡改日志后被检出", (not ok2) and len(broken2) >= 1, "异常记录 %s" % broken2)

    check("日志可导出 JSON/CSV",
          len(app.logs_to_json_bytes(rows)) > 10 and b"prompt" in app.logs_to_csv_bytes(rows))

    # ------------------ A9. 离线演示模式 ------------------
    print("\n[A9] 离线演示模式（未配置 API Key）")
    llm = app.LLMClient()
    check("自动进入离线模式", llm.offline)
    planner = app.PlannerAgent(llm)
    text = "".join(planner.generate_outline(meta, extra="", feedback=""))
    check("离线可生成大纲且可解析", len(app.parse_outline(text, 3)) == 3)
    mock_outline = app.parse_outline(text, 3)
    writer = app.WriterAgent(llm)
    ch = dict(mock_outline[0], text="", version=0, status="pending",
              target_words=800, approved_at="", feedback="")
    chapter_text = "".join(writer.write_chapter(meta, text, ch, 800,
                                                references=app._MOCK_REFERENCES))
    findings = app.RuleEngine.run_all(text=chapter_text, kind="chapter",
                                      style=app.CITATION_STYLES[0], target_name=ch["title"],
                                      target_words=800, project_references=app._MOCK_REFERENCES,
                                      chapters=[ch], outline_chapters=mock_outline, chapter=ch)
    blockers = [x for x in findings if x["level"] == "BLOCKER"]
    check("离线演示初稿可通过合规规则（无阻断）", not blockers,
          ("阻断项：" + str([b["message"][:30] for b in blockers])) if blockers else "")

    # ------------------ A10. 用户修改留痕（diff → 审计日志） ------------------
    print("\n[A10] 用户修改留痕（AI初稿 vs 用户修改稿）")
    old = "## 第1章 绪论\n\n本研究认为短视频对注意力没有影响。\n"
    new = "## 第1章 绪论\n\n本研究认为短视频的即时反馈机制可能削弱持续注意能力[1]。\n"
    diff = app.unified_diff(old, new, "第1章 v1")
    check("diff 包含 + / - 行", ("+" in diff) and ("-" in diff), diff.splitlines()[-1][:40] if diff else "")
    app.LOG_KEEPER.log(actor="测试同学", actor_role="HUMAN", event_type="USER_EDIT",
                       stage="chapter", target="chapter_1", prompt="用户编辑第1章",
                       ai_output=old, user_edit=diff, status="draft",
                       detail={"from_version": 1, "to_version": 2}, project_id="SMOKE-EDIT")
    row = app.fetch_logs("SMOKE-EDIT")[0]
    check("审计日志保存了 AI 原稿", old.strip() in (row["ai_output"] or ""))
    check("审计日志保存了用户修改 diff", "本研究认为短视频" in (row["user_edit"] or "")
          and "+" in (row["user_edit"] or ""))
    check("审计日志记录了角色与最终状态",
          row["actor"] == "测试同学" and row["actor_role"] == "HUMAN" and row["status"] == "draft")

    # ------------------ A11. .env 读取链路（兼容记事本 BOM / 不覆盖已有变量） ------------------
    print("\n[A11] .env → 应用 的 API Key 读取链路")
    env_path = os.path.join(TMP_DIR, ".env")
    # 模拟「记事本另存为 UTF-8」：带 BOM
    with open(env_path, "wb") as fp:
        fp.write("\ufeff".encode("utf-8"))
        fp.write(b"DEEPSEEK_API_KEY=sk-bom-test-9999\n")
    from dotenv import load_dotenv
    os.environ["DEEPSEEK_API_KEY"] = ""
    load_dotenv(env_path, encoding="utf-8-sig")
    check("已存在的环境变量不会被 .env 覆盖（保证自检离线）",
          os.getenv("DEEPSEEK_API_KEY") == "", repr(os.getenv("DEEPSEEK_API_KEY")))
    os.environ.pop("DEEPSEEK_API_KEY", None)
    load_dotenv(env_path, encoding="utf-8-sig")
    check("带 BOM 的 .env 也能被正确读取", os.getenv("DEEPSEEK_API_KEY") == "sk-bom-test-9999",
          str(os.getenv("DEEPSEEK_API_KEY")))
    os.environ["DEEPSEEK_API_KEY"] = ""   # 恢复离线
    with open(APP_PATH, "r", encoding="utf-8") as fp:
        src = fp.read()
    check("app.py 在模块级用 utf-8-sig 读取 .env", 'encoding="utf-8-sig"' in src)

    # ------------------ A12. 真实文献模块（免费开放数据源） ------------------
    print("\n[A12] 文献检索模块：著录格式 / 手工导入 / 溯源判定")
    import literature as lit_mod

    check("作者名「名 姓」被纠正", lit_mod._split_name("晓雅 王")["name"] == "王晓雅",
          lit_mod._split_name("晓雅 王")["name"])
    check("作者名「姓, 名」被纠正", lit_mod._split_name("张, 光闪")["name"] == "张光闪",
          lit_mod._split_name("张, 光闪")["name"])
    check("西文作者缩写（GB/T 无缩写点）",
          lit_mod.format_authors_gbt([{"family": "Smith", "given": "John A.", "name": ""}]) == "SMITH J A",
          lit_mod.format_authors_gbt([{"family": "Smith", "given": "John A.", "name": ""}]))
    check("西文作者缩写（APA 带缩写点）",
          "Smith, J. A." in lit_mod.format_authors_apa([{"family": "Smith", "given": "John A.", "name": ""}]))

    cn_ref = ("[1] 赵赫, 于晟伟, 王洪庆. 短视频沉迷对大学生的注意力下降影响[J]. "
              "中国科学与技术学报, 2025, 1(1): 947-955.")
    recs = lit_mod.parse_reference_text(cn_ref)
    check("知网引用文本可解析", len(recs) == 1)
    if recs:
        r = recs[0]
        check("解析出题名/年份/卷期页",
              r["title"].startswith("短视频沉迷") and r["year"] == 2025
              and r["volume"] == "1" and r["issue"] == "1" and r["pages"] == "947-955",
              "%s | %s | %s(%s): %s" % (r["title"][:16], r["year"], r["volume"], r["issue"], r["pages"]))
        check("无 DOI 时判定为不可溯源", not lit_mod.has_provenance(r))
        gbt = lit_mod.format_reference(r, "GB/T 7714-2015（顺序编码制）")
        check("GB/T 著录含类型标识与卷期页", "[J]" in gbt and "1(1)" in gbt and "947-955" in gbt, gbt[:60])

    with_doi = dict(recs[0], doi="10.1234/abcd.2025.001") if recs else {}
    if with_doi:
        check("有 DOI 时判定为可溯源", lit_mod.has_provenance(with_doi))
        out = lit_mod.format_reference(with_doi)
        check("GB/T 著录带 DOI", "DOI: 10.1234/abcd.2025.001" in out, out[-40:])
        apa = lit_mod.format_reference(with_doi, "APA 第7版")
        check("APA 著录带 https://doi.org/", "https://doi.org/10.1234/abcd.2025.001" in apa, apa[-46:])

    lst = lit_mod.build_reference_list([with_doi] if with_doi else [])
    check("参考文献列表自动编号", lst.startswith("## 参考文献") and "[1] " in lst)

    findings = app.RuleEngine.check_reference_provenance(cn_ref)
    check("合规规则：来源不可追溯 → 预警",
          any(f["category"] == "来源不可追溯" for f in findings))
    findings = app.RuleEngine.check_reference_provenance(
        cn_ref.replace("947-955.", "947-955. DOI: 10.1234/x."))
    check("合规规则：补齐 DOI 后不再预警", not findings)

    # 供 B 部分复用的临时工程（不落库，仅内存态）
    return True

# =============================================================================
# B. 端到端 UI 自检（Streamlit AppTest，真实执行 app.py）
# =============================================================================

def find_element(elements, substr, kind="元素"):
    for e in elements:
        label = getattr(e, "label", None)
        if label is None:
            label = str(getattr(e, "value", ""))
        if substr in str(label):
            return e
    raise AssertionError("未找到%s：%s" % (kind, substr))


def part_b():
    section("B. 端到端 UI 自检：完整人在回路流程（离线演示模式）")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:
        print("  [SKIP] 当前 Streamlit 版本不支持 AppTest（需 ≥1.28），跳过端到端测试。")
        return

    at = AppTest.from_file(APP_PATH, default_timeout=180)
    at.run()
    check("应用首屏加载无异常", not at.exception, str(list(at.exception)))

    # --- 1. 创建项目 ---
    at.sidebar.text_input[0].set_value("短视频使用对大学生注意力的影响研究")
    at.sidebar.text_input[1].set_value("教育技术学")
    find_element(at.sidebar.button, "创建项目").click()
    at.run()
    check("创建项目后无异常", not at.exception, str(list(at.exception)))
    ps = at.session_state["pstate"]
    check("项目状态已初始化", ps is not None and ps["state"] == app_state_init(),
          ps["state"] if ps else "None")

    # --- 2. 生成大纲 ---
    find_element(at.button, "生成大纲").click()
    at.run()
    check("生成大纲无异常", not at.exception, str(list(at.exception)))
    ps = at.session_state["pstate"]
    check("大纲已生成并入库", "第1章" in ps["outline"]["text"] and ps["outline"]["version"] == 1)
    check("生成后进入 HITL 暂停点（待审阅）", ps["outline"]["status"] == "draft", ps["outline"]["status"])

    # --- 3. 大纲合规审查 ---
    find_element(at.button, "运行合规审查（大纲）").click()
    at.run()
    check("大纲合规审查无异常", not at.exception, str(list(at.exception)))
    rv = at.session_state["pstate"]["reviews"].get("outline")
    check("已生成大纲审查结论", rv is not None and rv["version"] == 1,
          "阻断 %s / 警告 %s" % (rv["blockers"], rv["warnings"]) if rv else "无")

    # --- 4. 批准大纲（需先勾选诚信声明）---
    approve = find_element(at.button, "批准通过大纲")
    check("未勾选声明时批准按钮禁用", bool(getattr(approve, "disabled", False)))
    find_element(at.checkbox, "我已逐条核对以上大纲").check()
    at.run()
    find_element(at.button, "批准通过大纲").click()
    at.run()
    check("批准大纲无异常", not at.exception, str(list(at.exception)))
    ps = at.session_state["pstate"]
    check("大纲状态 → 已批准", ps["outline"]["status"] == "approved")
    check("章节结构已按大纲初始化", len(ps["chapters"]) == 5, "章节数 %d" % len(ps["chapters"]))

    # --- 5. 门禁：第 2 章应被锁住 ---
    idx2 = 1  # selectbox 选项下标对应第 2 章
    at.selectbox(key="writer_chapter_select").select_index(idx2)
    at.run()
    gen2 = [b for b in at.button if "第2章初稿" in str(b.label)]
    check("第1章未批准 → 第2章生成按钮不存在或禁用",
          (not gen2) or bool(getattr(gen2[0], "disabled", False)))

    # --- 6. 写第 1 章 ---
    at.selectbox(key="writer_chapter_select").select_index(0)
    at.run()
    find_element(at.button, "初稿").click()
    at.run()
    check("生成第1章无异常", not at.exception, str(list(at.exception)))
    ps = at.session_state["pstate"]
    ch1 = ps["chapters"][0]
    check("第1章初稿已生成", len(ch1["text"]) > 200, "%d 字符" % len(ch1["text"]))
    check("第1章状态为待审阅", ch1["status"] == "draft")

    # --- 7. 第1章合规审查 + 批准 ---
    find_element(at.button, "运行合规审查（本章）").click()
    at.run()
    check("第1章合规审查无异常", not at.exception, str(list(at.exception)))
    rv = at.session_state["pstate"]["reviews"].get("chapter_1")
    check("已生成第1章审查结论", rv is not None,
          "阻断 %s / 警告 %s" % (rv["blockers"], rv["warnings"]) if rv else "无")

    find_element(at.checkbox, "我已逐字核对本章内容").check()
    at.run()
    find_element(at.button, "批准通过第1章").click()
    at.run()
    ps = at.session_state["pstate"]
    check("第1章已批准", ps["chapters"][0]["status"] == "approved")

    # --- 8. 新会话恢复 + 第 2 章解锁 ---
    #  重新打开一个 AppTest（等价于刷新浏览器）：进度应从 SQLite 恢复，且第 2 章变为可写。
    at2 = AppTest.from_file(APP_PATH, default_timeout=180)
    at2.run()
    check("刷新后无异常", not at2.exception, str(list(at2.exception)))
    ps2 = at2.session_state["pstate"]
    check("刷新后自动恢复项目进度（SQLite 持久化）",
          bool(ps2) and ps2["chapters"][0]["status"] == "approved")
    check("恢复后默认定位到下一待办章节（第2章）",
          "第2章" in str(at2.selectbox(key="writer_chapter_select").value),
          str(at2.selectbox(key="writer_chapter_select").value))
    gen2 = find_element(at2.button, "第2章初稿")
    check("第1章批准后 → 第2章生成按钮可用", not bool(getattr(gen2, "disabled", False)))

    # --- 9. 审计日志完整性 ---
    import app as app_mod
    raw = getattr(app_mod.get_conn, "__wrapped__", None)
    if raw is not None:
        app_mod.get_conn = raw
    pid = ps2["project_id"]
    logs = app_mod.fetch_logs(pid)
    events = {r["event_type"] for r in logs}
    check("审计日志覆盖全流程事件",
          {"PROJECT_CREATE", "AI_OUTPUT", "COMPLIANCE_CHECK", "APPROVE"} <= events,
          str(sorted(events)))
    check("存在用户人工批准记录（人在回路留痕）",
          any(r["event_type"] == "APPROVE" and r["actor_role"] == "HUMAN" for r in logs))
    ok, broken = app_mod.verify_hash_chain(pid)
    check("端到端产生的日志哈希链完整", ok and not broken, str(broken))
    check("日志记录了 AI 原始输出", any(len(r["ai_output"] or "") > 100 for r in logs))


def app_state_init():
    import app as app_mod
    return app_mod.ST_INIT


# =============================================================================
# C. 一键生成模式端到端自检（离线 mock 大模型 + 打桩的文献检索）
# =============================================================================

FAKE_RECORDS = [
    {"title": "短视频使用与大学生注意力的关系研究", "authors": [{"family": "", "given": "", "name": "张三"}],
     "year": 2023, "venue": "心理科学进展", "volume": "31", "issue": "4", "pages": "612-625",
     "doi": "10.3724/SP.J.1041.2023.00612", "url": "", "type": "J", "source": "测试桩",
     "cited_by": 42, "abstract": ""},
    {"title": "移动短视频对青少年认知加工的影响", "authors": [{"family": "", "given": "", "name": "李四"},
                                                              {"family": "", "given": "", "name": "王五"}],
     "year": 2022, "venue": "中国电化教育", "volume": "43", "issue": "5", "pages": "12-19",
     "doi": "10.1234/cee.2022.005", "url": "", "type": "J", "source": "测试桩",
     "cited_by": 31, "abstract": ""},
    {"title": "The effect of short-video use on sustained attention",
     "authors": [{"family": "Smith", "given": "John A.", "name": ""}],
     "year": 2024, "venue": "Cognitive Processing", "volume": "25", "issue": "2", "pages": "100-115",
     "doi": "10.1007/s10339-024-00001-0", "url": "", "type": "J", "source": "测试桩",
     "cited_by": 18, "abstract": ""},
    {"title": "大学生媒介使用行为研究", "authors": [{"family": "", "given": "", "name": "赵六"}],
     "year": 2021, "venue": "北京大学出版社", "volume": "", "issue": "", "pages": "33-40",
     "doi": "10.5555/book.2021.001", "url": "", "type": "M", "source": "测试桩",
     "cited_by": 5, "abstract": ""},
]


def part_c():
    section("C. 一键生成模式自检（离线 mock + 文献检索打桩）")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:
        print("  [SKIP] 当前 Streamlit 版本不支持 AppTest（需 ≥1.28）。")
        return

    import literature as lit_mod
    import app as app_mod

    calls = []

    def fake_search(query, limit=10, year_from=None,
                    sources=("openalex", "openalex_zh", "crossref")):
        calls.append(query)
        return [dict(r) for r in FAKE_RECORDS]

    orig = lit_mod.search_literature
    lit_mod.search_literature = fake_search
    try:
        at = AppTest.from_file(APP_PATH, default_timeout=300)
        at.run()
        at.sidebar.text_input[0].set_value("短视频使用对大学生注意力的影响研究")
        at.sidebar.text_input[1].set_value("教育技术学")
        find_element(at.sidebar.button, "创建项目").click()
        at.run()
        check("一键模式：创建项目无异常", not at.exception, str(list(at.exception)))

        check("一键模式：未勾选声明时按钮禁用",
              bool(getattr(find_element(at.button, "开始一键生成"), "disabled", False)))
        find_element(at.checkbox, "我已了解").check()
        at.run()
        find_element(at.button, "开始一键生成").click()
        at.run()
        check("一键模式：全流程执行无异常", not at.exception, str(list(at.exception)))

        ps = at.session_state["pstate"]
        check("一键模式：已标记 mode=auto", ps.get("mode") == "auto", str(ps.get("mode")))
        check("一键模式：自动检索了文献", len(calls) > 0, "调用检索 %d 次" % len(calls))
        check("一键模式：文献库已自动入库", len(ps.get("ref_records") or []) >= 3,
              "%d 条" % len(ps.get("ref_records") or []))
        check("一键模式：参考文献文本已生成（带 [n] 编号）",
              "[1]" in (ps.get("references") or ""))
        check("一键模式：大纲已生成并自动批准",
              bool(ps["outline"]["text"]) and ps["outline"]["status"] == "approved")
        written = [c for c in ps["chapters"] if c.get("text")]
        check("一键模式：章节逐章生成完毕", len(written) == len(ps["chapters"]) and len(written) > 0,
              "已写 %d/%d 章" % (len(written), len(ps["chapters"])))
        check("一键模式：每章都留有自动审查结论",
              all(("chapter_%d" % c["index"]) in ps["reviews"] for c in ps["chapters"]))
        events = {r["event_type"] for r in app_mod.fetch_logs(ps["project_id"])}
        check("一键模式：审计日志记录 AUTO_* 事件",
              {"AUTO_START", "AUTO_LITERATURE", "AUTO_OUTLINE", "AUTO_CHAPTER"} <= events,
              str(sorted(e for e in events if e.startswith("AUTO"))))
        check("一键模式：审计日志标记 mode=auto",
              any("auto" in (r["detail"] or "") for r in app_mod.fetch_logs(ps["project_id"])))
        ok, broken = app_mod.verify_hash_chain(ps["project_id"])
        check("一键模式：日志哈希链完整", ok and not broken, str(broken))

        find_element(at.checkbox, "我已通读全部章节").check()
        at.run()
        find_element(at.button, "一键批准全部章节").click()
        at.run()
        ps = at.session_state["pstate"]
        check("一键模式：批量批准后章节全部通过",
              all(c["status"] == "approved" for c in ps["chapters"] if c.get("text")))
    finally:
        lit_mod.search_literature = orig


# =============================================================================
# main
# =============================================================================

def main():
    print("临时数据目录：%s" % TMP_DIR)
    try:
        part_a()
        part_b()
        part_c()
    finally:
        print("\n" + "=" * 78)
        print("自检结果：通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
        if FAILED:
            print("失败项：")
            for name in FAILED:
                print("  - %s" % name)
        print("=" * 78)
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    code = 1 if FAILED else 0
    sys.stdout.flush()
    sys.stderr.flush()
    # 直接退出：跳过解释器退出阶段的临时文件清理（部分受管环境会拒绝访问并打印无关堆栈）
    os._exit(code)


if __name__ == "__main__":
    main()
