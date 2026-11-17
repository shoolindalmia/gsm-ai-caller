"""Small control panel: create campaigns from CSV, start/pause, view results & transcripts."""
import csv
import html
import io
import json
import secrets

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .prompts import DEFAULT_SCRIPT

CSS = """body{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
textarea,input[type=text]{width:100%;box-sizing:border-box}textarea{height:180px}button{padding:6px 14px;margin-right:6px}
.tag{padding:2px 8px;border-radius:10px;background:#eee}.interested{background:#c8f7c5}.dnc,.not_interested{background:#f7c5c5}
.ai{color:#0645ad}.caller{color:#222}"""


def page(title, body):
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<title>{html.escape(title)}</title><style>{CSS}</style><h2>{html.escape(title)}</h2>{body}")


def create_app(cfg, db) -> FastAPI:
    app = FastAPI()
    security = HTTPBasic()
    w = cfg["web"]

    def auth(c: HTTPBasicCredentials = Depends(security)):
        ok = secrets.compare_digest(c.username, w["username"]) and secrets.compare_digest(c.password, w["password"])
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    @app.get("/", dependencies=[Depends(auth)])
    def index():
        rows = "".join(
            f"<tr><td><a href='/c/{c['id']}'>{html.escape(c['name'])}</a></td><td>{c['status']}</td>"
            f"<td>{c['done']}/{c['total']}</td><td>{c['interested']}</td>"
            f"<td><form method=post action='/c/{c['id']}/{'pause' if c['status'] == 'running' else 'start'}'>"
            f"<button>{'Pause' if c['status'] == 'running' else 'Start'}</button></form></td></tr>"
            for c in db.campaigns())
        return page("GSM AI Caller", f"""
<table><tr><th>Campaign</th><th>Status</th><th>Done</th><th>Interested</th><th></th></tr>{rows}</table>
<h3>New campaign</h3>
<form method=post action=/c enctype=multipart/form-data>
<p>Name<br><input type=text name=name required></p>
<p>Call script (use {{name}} or any CSV column like {{city}})<br><textarea name=script>{html.escape(DEFAULT_SCRIPT)}</textarea></p>
<p>Contacts CSV (columns: phone, name, any extra)<br><input type=file name=file accept=.csv required></p>
<button>Create (paused)</button></form>""")

    @app.post("/c", dependencies=[Depends(auth)])
    async def create(name: str = Form(...), script: str = Form(...), file: UploadFile = File(...)):
        text = (await file.read()).decode("utf-8-sig", errors="replace")
        cid = db.create_campaign(name, script)
        db.add_contacts(cid, list(csv.DictReader(io.StringIO(text))))
        return RedirectResponse(f"/c/{cid}", 303)

    @app.post("/c/{cid}/{action}", dependencies=[Depends(auth)])
    def control(cid: int, action: str):
        if action not in ("start", "pause"):
            raise HTTPException(400)
        db.set_campaign_status(cid, "running" if action == "start" else "paused")
        return RedirectResponse("/", 303)

    @app.get("/c/{cid}", dependencies=[Depends(auth)])
    def campaign(cid: int):
        cp = db.campaign(cid) or {}
        def row(c):
            outcome = html.escape(c["outcome"] or "")
            link = f"<a href='/call/{c['last_call_uuid']}'>transcript</a>" if c["last_call_uuid"] else ""
            return (f"<tr><td>{html.escape(c['name'] or '')}</td><td>{c['phone']}</td><td>{c['status']}</td>"
                    f"<td><span class='tag {outcome.split(':')[0]}'>{outcome}</span></td>"
                    f"<td>{c['attempts']}</td><td>{html.escape(c['summary'] or '')}</td><td>{link}</td></tr>")
        rows = "".join(row(c) for c in db.contacts(cid))
        return page(cp.get("name", "Campaign"), f"""<p><a href=/>← back</a> · <a href=/c/{cid}/export.csv>Export CSV</a></p>
<table><tr><th>Name</th><th>Phone</th><th>Status</th><th>Outcome</th><th>Tries</th><th>Summary</th><th></th></tr>{rows}</table>""")

    @app.get("/c/{cid}/export.csv", dependencies=[Depends(auth)])
    def export(cid: int):
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["phone", "name", "status", "outcome", "attempts", "summary", "data"])
        for c in db.contacts(cid):
            wr.writerow([c["phone"], c["name"], c["status"], c["outcome"], c["attempts"], c["summary"], c["data"]])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename=campaign_{cid}.csv"})

    @app.get("/call/{call_uuid}", dependencies=[Depends(auth)])
    def call(call_uuid: str):
        c = db.call(call_uuid)
        if not c:
            raise HTTPException(404)
        lines = "".join(f"<p class={t['role']}><b>{t['role'].upper()}:</b> {html.escape(t['text'])}</p>"
                        for t in json.loads(c["transcript"] or "[]"))
        return page(f"Call {call_uuid[:8]} — {c['result'] or ''}", f"<p><a href='javascript:history.back()'>← back</a></p>{lines}")

    return app
