"""Serve one local dashboard and its matched Answerbit originals."""
import argparse
from contextlib import closing
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
import sqlite3
from urllib.parse import parse_qs, urlsplit

from dashboard import retained_data


def make_server(snapshot, output):
    _, data, _ = retained_data(output)
    if data.get("recommendations") is None:
        raise ValueError("该看板没有原文索引，请先重新取数生成看板。")
    dashboard = output.read_bytes()
    db_uri = snapshot.resolve().as_uri() + "?mode=ro"
    route = "/" + token_urlsafe(24) + "/"
    indexed = {r["task"]:r["q"] for r in data["recommendations"]}
    indexed.update({r['task']:r['q'] for r in (data.get('quality') or {}).get('records',[])})
    indexed.update({r['reference_task']:r['q'] for r in (data.get('quality') or {}).get('records',[]) if r['reference_task']})

    def check_batch(db):
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        batch = db.execute("SELECT * FROM batch").fetchone()
        if batch is None or dict(batch) != data["batch"]:
            raise ValueError("本地数据库与看板批次不同，请重新打开最新看板。")

    with closing(sqlite3.connect(db_uri, uri=True)) as db:
        check_batch(db)
        db.execute("SELECT task_id, prompt, body FROM sample_answer LIMIT 0")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def message(self, status, message):
            self.reply(status, message.encode(), "text/plain; charset=utf-8")

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.message(404, "页面不存在。")
                return
            url = urlsplit(self.path)
            if url.path == route and not url.query:
                self.reply(200, dashboard, "text/html; charset=utf-8")
                return
            task = parse_qs(url.query).get("task", [""])[0]
            if url.path != route + "answer" or task not in indexed:
                self.message(404, "该回答未在当前看板中列出。")
                return
            try:
                with closing(sqlite3.connect(db_uri, uri=True)) as db:
                    check_batch(db)
                    row = db.execute("""
                        SELECT a.prompt,a.body,s.question_id,s.task_date,s.platform,q.market,g.group_name
                        FROM sample_answer a JOIN sample s USING(task_id) JOIN question q USING(question_id)
                        LEFT JOIN sample_group g USING(task_id)
                        WHERE a.task_id=? AND s.question_id=?
                    """, (task, indexed[task])).fetchone()
            except (ValueError, sqlite3.Error):
                self.message(409, "本地原文库已变更或无法读取，请重新打开最新看板。")
                return
            if row is None:
                self.message(404, "该任务在本地库中没有可核实的原文。")
                return
            meta = " · ".join([row["market"], row["platform"], row["task_date"], row["group_name"] or "分组未知"])
            page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
                <title>{escape(row["question_id"])} · 本地回答原文</title>
                <style>body{{font:16px/1.7 system-ui,sans-serif;color:#152849;background:#f2f6fd;margin:0}}
                main{{box-sizing:border-box;max-width:960px;margin:auto;padding:clamp(16px,4vw,44px)}}
                pre{{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px;border-radius:12px}}
                h1,h2{{overflow-wrap:anywhere}} p{{color:#465c7b}} a{{color:#1f4b99}}</style></head>
                <body><main><a href="./#answers">返回看板</a><h1>{escape(row["question_id"])}</h1>
                <p>{escape(meta)} · Answerbit 任务 {escape(task)}</p><h2>提问</h2><pre>{escape(row["prompt"])}</pre>
                <h2>Answerbit 回答原文</h2><pre>{escape(row["body"])}</pre></main></body></html>"""
            self.reply(200, page.encode(), "text/html; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, f"http://127.0.0.1:{server.server_port}{route}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("dashboard", type=Path)
    args = parser.parse_args()
    try:
        server, url = make_server(args.snapshot, args.dashboard)
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        parser.exit(1, f"本地原文查询无法启动，请确认数据库和看板来自同一次成功刷新。{error if isinstance(error, ValueError) else ''}\n")
    print(url, flush=True)
    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
