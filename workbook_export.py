"""Export a filterable workbook from a verified GEO snapshot."""
from collections import defaultdict
from contextlib import closing
from datetime import date
from io import BytesIO
import json
import sqlite3

from archive import require, write_atomic
from content_metrics import reputation_label, named_prompt


def write_question_check(path, issues):
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    book = Workbook()
    sheet = book.active
    sheet.title = "题号待核对"
    sheet.append(("任务 ID","回答日期","平台","异常代码","Answerbit 题文","候选飞书题号","回查入口"))
    for task, _, platform, day, code, check_at, prompt, candidates, _ in issues:
        sheet.append([ILLEGAL_CHARACTERS_RE.sub("",value or "")
                      for value in (task,day,platform,code,prompt,candidates,check_at)])
        for cell in sheet[sheet.max_row]:
            cell.data_type = "s"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in zip("ABCDEFG",(25,15,18,25,55,35,32)):
        sheet.column_dimensions[column].width = width
    output = BytesIO()
    book.save(output)
    write_atomic(path,output.getvalue())


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
    if data.get('market_scope'):
        append(summary, ('运营国家', '、'.join(data['market_scope']['markets'])))
        append(summary, ('未归属异常', data['market_scope']['unassigned_issues']))
        append(summary, ('平台快照', data['market_scope']['platform_snapshot']))
    for item in (
        ("数据来源",batch["source"]),("窗口开始",date.fromisoformat(batch["begin_date"])),
        ("窗口结束",date.fromisoformat(batch["end_date"])),("最后成功取数（UTC）",batch["fetched_at_utc"]),
        ("接口记录数",batch["returned_records"]),("有效回答数",sum(r["n"] for r in data["rows"])),
        ("题库版本",batch["question_version"]),("指标版本",batch["metric_version"]),
        ("查找方法","按题号、市场、平台、日期、分组或标签筛选；多个标签可用文本包含条件。"),
        ("平台回查","在 Answerbit「大模型回答记录」按题号对应题文、日期及平台核对。"),
    ):
        append(summary,item)
    source = data.get("bank_source")
    if source:
        append(summary, ('题库来源', '本地导出，未核实在线最新版' if source['source']=='local-export' else '飞书在线题库'))
        for label, key in (("在线题库修订号","revision"),("题库读取时间（UTC）","read_at"),("有效题数","question_count")):
            if key in source:
                append(summary,(label,source[key]))
    core = data.get("core_mention")
    if core:
        append(summary,("提及率口径","未点名、扣除明确负面；分母保留全部未点名有效回答"))
        append(summary,("提及率分子",core["numerator"]))
        append(summary,("提及率分母",core["denominator"]))
        append(summary,("分子扣除负面",core["excluded_negative"]))
        append(summary,("提及率",core["value"]))
        summary.cell(summary.max_row,2).number_format = "0.00%"
        append(summary,("点名判定来源",data.get("prompt_basis")))
    comparison = data.get("comparison")
    if comparison and comparison["previous_batch"]:
        history = book.create_sheet("上次对比")
        append(history,("指标","数据来源","本次","上次","变化","单位"))
        baseline = comparison["previous_batch"]
        append(summary,("上次数据窗口",baseline["begin_date"]+" 至 "+baseline["end_date"]))
        append(summary,("上次成功取数（UTC）",baseline["fetched_at_utc"]))
        if comparison["limitations"]:
            append(summary,("上次对比限制","；".join(comparison["limitations"])))
        for row in comparison["metrics"]:
            unit = {"count":"条","percent":"百分点","rank":"名","score":"分"}[row["kind"]]
            append(history,(row["name"],row["source"],row["value"],row["previous"],row["change"],unit))
            if row["kind"]=="percent":
                for cell in list(history[history.max_row])[2:4]:
                    if isinstance(cell.value,(float,int)):
                        cell.number_format = '0.00"%"'
        style(history,(25,25,17,17,17,15))
    elif comparison:
        append(summary,("上次对比","首次运行或没有可读取的上次成功结果"))
    append(records, ("飞书题号","回答日期","市场","平台","Answerbit 分组","Answerbit 标签","任务 ID",
                     "样本状态","点名状态","推荐判断","平台口碑","品牌首次出现顺序",
                     "Kalodata 正文链接","Kalodata 正式来源","FastMoss 正文链接","FastMoss 正式来源","提及率分子","Answerbit 题文"))
    append(issues, ("飞书题号","题号匹配状态","Answerbit 题文","回答日期","平台","任务 ID","异常代码","回查入口"))
    with closing(sqlite3.connect(snapshot.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        require(dict(db.execute("SELECT * FROM batch").fetchone())==batch,"Excel 导出 / 数据库与看板批次不一致")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prompt_join = ("LEFT JOIN sample_question p USING(task_id)" if "sample_question" in tables else
                       "LEFT JOIN sample_answer p USING(task_id)" if "sample_answer" in tables else "")
        prompt_column = "p.prompt" if prompt_join else "NULL"
        excluded = db.execute("SELECT count(DISTINCT task_id) FROM issue WHERE coalesce(question_id,'')=''").fetchone()[0]
        append(summary, ("未确认题号（未纳入明细）",excluded))
        tags = defaultdict(list)
        for row in db.execute("SELECT task_id,tag_name FROM sample_tag ORDER BY tag_id"):
            tags[row["task_id"]].append(row["tag_name"])
        links = {(r["task_id"],r["brand"]):(r["body_hit"],r["formal_hit"])
                 for r in db.execute("SELECT task_id,brand,body_hit,formal_hit FROM sample_website")}
        for row in db.execute(f"""
            SELECT s.task_id,s.question_id,s.task_date,s.platform,s.status,q.market,q.named_prompt,
                   g.group_name,g.tags_known,c.recommendation,c.reputation_code,c.ranks,{prompt_column} AS actual_prompt
            FROM sample s JOIN question q USING(question_id) LEFT JOIN sample_group g USING(task_id)
            LEFT JOIN sample_content c USING(task_id) {prompt_join} ORDER BY s.task_date,s.question_id,s.platform,s.task_id
        """):
            task = row["task_id"]
            ranks = json.loads(row["ranks"] or "{}")
            flags = (*links.get((task,"Kalodata"),(None,None)),*links.get((task,"FastMoss"),(None,None)))
            valid = row["status"] == "valid"
            sentiment = reputation_label(row["reputation_code"])
            named = (named_prompt(row["actual_prompt"]) if row["actual_prompt"] else
                     data.get("question_named",{}).get(row["question_id"],row["named_prompt"]))
            mention_status = ("点名题排除" if named else "未提及" if "Kalodata" not in ranks else
                              "负面排除" if sentiment=="negative" else "计入")
            append(records, (row["question_id"],date.fromisoformat(row["task_date"]),data["markets"].get(row["market"],row["market"]),
                row["platform"],row["group_name"] or "分组未知","、".join(tags[task]) or ("无标签" if row["tags_known"] else "未知"),
                task,"有效" if valid else "待确认","点名" if named else "未点名",
                labels.get(reviewed.get(task,row["recommendation"])) if valid else None,
                sentiments[sentiment] if valid else None,
                " → ".join(sorted(ranks,key=ranks.get)),
                *({0:"未命中",1:"命中"}.get(flag,"未知") if valid else None for flag in flags),
                mention_status if valid else None,row["actual_prompt"]))
        issue_columns = {r["name"] for r in db.execute("PRAGMA table_info(issue)")}
        evidence_columns = ("question_text","match_basis")
        extra = ", ".join(name if name in issue_columns else f"NULL AS {name}" for name in evidence_columns)
        for row in db.execute(f"""
            SELECT question_id,task_date,platform,task_id,code,check_at,{extra}
            FROM issue WHERE coalesce(question_id,'')<>'' ORDER BY task_date,task_id,code
        """):
            status = f"已匹配（{row['match_basis']}）" if row["match_basis"] else "已匹配"
            append(issues,(row["question_id"],status,row["question_text"],
                           date.fromisoformat(row["task_date"]),row["platform"],row["task_id"],row["code"],row["check_at"]))
    style(summary,(26,82))
    style(records,(23,14,12,16,24,32,24,12,12,14,14,34,16,16,16,16,20,52),filters=True)
    style(issues,(23,27,48,14,16,24,29,36),filters=True)
    output = BytesIO()
    book.save(output)
    return output.getvalue()
