"""生成《文档切分策略设计与实现》Word 文档。"""
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Cm

OUT = r"E:\桌面\CodePilot 文档切分策略设计与实现.docx"

doc = Document()

# ---------------- 页面：A4 + 2cm 页边距（可用宽度 17cm） ----------------
sec = doc.sections[0]
sec.page_width = Cm(21.0)
sec.page_height = Cm(29.7)
sec.left_margin = Cm(2.0)
sec.right_margin = Cm(2.0)
sec.top_margin = Cm(2.2)
sec.bottom_margin = Cm(2.2)
USABLE_CM = 17.0

# ---------------- 全局字体 ----------------
CN_FONT = "微软雅黑"
MONO = "Consolas"


def set_cn(style_or_run, font=CN_FONT):
    obj = style_or_run
    obj.font.name = font
    rpr = obj.element.get_or_add_rPr() if hasattr(obj.element, "get_or_add_rPr") else None
    if rpr is not None:
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), font)


set_cn(doc.styles["Normal"])
doc.styles["Normal"].font.size = Pt(10.5)
doc.styles["Normal"].paragraph_format.space_after = Pt(6)
doc.styles["Normal"].paragraph_format.line_spacing = 1.4

for name in ("Heading 1", "Heading 2", "Heading 3", "Title"):
    try:
        st = doc.styles[name]
        set_cn(st)
        st.font.color.rgb = RGBColor(0x1F, 0x30, 0x50)
    except KeyError:
        pass


def h(text, level=1):
    p = doc.add_heading(text, level=level)
    return p


def para(text, bold=False, size=10.5):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    set_cn(run)
    return p


def bullet(text):
    p = doc.add_paragraph(style="List Bullet")
    run = p.add_run(text)
    run.font.size = Pt(10.5)
    set_cn(run)
    return p


def code(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.6)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)
    p.paragraph_format.line_spacing = 1.15
    run = p.add_run(text)
    run.font.name = MONO
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x24, 0x29, 0x2E)
    return p


def table(headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    # 固定布局，否则 Word 会按内容自动撑宽导致溢出页面
    tblPr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)

    hdr = t.rows[0].cells
    for i, name in enumerate(headers):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(name)
        run.bold = True
        run.font.size = Pt(9.5)
        set_cn(run)
    for row in rows:
        cells = t.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(value))
            run.font.size = Pt(9.5)
            set_cn(run)

    if widths:
        total = sum(widths)
        if total > USABLE_CM:  # 等比缩放到版心宽度
            widths = [w * USABLE_CM / total for w in widths]
        widths = [w * 0.98 for w in widths]  # 留一点余量，避免贴边
        # 1) 表格总宽
        tblW = tblPr.find(qn("w:tblW"))
        if tblW is None:
            tblW = OxmlElement("w:tblW")
            tblPr.append(tblW)
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(int(sum(widths) * 567)))
        # 2) 列网格必须一起改，否则 Word/LibreOffice 仍按等分列渲染
        grid = t._tbl.find(qn("w:tblGrid"))
        if grid is not None:
            grid_cols = grid.findall(qn("w:gridCol"))
            for col, w in zip(grid_cols, widths):
                col.set(qn("w:w"), str(int(w * 567)))
        # 3) 每个单元格宽度
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph()
    return t


# ================= 封面 =================
title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = title.add_run("CodePilot 文档切分策略设计与实现")
r.bold = True
r.font.size = Pt(20)
set_cn(r)

sub = doc.add_paragraph()
sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = sub.add_run("基于 9 份真实技术笔记的实测设计与落地")
r.font.size = Pt(12)
r.font.color.rgb = RGBColor(0x60, 0x6A, 0x78)
set_cn(r)

meta = doc.add_paragraph()
meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = meta.add_run("项目：CodePilot 研发知识助手　|　模块：app/rag　|　2026-10")
r.font.size = Pt(9)
r.font.color.rgb = RGBColor(0x80, 0x88, 0x94)
set_cn(r)

doc.add_paragraph()

# ================= 摘要 =================
h("摘要", 1)
para(
    "本文针对 CodePilot 的文档切分环节做一次完整重构。原实现采用固定字符数（512）+ 字符级重叠（50）"
    "的通用切分，在真实语料上会切断命令行、跨专题合并、并产生半截编号的残片。"
    "本文先对 9 份真实技术笔记做结构实测，据此设计「结构感知分块」：以分隔线和小节标题为父边界、"
    "以行为原子单元、以整行重叠替代字符重叠，并把标题路径注入块首以保证上下文自足。"
    "实测结果：99 个分块中，被切断的行 0 个、残缺开头块 0 个、标题识别覆盖 9/9。"
)

# ================= 一、语料实测 =================
h("一、语料实测：先量清楚，再设计", 1)
para(
    "切分策略没有通用最优解，只有与语料结构匹配的解。因此第一步不是写代码，"
    "而是把真实语料的结构量化出来。实测对象为 9 份个人技术笔记（命令速查、环境配置、工具手册）。"
)
para(
    "以下指标不再手工统计，而由 scripts/analyze_corpus.py 一次算出；"
    "语料变化后重跑即可复现，文件清单见 eval/tech_notes.txt。",
    size=9,
)

h("1.1 规模与行长分布", 2)
table(
    ["指标", "数值", "说明"],
    [
        ["文档数", "9 份", "个人技术笔记，均为 .txt"],
        ["总大小", "40.7 KB", "小体量，但结构差异大"],
        ["非空行数", "898 行", "平均每份 100 行"],
        ["行长中位数 P50", "29 字符", "极短，一行就是一条命令"],
        ["行长 P90", "56 字符", "90% 的行在 56 字符以内"],
        ["行长 P99", "78 字符", ""],
        ["最长行", "153 字符", "没有一行超过 200 字符"],
        ["超过 512 字符的行", "0 条", "关键结论：没有任何一行会被迫硬切"],
    ],
    widths=[4.2, 3.2, 8.6],
)

para(
    "这条数据直接决定了设计方向：既然没有任何一行超过 512 字符，"
    "那么「行」就可以作为不可分割的原子单元，永远不会触发硬切。",
    bold=True,
)

h("1.2 结构标记统计", 2)
table(
    ["标记形态", "出现次数", "示例", "语义层级"],
    [
        ["【…】中文序号", "55", "【一、文件与目录操作 - 每天都在用】", "章级标题"],
        ["连续 - （≥5）", "74", "---------- Docker 安装与下载 ----------", "小节标题 / 分隔线"],
        ["连续 = （≥5）", "22", "====================", "文档标题包夹线"],
        ["无任何标记", "2 份", "ngrok命令.txt、jwt.txt", "纯行序列，走字符切分兜底"],
    ],
    widths=[3.6, 2.4, 6.6, 3.4],
)
para(
    "结论：语料是典型的「两级结构」——章（【一、…】）与节（---- 小节 ----）构成天然父边界，"
    "节内是大量短行。这正是字符切分会失效的地方。"
)

# ================= 二、旧策略缺陷 =================
h("二、原策略的三个缺陷（附实证）", 1)
para(
    "原实现为 DocumentProcessor：chunk_size=512、chunk_overlap=50，"
    "分隔符优先级为 \\n\\n → \\n → 句号 → 逗号 → 空格 → 硬切，重叠按 50 个字符截取。"
)

h("缺陷 1：切点落在行中间，产生半截编号", 2)
para("在 Git 速查手册上的实际结果：")
code(
    "CHUNK 0 结尾：\n"
    "    2. 查看状态\n"
    "       命令：git status\n"
    "\n"
    "CHUNK 1 开头：\n"
    "    . 查看状态          ← “2” 丢失，只剩 “. 查看状态”\n"
    "       命令：git status"
)
para(
    "原因：字符级重叠取的是「上一块最后 50 个字符」，切点落在 “2” 与 “.” 之间。"
    "行是语义原子，在行内任意位置切断都会破坏语义。"
)

h("缺陷 2：跨节合并，上下文被污染", 2)
para(
    "字符累计不认识 【一、…】 和 ---- 小节 ---- 这类结构标记，"
    "当上一节剩余不足 512 字符时会继续吞入下一节内容。"
    "检索命中后注入 prompt 的上下文里夹着不相关章节，模型容易答偏。"
)

h("缺陷 3：块长超出设定上限", 2)
para(
    "重叠实现为 result.append((tail + chunk)[: chunk_size + chunk_overlap])，"
    "因此块长可达 512 + 50 = 562 字符。实测最大块为 553 字符，超出 chunk_size 设定值。"
)

# ================= 三、新策略 =================
h("三、新策略：结构感知分块", 1)
para("设计目标一句话：行不切、节不跨、路径带上。")

h("3.1 分层模型", 2)
code(
    "文档\n"
    " ├─ 标题    （==== 包夹 / 下划线式 / 首行）\n"
    " ├─ 章  【一、文件与目录操作】\n"
    " │   ├─ 节  ---------- Docker 安装与下载 ----------\n"
    " │   │    ├─ 行   docker --version      查看版本      ← 原子单元\n"
    " │   │    └─ 行   docker info           查看系统信息  ← 原子单元\n"
    " │   └─ 节  ...\n"
    " └─ 章  【二、…】\n"
    "\n"
    "聚合规则：只在同一「节」内部按行累加；跨节必须断开。"
)

h("3.2 五条核心规则", 2)
table(
    ["规则", "做法", "解决的问题"],
    [
        ["① 行是原子", "任何一行整体保留，绝不在行内断开", "半截编号、命令被切碎"],
        ["② 节是父边界", "遇到 【…】 或 ---- 分隔线即落块并切换路径", "跨节污染上下文"],
        ["③ 按行聚合", "节内逐行累加到 target_chars 即落块", "块长可控且切点自然"],
        ["④ 整行重叠", "用上一块最后 1 行作为下一块开头", "字符重叠产生的残片"],
        ["⑤ 路径注入", "块首拼上 [文件标题 > 章 > 节]", "块脱离全文不自足"],
    ],
    widths=[3.0, 6.4, 6.6],
)

h("3.3 参数设定", 2)
table(
    ["参数", "取值", "依据"],
    [
        ["target_chars", "480", "主题集中；约为 P90 行长（64）的 7~8 行"],
        ["max_chars", "640", "上限兜底，实测均值为 380，未逼近上限"],
        ["overlap_lines", "1", "整行重叠，只重复 1 行即可兜住交界"],
        ["inject_path", "True", "块首注入标题路径，替代父子检索"],
    ],
    widths=[3.4, 2.4, 10.2],
)

# ================= 四、代码 =================
h("四、代码实现", 1)
para("新增文件：backend/app/rag/structured_splitter.py（不依赖 langchain_text_splitters，只依赖 langchain_core.Document）。")

h("4.1 结构标记识别", 2)
code(
    "import re\n"
    "\n"
    "# 文件/节级分隔线：连续 5 个以上的 = 或 -\n"
    "_RE_RULE = re.compile(r\"^[=\\-]{5,}\\s*$\")\n"
    "# 中文章标题：【一、文件与目录操作】\n"
    "_RE_SECTION_CN = re.compile(r\"^【(?P<title>[^】]+)】\\s*$\")\n"
    "# 小节标题：---------- Docker 安装与下载 ----------\n"
    "_RE_SUBSECTION = re.compile(r\"^-{3,}\\s*(?P<title>\\S.*?)\\s*-{3,}$\")"
)

h("4.2 标题识别（兼容三种写法）", 2)
para("三种写法必须先判断再取，否则会把「生成时间：…」这类行误当标题。")
code(
    "non_empty = [i for i, line in enumerate(raw_lines) if line.strip()]\n"
    "if non_empty:\n"
    "    first = non_empty[0]\n"
    "    second = non_empty[1] if len(non_empty) > 1 else None\n"
    "    if _RE_RULE.match(raw_lines[first].strip()):\n"
    "        # B. 上下包夹式：首行是 ====，标题在其后\n"
    "        for idx in non_empty[1:]:\n"
    "            if not _RE_RULE.match(raw_lines[idx].strip()):\n"
    "                doc_title = raw_lines[idx].strip()\n"
    "                break\n"
    "    elif second is not None and _RE_RULE.match(raw_lines[second].strip()):\n"
    "        # A. 下划线式：标题 + ====\n"
    "        doc_title = raw_lines[first].strip()\n"
    "    else:\n"
    "        # C. 无分隔线：取首行\n"
    "        doc_title = raw_lines[first].strip()"
)

h("4.3 节内按行聚合（核心）", 2)
code(
    "def _pack(self, section_path, lines):\n"
    "    result, current, current_len, start = [], [], 0, 0\n"
    "    for offset, line in enumerate(lines):\n"
    "        line_len = len(line) + 1\n"
    "        # 单行超长兜底：整行保留，绝不硬切\n"
    "        if line_len > self.max_chars:\n"
    "            if current:\n"
    "                result.append((current, start)); current, current_len = [], 0\n"
    "            result.append(([line], offset))\n"
    "            continue\n"
    "        if not current:\n"
    "            start = offset\n"
    "        # 达到目标长度就落块\n"
    "        if current_len + line_len > self.target_chars and current:\n"
    "            result.append((current, start))\n"
    "            # 整行重叠：拿上一块最后 1 行做新块开头\n"
    "            carry = current[-self.overlap_lines:] if self.overlap_lines else []\n"
    "            current = list(carry)\n"
    "            current_len = sum(len(x) + 1 for x in current)\n"
    "            start = offset - len(carry)\n"
    "        current.append(line)\n"
    "        current_len += line_len\n"
    "    if current:\n"
    "        result.append((current, start))\n"
    "    return result"
)

h("4.4 块内容与元数据", 2)
code(
    "body = \"\\n\".join(chunk_lines).strip()\n"
    "content = f\"[{section_path}]\\n{body}\" if self.inject_path else body\n"
    "meta.update({\n"
    "    \"doc_title\": doc_title,          # 文档标题\n"
    "    \"section_path\": section_path,    # 文件标题 > 章 > 节\n"
    "    \"start_line\": start_line,        # 在原文中的起始行，便于溯源\n"
    "    \"line_count\": len(chunk_lines),\n"
    "    \"char_count\": len(content),\n"
    "    \"chunk_type\": \"structured\",\n"
    "    \"chunk_index\": index,\n"
    "    \"total_chunks\": len(documents),\n"
    "})"
)
para("块的最终形态（真实输出）：")
code(
    "[Docker 常用命令速查表 > Docker 安装与下载]\n"
    "docker --version                       查看 Docker 版本\n"
    "docker compose version                 查看 Docker Compose 版本\n"
    "docker info                            查看 Docker 系统详细信息"
)

# ================= 五、接入 =================
h("五、接入本项目需要改动的文件", 1)
table(
    ["文件", "改动内容", "是否必须"],
    [
        ["app/rag/structured_splitter.py", "新增：结构感知分块器（本文实现）", "必须"],
        ["app/rag/processor.py", "保留作为散文/无结构文档的兜底策略", "建议"],
        ["api/routes.py 上传接口", "按文档类型选择分块器（约 5 行）", "必须"],
        ["services/reindex.py", "重索引时同样走策略分发（约 5 行）", "必须"],
        ["app/rag/retriever.py", "无需改动（仍写入同一 collection）", "不需要"],
        ["agents/ 与生成逻辑", "无需改动（仍消费 List[Document]）", "不需要"],
    ],
    widths=[5.4, 8.4, 2.4],
)
para(
    "关键点：检索与生成层完全无感。切分只改变「块的形状与元数据」，"
    "不改变 search() 的返回类型，所以整套链路的其他部分一行都不用动。",
    bold=True,
)

# ================= 六、切换 =================
h("六、更换切分策略要改哪里", 1)
table(
    ["你的目的", "改哪里", "改多少"],
    [
        ["换一种新策略（如句子窗口）", "新增一个类 + 在分发函数里注册一行", "约 80~120 行新代码"],
        ["调整块大小 / 重叠行数", "StructuredSplitter 构造参数或 settings", "1~3 行"],
        ["按文档类型自动选策略", "在文件类型识别处加映射（扩展名 / 内容特征）", "约 20 行"],
        ["按章节过滤检索", "search() 里加 where={\"section_path\": …}", "约 5 行"],
        ["回滚到旧的字符切分", "把分发处指向 DocumentProcessor 即可", "1 行"],
    ],
    widths=[5.6, 8.0, 3.0],
)
para(
    "设计上的取舍：把「分块」抽象成一个只有 split() 的接口，"
    "新增策略的成本就是实现这一个方法，其余链路零改动——这是可扩展性的实际体现。"
)

# ================= 七、实测 =================
h("七、实测验证结果", 1)
para("用 9 份真实技术笔记跑完整切分，结果如下：")
table(
    ["检查项", "结果", "判定"],
    [
        ["文档数", "9 份", "-"],
        ["生成块数", "99 块", "-"],
        ["被切断的行", "0", "通过"],
        ["残缺开头块（以 . 、 ) 开头）", "0", "通过"],
        ["块长 min / avg / max", "31 / 273 / 509 字符", "均在 max_chars=640 以内"],
        ["标题识别成功", "9 / 9", "通过"],
        ["结构化标记识别", "章 55 个、节 74 个", "全部命中"],
    ],
    widths=[6.6, 6.0, 2.8],
)
para(
    "以上指标全部由脚本产出：语料结构来自 scripts/analyze_corpus.py，"
    "切分质量来自 scripts/verify_split.py。命令与清单见下一节。",
    size=9,
)

# ================= 八、收益与风险 =================
h("八、收益、局限与风险", 1)
h("8.1 收益", 2)
bullet("切点落在自然边界：不再出现半截命令、半截编号。")
bullet("块自足：块首带标题路径，模型无需回看全文即可理解所属章节。")
bullet("可溯源：metadata 记录 start_line 与 section_path，可直接定位原文位置。")
bullet("可扩展：新增策略只需实现 split()，检索与生成层零改动。")

h("8.2 局限（必须承认）", 2)
bullet("只适配「有结构标记」的文档。纯散文、无小节标题的长文仍应回落到字符切分。")
bullet("中文章节序号只识别【…】形态；「第一部分：」这类写法目前识别不到，会并入正文。")
bullet("表格、代码块尚未做原子化处理——本次语料中没有，但一旦引入会退化为按行切。")

h("8.3 下一步", 2)
bullet("加中文分章识别（第一部分：/ 第1章 / 一、）。")
bullet("代码块与表格原子化：识别围栏与表格块，整块不切。")
bullet("建立评测集，用 MRR / nDCG 验证本次改动的实际检索收益，而不只看结构指标。")

# ================= 九、简历 =================
h("九、简历表述建议", 1)
para("本次改动可以用下面这段替换简历中「检索优化」那条的分块部分：")
code(
    "分块策略：实测 9 份真实技术笔记（898 行；行长中位数 29、最长 153、无一行超 512；"
    "章级 55 / 节级 74），据此自实现「结构感知分块」——"
    "章/节为父边界、行为原子单元、整行重叠替代字符重叠、块首注入标题路径；"
    "99 个分块中行切断与残缺开头均为 0，标题识别 9/9。"
)
para(
    "注：写进简历前需确认代码已合入主干（本次已新增 app/rag/structured_splitter.py 并通过实测）。",
    size=9,
)

# ================= 十、数据来源 =================
h("十、数据来源（可复现）", 1)
para(
    "文中所有统计数字都由下面两个脚本产出，不再手工统计。语料有增减时，"
    "只需更新清单 eval/tech_notes.txt 后重跑，数字自动更新。"
)

h("10.1 语料结构统计", 2)
code(
    "python scripts/analyze_corpus.py --dir \"E:\\桌面\" `\n"
    "    --list eval/tech_notes.txt --json eval/corpus_stats.json"
)
para("输出：文档数、总大小、非空行、行长 P50/P90/P99/最长、超限行数、三类结构标记计数。", size=9)

h("10.2 切分质量验证", 2)
code(
    "docker compose run --rm --no-deps `\n"
    "  -v \"<repo>\\backend:/app\" -v \"<repo>\\scripts:/scripts:ro\" `\n"
    "  -v \"<repo>\\eval:/eval:ro\" -v \"<语料目录>:/docs:ro\" `\n"
    "  backend python /scripts/verify_split.py --dir /docs --list /eval/tech_notes.txt"
)
para(
    "输出：每份文档的块数、平均/最长块长、被切断的行数、残缺开头块数、标题识别结果。",
    size=9,
)

doc.save(OUT)
print("saved:", OUT)
