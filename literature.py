# -*- coding: utf-8 -*-
"""
真实文献检索与著录格式化模块
================================================================================

为什么要单独做这个模块？
    "让大模型帮你写参考文献" 是学术写作里最危险的操作——模型会一本正经地编造出
    看起来完全合理的作者、期刊、卷期和页码（业内称 halluciated citations）。
    所以本模块**只从真实文献数据库检索元数据**，绝不生成：

        OpenAlex    https://api.openalex.org      覆盖面最广的开放学术图谱（含中文期刊）
        Crossref    https://api.crossref.org      DOI 官方注册机构，元数据最权威

    检索到的每条记录都带真实 DOI / 原文链接，学生可以逐条点开核验。

对外主要接口：
    search_literature(query, limit, year_from, sources) -> List[dict]   检索并去重
    format_reference(record, style) -> str                              单条著录
    build_reference_list(records, style) -> str                         带 [n] 编号的参考文献章节
    has_provenance(record) -> bool                                      是否可溯源（有 DOI/URL）

所有网络请求只用标准库 urllib，不引入额外依赖；任何失败都抛 LiteratureError，
由界面层给出友好提示。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "LiteratureError",
    "search_literature",
    "search_openalex",
    "search_crossref",
    "format_reference",
    "build_reference_list",
    "has_provenance",
    "format_authors_gbt",
    "format_authors_apa",
    "records_to_text_block",
    "parse_reference_text",
    "parse_one_reference",
]

USER_AGENT = "thesis-writing-agent/1.0 (https://github.com/220122/thesis-writing-agent)"
TIMEOUT = 25

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 常见中文姓氏：用于判断「名 姓」还是「姓 名」——OpenAlex 与部分中文出版商
# （如汉斯出版社）在元数据里会把中文姓名存成西文顺序，导致「董泽」变成「泽 董」。
CJK_SURNAMES = set(
    "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤温康施文牛樊葛邢安常易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍华喻祁蒲房滕屈饶解牟艾尤阳时穆农司卓古吉缪简车项连芦麦褚娄窦戚岑景党宫费卜冷晏席卫米柏宗瞿桂全佟应臧闵苟邬边卞姬"
)
CJK_COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "闻人", "夏侯",
    "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶", "太叔",
    "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空",
    "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "漆雕", "乐正",
    "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里", "呼延", "东郭",
    "南门", "羊舌", "微生", "梁丘", "左丘", "西门", "第五", "即墨", "达奚", "褚师",
}


def _is_cjk_surname(token: str) -> bool:
    token = _clean(token)
    return token in CJK_SURNAMES or token in CJK_COMPOUND_SURNAMES

# 文献类型标识（GB/T 7714-2015 附录）
TYPE_MAP = {
    # OpenAlex / Crossref 常见取值 -> GB/T 7714 文献类型标识
    "article": "J", "journal-article": "J", "review": "J", "review-article": "J",
    "book": "M", "monograph": "M", "book-chapter": "M", "edited-book": "M",
    "proceedings": "C", "proceedings-article": "C", "paper-conference": "C",
    "dissertation": "D", "thesis": "D",
    "report": "R", "report-component": "R",
    "dataset": "DB", "database": "DB",
    "posted-content": "EB/OL", "preprint": "EB/OL",
    "standard": "S", "patent": "P",
    "webpage": "EB/OL", "reference-entry": "G", "peer-review": "J",
}


class LiteratureError(RuntimeError):
    """文献检索失败（网络、限流、接口变更等），界面层捕获后给出提示。"""


# =============================================================================
# 一、HTTP 工具
# =============================================================================

def _get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LiteratureError("文献接口限流（429），请等 30 秒再试一次。") from exc
        raise LiteratureError("文献接口返回错误 %s：%s" % (exc.code, exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise LiteratureError(
            "无法访问文献数据库（%s）。请检查网络；若在校园网/公司网络下，"
            "可能需要允许访问 api.openalex.org 与 api.crossref.org。" % exc.reason
        ) from exc
    except (ValueError, OSError) as exc:
        raise LiteratureError("文献接口响应解析失败：%s" % exc) from exc


# =============================================================================
# 二、字段归一化
# =============================================================================

def _clean(text: Any) -> str:
    if text is None:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))          # 去掉 HTML 标签
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def _split_name(name: str) -> Dict[str, str]:
    """
    把姓名拆成 family / given。

    坑点：OpenAlex 对中文作者名的存储并不统一，实测会出现
        '赵赫'            → 已是「姓名」顺序
        '晓雅 王'          → 「名 姓」顺序（需交换）
        '张, 光闪'         → 带逗号，是「姓, 名」（raw_author_name 更可靠）
        'Qiang Zhong'     → 罗马字母，走西文规则
    所以这里按「先看逗号 → 再看中日韩字符 → 最后按西文规则」处理。
    """
    name = _clean(name)
    if not name:
        return {"family": "", "given": "", "name": ""}

    # 1) 带逗号：明确是「姓, 名」
    if "," in name:
        family, given = name.split(",", 1)
        family, given = _clean(family), _clean(given)
        if CJK_RE.search(family + given):
            # 有些源把中文也写成「名, 姓」，用姓氏表判断后纠正
            if _is_cjk_surname(given) and not _is_cjk_surname(family):
                family, given = given, family
            return {"family": "", "given": "", "name": family + given}   # 中文连写成「姓名」
        return {"family": family, "given": given, "name": name}

    # 2) 中日韩姓名：OpenAlex 常存成「名 姓」，用姓氏表判断后交换
    if CJK_RE.search(name):
        parts = name.split()
        if len(parts) == 2:
            if _is_cjk_surname(parts[1]) and not _is_cjk_surname(parts[0]):
                return {"family": "", "given": "", "name": parts[1] + parts[0]}
            if _is_cjk_surname(parts[0]) and not _is_cjk_surname(parts[1]):
                return {"family": "", "given": "", "name": parts[0] + parts[1]}
            if 1 <= len(parts[0]) <= 2 and 1 <= len(parts[1]) <= 2:
                return {"family": "", "given": "", "name": parts[1] + parts[0]}
        return {"family": "", "given": "", "name": name.replace(" ", "")}

    # 3) 西文姓名
    parts = name.split()
    if len(parts) == 1:
        return {"family": parts[0], "given": "", "name": name}
    return {"family": parts[-1], "given": " ".join(parts[:-1]), "name": name}


def _pages(first: Any, last: Any) -> str:
    first, last = _clean(first), _clean(last)
    if first and last and first != last:
        return "%s-%s" % (first, last)
    return first or last


def _doc_type(raw_type: Any) -> str:
    return TYPE_MAP.get(str(raw_type or "").lower().strip(), "J")


def _normalize_doi(doi: Any) -> str:
    doi = _clean(doi)
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.strip().rstrip(".")


def _record(title: str, authors: List[Dict[str, str]], year: Any, venue: str,
            volume: Any, issue: Any, pages: str, doi: str, url: str,
            doc_type: str, source: str, cited_by: int = 0, abstract: str = "") -> Dict[str, Any]:
    year_int = None
    try:
        year_int = int(str(year)[:4])
    except (TypeError, ValueError):
        year_int = None
    return {
        "title": _clean(title),
        "authors": [a for a in authors if a.get("name") or a.get("family")],
        "year": year_int,
        "venue": _clean(venue),
        "volume": _clean(volume),
        "issue": _clean(issue),
        "pages": _clean(pages),
        "doi": doi,
        "url": _clean(url),
        "type": doc_type,
        "source": source,
        "cited_by": int(cited_by or 0),
        "abstract": _clean(abstract)[:400],
    }


# =============================================================================
# 三、数据源
# =============================================================================

def search_openalex(query: str, limit: int = 10, year_from: Optional[int] = None,
                    language: str = "") -> List[Dict[str, Any]]:
    """
    OpenAlex：覆盖面最广，含大量中文期刊与学位论文。无需 API Key。
    language="zh" 时只返回中文文献（OpenAlex 支持 language 过滤，实测中文命中上万条）。
    """
    params = {
        "search": query,
        "per-page": max(1, min(int(limit), 50)),
        "mailto": "thesis-agent@example.com",   # 加入 polite pool，降低被限流概率
    }
    filters = []
    if year_from:
        filters.append("from_publication_date:%d-01-01" % int(year_from))
    if language:
        filters.append("language:%s" % language)
    if filters:
        params["filter"] = ",".join(filters)
    data = _get_json("https://api.openalex.org/works", params)

    out: List[Dict[str, Any]] = []
    for item in data.get("results") or []:
        authors: List[Dict[str, str]] = []
        for a in item.get("authorships") or []:
            raw = a.get("raw_author_name") or ""
            disp = (a.get("author") or {}).get("display_name") or ""
            # 带逗号的原始名更可靠（"张, 光闪" = 姓, 名），优先用它
            nm = raw if ("," in raw) else (disp or raw)
            if nm:
                authors.append(_split_name(nm))
        loc = item.get("primary_location") or {}
        src = loc.get("source") or {}
        bib = item.get("biblio") or {}
        out.append(_record(
            title=item.get("title") or item.get("display_name") or "",
            authors=authors,
            year=item.get("publication_year"),
            venue=src.get("display_name") or "",
            volume=bib.get("volume"), issue=bib.get("issue"),
            pages=_pages(bib.get("first_page"), bib.get("last_page")),
            doi=_normalize_doi(item.get("doi")),
            url=loc.get("landing_page_url") or item.get("doi") or "",
            doc_type=_doc_type(item.get("type")),
            source="OpenAlex",
            cited_by=item.get("cited_by_count") or 0,
        ))
    return [r for r in out if r["title"]]


def search_crossref(query: str, limit: int = 10, year_from: Optional[int] = None) -> List[Dict[str, Any]]:
    """Crossref：DOI 官方注册机构，作者/卷期/页码等元数据最规范。无需 API Key。"""
    params = {
        "query.bibliographic": query,
        "rows": max(1, min(int(limit), 50)),
        "select": "DOI,title,author,container-title,issued,volume,issue,page,type,URL,abstract,is-referenced-by-count",
    }
    if year_from:
        params["filter"] = "from-pub-date:%d-01-01" % int(year_from)
    data = _get_json("https://api.crossref.org/works", params)

    out: List[Dict[str, Any]] = []
    for item in ((data.get("message") or {}).get("items") or []):
        authors: List[Dict[str, str]] = []
        for a in item.get("author") or []:
            family, given = _clean(a.get("family")), _clean(a.get("given"))
            if family and given and CJK_RE.search(family + given):
                # 中文姓名统一成「姓名」连写；并按姓氏表纠正被写反的「名 姓」
                if _is_cjk_surname(given) and not _is_cjk_surname(family):
                    family, given = given, family
                authors.append({"family": "", "given": "", "name": family + given})
            elif family or given:
                authors.append({"family": family, "given": given,
                                "name": _clean(a.get("name")) or (given + " " + family).strip()})
            elif a.get("name"):
                authors.append(_split_name(a["name"]))
        issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        year = issued[0] if issued else None
        titles = item.get("title") or []
        containers = item.get("container-title") or []
        out.append(_record(
            title=titles[0] if titles else "",
            authors=authors,
            year=year,
            venue=containers[0] if containers else "",
            volume=item.get("volume"), issue=item.get("issue"),
            pages=item.get("page"),
            doi=_normalize_doi(item.get("DOI")),
            url=item.get("URL") or "",
            doc_type=_doc_type(item.get("type")),
            source="Crossref",
            cited_by=item.get("is-referenced-by-count") or 0,
            abstract=item.get("abstract") or "",
        ))
    return [r for r in out if r["title"]]


# =============================================================================
# 四、合并检索
# =============================================================================

def _key(rec: Dict[str, Any]) -> str:
    if rec.get("doi"):
        return "doi:" + rec["doi"].lower()
    return "title:" + re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", rec["title"].lower())[:80]


def search_literature(query: str, limit: int = 10, year_from: Optional[int] = None,
                      sources: Sequence[str] = ("openalex", "openalex_zh", "crossref")) -> List[Dict[str, Any]]:
    """
    多源检索 + 去重 + 排序。返回的每条记录都是**真实存在的文献**（带 DOI 可核验）。

    sources 可选值：
        openalex       OpenAlex 全库（中英文都有，英文文献元数据最全）
        openalex_zh    OpenAlex 仅限中文文献（language:zh，中文期刊/学位论文覆盖最好）
        crossref       Crossref（DOI 官方注册机构，含汉斯出版社等中文开放获取期刊）
    任一数据源失败不影响其它源；全部失败时抛 LiteratureError。
    """
    query = _clean(query)
    if not query:
        raise LiteratureError("检索关键词为空。")

    per_source = max(5, int(limit))
    merged: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    for src in sources:
        try:
            if src == "openalex":
                found = search_openalex(query, per_source, year_from)
            elif src == "openalex_zh":
                time.sleep(0.3)
                found = search_openalex(query, per_source, year_from, language="zh")
                for rec in found:
                    rec["source"] = "OpenAlex中文"
            elif src == "crossref":
                time.sleep(0.3)  # 对公共接口保持礼貌
                found = search_crossref(query, per_source, year_from)
            else:
                continue
        except LiteratureError as exc:
            errors.append("%s: %s" % (src, exc))
            continue

        for rec in found:
            k = _key(rec)
            if k in merged:
                # 交叉源补全：Crossref 的作者拆分更规范，OpenAlex 的引用数更全
                old = merged[k]
                for field in ("authors", "volume", "issue", "pages", "venue", "abstract"):
                    if not old.get(field) and rec.get(field):
                        old[field] = rec[field]
                if not old.get("url") and rec.get("url"):
                    old["url"] = rec["url"]
                old["source"] = old.get("source", "") + "+" + rec.get("source", "")
            else:
                merged[k] = rec

    if not merged:
        if errors:
            raise LiteratureError("；".join(errors))
        raise LiteratureError("没有检索到文献，请换更具体的关键词（建议中英文各试一次）。")

    records = list(merged.values())
    # 排序：有 DOI 的优先 → 年份新 → 被引多
    records.sort(key=lambda r: (0 if r.get("doi") else 1, -(r.get("year") or 0), -r.get("cited_by", 0)))
    return records[: max(1, int(limit))]


# =============================================================================
# 五、著录格式化（GB/T 7714-2015 / APA 第7版）
# =============================================================================

def _initials(given: str) -> str:
    """GB/T 7714 用不带缩写点的首字母：'John A.' -> 'J A'"""
    parts = re.split(r"[\s.\-]+", _clean(given))
    return " ".join(p[0].upper() for p in parts if p)


def _initials_dotted(given: str) -> str:
    """APA 用带缩写点的首字母：'John A.' -> 'J. A.'"""
    parts = re.split(r"[\s.\-]+", _clean(given))
    return " ".join(p[0].upper() + "." for p in parts if p)


def format_authors_gbt(authors: Sequence[Dict[str, str]], max_show: int = 3) -> str:
    """
    GB/T 7714-2015：西文姓名姓前名后且姓全大写、名用缩写（不加缩写点）；
    中文姓名照录；超过 3 位作者时列前 3 位后加 ", 等" 或 ", et al."。
    """
    if not authors:
        return "佚名"
    names: List[str] = []
    western = False
    for a in authors:
        family, given, full = a.get("family", ""), a.get("given", ""), a.get("name", "")
        if family and not CJK_RE.search(family):
            western = True
            names.append(("%s %s" % (family.upper(), _initials(given))).strip())
        elif full or family:
            names.append(full or family)
        else:
            names.append("佚名")

    if len(names) > max_show:
        tail = ", et al" if western else ", 等"
        return ", ".join(names[:max_show]) + tail
    return ", ".join(names)


def format_authors_apa(authors: Sequence[Dict[str, str]]) -> str:
    """APA 第7版：Family, F. M.，最后一位前加 &。"""
    if not authors:
        return "佚名"
    parts: List[str] = []
    for a in authors:
        family, given, full = a.get("family", ""), a.get("given", ""), a.get("name", "")
        if family and given:
            parts.append("%s, %s" % (family, _initials_dotted(given)))
        elif family:
            parts.append(family)
        else:
            parts.append(full or "佚名")
    if len(parts) > 20:
        parts = parts[:19] + ["…"] + [parts[-1]]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", & " + parts[-1]


def format_reference(rec: Dict[str, Any], style: str = "GB/T 7714-2015（顺序编码制）") -> str:
    """把一条文献记录格式化为著录字符串（不含 [n] 序号）。"""
    is_gbt = "GB/T" in style or "7714" in style
    title = rec.get("title") or "（题名缺失）"
    year = rec.get("year") or ""
    venue = rec.get("venue") or ""
    doc_type = rec.get("type") or "J"

    if is_gbt:
        authors = format_authors_gbt(rec.get("authors") or [])
        head = "%s. %s[%s]" % (authors, title, doc_type)
        if doc_type in ("EB/OL", "DB"):
            # GB/T 7714：主要责任者. 题名[EB/OL]. 出版信息. 获取路径.
            url = rec.get("url") or ""
            if rec.get("doi") and (not url or "doi.org" in url.lower()):
                url = ""      # 有 DOI 时不再重复打印 doi 链接
            segs = [head]
            if venue:
                segs.append(venue)
            if url:
                segs.append(url)
            if rec.get("doi"):
                segs.append("DOI: %s" % rec["doi"])
            return ". ".join(segs) + "."
        if venue:
            head += ". %s" % venue
        if year:
            head += ", %s" % year
        if rec.get("volume"):
            head += ", %s" % rec["volume"]
            if rec.get("issue"):
                head += "(%s)" % rec["issue"]
        if rec.get("pages"):
            head += ": %s" % rec["pages"]
        head += "."
        if rec.get("doi"):
            head += " DOI: %s." % rec["doi"]
        return head

    # APA 第7版
    authors = format_authors_apa(rec.get("authors") or [])
    out = "%s (%s). %s." % (authors, year or "n.d.", title)
    if venue:
        out += " %s" % venue
        if rec.get("volume"):
            out += ", %s" % rec["volume"]
            if rec.get("issue"):
                out += "(%s)" % rec["issue"]
        if rec.get("pages"):
            out += ", %s" % rec["pages"]
        out += "."
    if rec.get("doi"):
        out += " https://doi.org/%s" % rec["doi"]
    elif rec.get("url"):
        out += " %s" % rec["url"]
    return out


def build_reference_list(records: Sequence[Dict[str, Any]],
                         style: str = "GB/T 7714-2015（顺序编码制）") -> str:
    """生成带 [n] 连续编号的「## 参考文献」章节。"""
    lines = ["## 参考文献"]
    for i, rec in enumerate(records, start=1):
        lines.append("[%d] %s" % (i, format_reference(rec, style)))
    return "\n".join(lines) + "\n"


def has_provenance(rec: Dict[str, Any]) -> bool:
    """是否可溯源：有 DOI 或原文链接才算通过（用于「来源不可追溯」合规预警）。"""
    return bool(_clean(rec.get("doi")) or _clean(rec.get("url")))


def records_to_text_block(records: Sequence[Dict[str, Any]]) -> str:
    """给大模型看的紧凑文献清单（含编号，便于它在正文里用 [n] 引用）。"""
    lines = []
    for i, rec in enumerate(records, start=1):
        authors = format_authors_gbt(rec.get("authors") or [], max_show=2)
        lines.append("[%d] %s（%s）. %s. %s" % (
            i, authors, rec.get("year") or "年份不详",
            rec.get("title") or "", rec.get("venue") or "来源不详"))
    return "\n".join(lines)


# =============================================================================
# 六、手工导入：从知网 / 万方 / 谷歌学术复制「引用」文本
# =============================================================================
#  说明：知网、万方、维普均无免费开放接口，无法直接检索。
#  但它们每条文献都有「引用」按钮，复制出来的正是 GB/T 7714 著录字符串，
#  学生粘贴进来即可入库，源数据依然是真实文献，只是由人工带入。

_REF_LINE_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)\s*[.、])\s*(.+)$")
_REF_TYPE_RE = re.compile(r"\[([A-Za-z]{1,3}(?:/[A-Za-z]{2})?)\]")
_DOI_RE = re.compile(r"(?:doi[:\s]*|doi\.org/)(10\.\d{4,9}/[^\s,;，；]+)", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def parse_one_reference(body: str) -> Optional[Dict[str, Any]]:
    """把单条 GB/T 7714 风格著录文本解析成文献记录（启发式，允许字段不完整）。"""
    body = _clean(body)
    if not body or len(body) < 8:
        return None

    tm = _REF_TYPE_RE.search(body)
    doc_type = tm.group(1).upper() if tm else "J"

    authors_raw, rest = "", body
    m = re.match(r"^(?P<a>[^.]{2,120}?)\.\s*(?P<r>.+)$", body)
    if m:
        authors_raw, rest = m.group("a"), m.group("r")

    if tm and tm.start() > 0:
        title = body[:tm.start()].split(". ", 1)[-1].strip(" .")
        after = body[tm.end():]
    else:
        pieces = rest.split(". ", 1)
        title = pieces[0].strip(" .")
        after = pieces[1] if len(pieces) > 1 else ""

    year = None
    ym = _YEAR_RE.search(after) or _YEAR_RE.search(body)
    if ym:
        year = int(ym.group(0))

    doi = ""
    dm = _DOI_RE.search(body)
    if dm:
        doi = dm.group(1).rstrip(".")

    url = ""
    um = re.search(r"(https?://[^\s，；]+)", body)
    if um:
        url = um.group(1).rstrip(".")

    volume = issue = pages = ""
    vm = re.search(r"(\d+)\s*\(\s*(\d+)\s*\)", after)
    if vm:
        volume, issue = vm.group(1), vm.group(2)
    pm = re.search(r":\s*(\d+\s*[-–—]\s*\d+)", after)
    if pm:
        pages = pm.group(1).replace(" ", "").replace("–", "-").replace("—", "-")

    # 刊名/来源：取到第一个逗号或左括号之前，且绝不能把 URL 当刊名
    venue = re.split(r"[,，(（]", after)[0].strip(" .") if after else ""
    if re.fullmatch(r"(19|20)\d{2}.*", venue) or "http" in venue.lower():
        venue = ""

    # 电子资源：保留「(更新日期)[引用日期]」在 URL 之前（GB/T 7714 对 EB/OL 的要求）
    if doc_type in ("EB/OL", "DB"):
        dm2 = re.match(r"^\s*(\([^)]*\)\s*(?:\[[^\]]*\])?)\s*\.?\s*", after)
        if dm2:
            prefix = dm2.group(1).strip()
            url = (prefix + ". " + url) if url else prefix

    authors: List[Dict[str, str]] = []
    for name in re.split(r"[,，;；]\s*|\s+and\s+", authors_raw):
        name = name.strip(" .")
        if not name or name in ("等", "et al", "et al."):
            continue
        if CJK_RE.search(name):
            authors.append({"family": "", "given": "", "name": name.replace(" ", "")})
        else:
            authors.append(_split_name(name))

    if not title:
        return None
    return _record(title=title, authors=authors, year=year, venue=venue,
                   volume=volume, issue=issue, pages=pages, doi=doi, url=url,
                   doc_type=doc_type, source="手工导入（请核对）")


def parse_reference_text(text: str) -> List[Dict[str, Any]]:
    """
    解析多行著录文本（知网/万方「引用」按钮复制出来的内容），一行一条，
    带 `[1]` 编号或不带编号都可以。
    """
    records: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if len(line) < 12:
            continue
        if re.match(r"^#{0,6}\s*(参考文献|references|引用文献)\s*$", line, re.I):
            continue
        m = _REF_LINE_RE.match(line)
        body = _clean(m.group(3)) if m else line
        rec = parse_one_reference(body)
        if rec:
            records.append(rec)
    return records
