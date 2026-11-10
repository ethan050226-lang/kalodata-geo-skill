"""Export a filterable workbook from a verified GEO snapshot."""
from collections import defaultdict
from contextlib import closing
from datetime import date
from io import BytesIO
import json
import sqlite3

from archive import require
from content_metrics import reputation_label


def export_workbook(snapshot, data):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    labels = {"explicit":"明确推荐","conditional":"条件推荐","neutral":"中立推荐",
              "negative":"未推荐","no_recommendation":"未推荐","unknown":"未知"}
    sentiments = {"positive":"正面","negative":"负面","mixed":"褒贬并存","neutral":"中性",
                  "not_mentioned":"未提及","unknown":"未知"}
    reviewed = {r["task"]:r["label"] for r in (data["review"] or {}).get("rows",[]) if r["status"] == "ok"}
    book = Workbook()
    summary = book.active
    summary.title = "批次说明"
    records = book.create_sheet("回答记录")
    issues = book.create_sheet("数据异常")

    def append(sheet, values):
        sheet.append([ILLEGAL_CHARACTERS_RE.sub("",v) if isinstance(v,str) else v for v in values])
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            elif isinstance(cell.value, date):
                cell.number_format = "yyyy-mm-dd"

    def style(sheet, widths, filters=False):
        sheet.freeze_panes = "B2"
        for cell in sheet[1]:
            cell.fill = PatternFill("solid",fgColor="173660")
            cell.font = Font(color="FFFFFF",bold=True)
            cell.alignment = Alignment(wrap_text=True,vertical="center")
        sheet.row_dimensions[1].height = 34
        for i, width in enumerate(widths,1):
            sheet.column_dimensions[get_column_letter(i)].width = width
        if filters:
            sheet.auto_filter.ref = sheet.dimensions

    append(summary, ("项目","内容"))
    batch = data["batch"]
    for item in (
        ("数据来源",batch["source"]),("窗口开始",date.fromisoformat(batch["begin_date"])),
        ("窗口结束",date.fromisoformat(batch["end_date"])),("最后成功取数（UTC）",batch["fetched_at_utc"]),
        ("接口记录数",batch["returned_records"]),("有效回答数",sum(r["n"] for r in data["rows"])),
        ("题库版本",batch["question_version"]),("指标版本",batch["metric_version"]),
        ("查找方法","按题号、市场、平台、日期、分组或标签筛选；多个标签可用文本包含条件。"),
        ("平台回查","在 Answerbit「大模型回答记录」按题号对应题文、日期及平台核对。"),
    ):
        append(summary,item)
    append(records, ("飞书题号","回答日期","市场","平台","Answerbit 分组","Answerbit 标签","任务 ID",
                     "样本状态","点名状态","推荐判断","平台口碑","品牌首次出现顺序",
                     "Kalodata 正文链接","Kalodata 正式来源","FastMoss 正文链接","FastMoss 正式来源"))
    append(issues, ("飞书题号","回答日期","平台","任务 ID","异常代码","回查入口"))
    with closing(sqlite3.connect(snapshot.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        require(dict(db.execute("SELECT * FROM batch").fetchone())==batch,"Excel 导出 / 数据库与看板批次不一致")
        tags = defaultdict(list)
        for row in db.execute("SELECT task_id,tag_name FROM sample_tag ORDER BY tag_id"):
            tags[row["task_id"]].append(row["tag_name"])
        links = {(r["task_id"],r["brand"]):(r["body_hit"],r["formal_hit"])
                 for r in db.execute("SELECT task_id,brand,body_hit,formal_hit FROM sample_website")}
        for row in db.execute("""
            SELECT s.task_id,s.question_id,s.task_date,s.platform,s.status,q.market,q.named_prompt,
                   g.group_name,g.tags_known,c.recommendation,c.reputation_code,c.ranks
            FROM sample s JOIN question q USING(question_id) LEFT JOIN sample_group g USING(task_id)
            LEFT JOIN sample_content c USING(task_id) ORDER BY s.task_date,s.question_id,s.platform,s.task_id
        """):
            task = row["task_id"]
            ranks = json.loads(row["ranks"] or "{}")
            flags = (*links.get((task,"Kalodata"),(None,None)),*links.get((task,"FastMoss"),(None,None)))
            valid = row["status"] == "valid"
            append(records, (row["question_id"],date.fromisoformat(row["task_date"]),data["markets"].get(row["market"],row["market"]),
                row["platform"],row["group_name"] or "分组未知","、".join(tags[task]) or ("无标签" if row["tags_known"] else "未知"),
                task,"有效" if valid else "待确认","点名" if row["named_prompt"] else "未点名",
                labels.get(reviewed.get(task,row["recommendation"])) if valid else None,
                sentiments[reputation_label(row["reputation_code"])] if valid else None,
                " → ".join(sorted(ranks,key=ranks.get)),
                *({0:"未命中",1:"命中"}.get(flag,"未知") if valid else None for flag in flags)))
        for row in db.execute("SELECT question_id,task_date,platform,task_id,code,check_at FROM issue ORDER BY task_date,task_id"):
            append(issues,(row[0],date.fromisoformat(row[1]),*row[2:]))
    style(summary,(26,82))
    style(records,(23,14,12,16,24,32,24,12,12,14,14,34,16,16,16,16),filters=True)
    style(issues,(23,14,16,24,29,36),filters=True)
    output = BytesIO()
    book.save(output)
    return output.getvalue()
