import asyncio
import os
import time
from pathlib import Path
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, String, Integer, Boolean, Float, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session
from dotenv import load_dotenv
from core.auth import hash_value, authenticate, create_token, current_user
from core.monitor import metrics, process_snapshot
from core.bot_manager import BotManager
from core.file_manager import FileManager
from core.terminal import terminal_socket
from core.package_mgr import installed, run as package_run, search as package_search
from core.backup import BackupManager
from core.logs import LogManager
load_dotenv()
BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
engine = create_engine(f"sqlite:///{DATA / 'db.sqlite'}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__ = "users"; id: Mapped[int] = mapped_column(primary_key=True); username: Mapped[str] = mapped_column(String(80), unique=True); password_hash: Mapped[str] = mapped_column(String(255)); pin_hash: Mapped[str | None] = mapped_column(String(255), nullable=True); role: Mapped[str] = mapped_column(String(30), default="admin"); created_at: Mapped[float] = mapped_column(Float, default=time.time); last_login: Mapped[float | None] = mapped_column(Float, nullable=True)
class Bot(Base):
    __tablename__ = "bots"; id: Mapped[int] = mapped_column(primary_key=True); name: Mapped[str] = mapped_column(String(120), unique=True); path: Mapped[str] = mapped_column(String(500)); startup_cmd: Mapped[str] = mapped_column(String(500), default="python main.py"); status: Mapped[str] = mapped_column(String(30), default="stopped"); pid: Mapped[int | None] = mapped_column(Integer, nullable=True); auto_restart: Mapped[bool] = mapped_column(Boolean, default=False); created_at: Mapped[float] = mapped_column(Float, default=time.time); last_started: Mapped[float | None] = mapped_column(Float, nullable=True)
class LoginHistory(Base):
    __tablename__ = "login_history"; id: Mapped[int] = mapped_column(primary_key=True); user_id: Mapped[int | None] = mapped_column(Integer, nullable=True); ip_address: Mapped[str] = mapped_column(String(80)); status: Mapped[str] = mapped_column(String(30)); timestamp: Mapped[float] = mapped_column(Float, default=time.time); device_info: Mapped[str] = mapped_column(String(255), default="")
class Setting(Base):
    __tablename__ = "settings"; id: Mapped[int] = mapped_column(primary_key=True); key: Mapped[str] = mapped_column(String(100), unique=True); value: Mapped[str] = mapped_column(Text); updated_at: Mapped[float] = mapped_column(Float, default=time.time)
class Backup(Base):
    __tablename__ = "backups"; id: Mapped[int] = mapped_column(primary_key=True); filename: Mapped[str] = mapped_column(String(255)); size_bytes: Mapped[int] = mapped_column(Integer); created_at: Mapped[float] = mapped_column(Float, default=time.time); notes: Mapped[str] = mapped_column(String(255), default="")
Base.metadata.create_all(engine)

def db():
    session = SessionLocal()
    try: yield session
    finally: session.close()

templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="Shadow Host Panel")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
bots = BotManager(str(DATA / "bots")); files = FileManager(str(DATA / "bots")); backups = BackupManager(str(DATA)); logs = LogManager(str(DATA / "logs"))

def guard(request: Request, session: Session):
    user = current_user(request, session)
    if not user: raise HTTPException(401, "Authentication required")
    return user

def page(request, template, db_session, **context):
    if not current_user(request, db_session): return RedirectResponse("/login", 303)
    return templates.TemplateResponse(template, {"request": request, **context})

@app.on_event("startup")
async def startup():
    with SessionLocal() as session:
        if not session.scalar(select(User).where(User.username == "admin")):
            session.add(User(username="admin", password_hash=hash_value(os.getenv("MASTER_PASSWORD", "admin123"))))
            session.commit()
        bots.discover(session)
    asyncio.create_task(bots.watchdog(SessionLocal))

@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(db)): return RedirectResponse("/dashboard" if current_user(request, session) else "/login", 303)
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request): return templates.TemplateResponse("login.html", {"request": request})
@app.post("/api/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), pin: str = Form(""), session: Session = Depends(db)):
    user = authenticate(session, username, password, pin)
    session.add(LoginHistory(user_id=user.id if user else None, ip_address=request.client.host if request.client else "unknown", status="success" if user else "failed", device_info=request.headers.get("user-agent", "")[:255])); session.commit()
    if not user: return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    user.last_login = time.time(); session.commit(); response = JSONResponse({"ok": True, "redirect": "/dashboard"}); response.set_cookie("shadow_session", create_token(user.id), httponly=True, samesite="lax", max_age=1800); return response
@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True}); response.delete_cookie("shadow_session"); return response

PAGES = {"dashboard": "dashboard.html", "bots": "bots.html", "files": "files.html", "terminal": "terminal.html", "monitoring": "monitoring.html", "packages": "packages.html", "logs": "logs.html", "backup": "backup.html", "settings": "settings.html"}
for name, template in PAGES.items():
    def make_route(t):
        def route(request: Request, session: Session = Depends(db)): return page(request, t, session)
        return route
    app.add_api_route("/" + name, make_route(template), methods=["GET"], response_class=HTMLResponse)

@app.get("/api/stats")
def stats(session: Session = Depends(db), request: Request = None):
    guard(request, session); bots.sync(session); all_bots = session.scalars(select(Bot)).all(); m = metrics(); return {**m, "running": sum(b.status == "running" for b in all_bots), "stopped": sum(b.status != "running" for b in all_bots), "projects": len(all_bots), "bots": [{"id": b.id, "name": b.name, "status": b.status, "pid": b.pid, "uptime": int(time.time() - b.last_started) if b.last_started and b.status == "running" else 0} for b in all_bots]}
@app.get("/api/bots")
def list_bots(request: Request, session: Session = Depends(db)): guard(request, session); bots.sync(session); return [{"id": b.id, "name": b.name, "status": b.status, "pid": b.pid, "path": b.path, "startup_cmd": b.startup_cmd, "auto_restart": b.auto_restart} for b in session.scalars(select(Bot)).all()]
@app.post("/api/bots/{bot_id}/{action}")
def bot_action(bot_id: int, action: str, request: Request, session: Session = Depends(db)):
    guard(request, session)
    if action == "start": result = bots.start(session, bot_id)
    elif action == "stop": result = bots.stop(session, bot_id)
    elif action == "force": result = bots.stop(session, bot_id, True)
    elif action == "restart": result = bots.restart(session, bot_id)
    else: raise HTTPException(400, "Unknown action")
    return {"ok": bool(result)}
@app.get("/api/metrics")
def api_metrics(request: Request, session: Session = Depends(db)): guard(request, session); return {"metrics": metrics(), "processes": process_snapshot()}
@app.get("/api/files/{bot}")
def file_tree(bot: str, request: Request, session: Session = Depends(db)): guard(request, session); return files.tree(bot)
@app.get("/api/files/{bot}/{rel:path}")
def file_read(bot: str, rel: str, request: Request, session: Session = Depends(db)): guard(request, session); path = files.safe(bot, rel); return {"path": rel, "content": path.read_text(encoding="utf-8") if path.is_file() else ""}
@app.post("/api/files/{bot}/{rel:path}")
def file_write(bot: str, rel: str, request: Request, content: str = Form(""), session: Session = Depends(db)): guard(request, session); files.write(bot, rel, content); return {"ok": True}
@app.delete("/api/files/{bot}/{rel:path}")
def file_delete(bot: str, rel: str, request: Request, session: Session = Depends(db)): guard(request, session); files.delete(bot, rel); return {"ok": True}
@app.post("/api/files/{bot}/upload")
def file_upload(bot: str, request: Request, upload: UploadFile = File(...), session: Session = Depends(db)): guard(request, session); target = files.safe(bot, upload.filename); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(upload.file.read()); return {"ok": True}
@app.get("/api/packages")
def packages(request: Request, session: Session = Depends(db)): guard(request, session); return installed()
@app.post("/api/packages/{action}")
def packages_action(action: str, request: Request, package: str = Form(...), session: Session = Depends(db)): guard(request, session); return {"output": package_run(action, package)}
@app.get("/api/packages/search")
def packages_search(request: Request, q: str, session: Session = Depends(db)): guard(request, session); return package_search(q)
@app.get("/api/logs/{name}")
def read_logs(name: str, request: Request, session: Session = Depends(db)): guard(request, session); return {"lines": logs.read(name)}
@app.post("/api/backups")
def create_backup(request: Request, session: Session = Depends(db)): guard(request, session); path = backups.create(str(DATA / "db.sqlite")); session.add(Backup(filename=path.name, size_bytes=path.stat().st_size)); session.commit(); return {"filename": path.name}
@app.get("/api/backups")
def list_backups(request: Request, session: Session = Depends(db)): guard(request, session); return [{"filename": b.filename, "size": b.size_bytes, "created_at": b.created_at} for b in session.scalars(select(Backup).order_by(Backup.created_at.desc())).all()]
@app.get("/api/backups/{filename}")
def download_backup(filename: str, request: Request, session: Session = Depends(db)): guard(request, session); path = backups.dir / filename; return FileResponse(path, filename=filename)
@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket): await terminal_socket(websocket)
@app.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket):
    await websocket.accept()
    while True:
        await websocket.send_json({"metrics": metrics(), "processes": process_snapshot()}); await asyncio.sleep(2)
