
import hashlib, secrets, sqlite3, threading
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}
# 聚合锁：停用与水位刷新同时进行须占锁，后交只记停用聚合争用
AGG_LOCK = threading.Lock()
# 聚合切断择定甲路径：停用牌写入与水位剔除捆成同库提交，失败则整段撤销
CUTOFF_METHOD = "甲"

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agg_card(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          day TEXT NOT NULL,
          method TEXT NOT NULL,
          sealed INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agg_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS waterline(
          id INTEGER PRIMARY KEY CHECK (id=1),
          total INTEGER NOT NULL,
          writable INTEGER NOT NULL,
          degraded INTEGER NOT NULL,
          frozen INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
        if "disabled" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
        ensure_agg_card(conn)
        recompute_waterline(conn)

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def record_agg_event(conn, kind, detail):
    """聚合切断册：追加争用/偏离/封档等痕迹，只增不改。"""
    conn.execute("INSERT INTO agg_events(kind,detail,created_at) VALUES (?,?,?)",
                 (kind, detail, now_iso()))

def ensure_agg_card(conn):
    """聚合切断卡：择定甲路径后写入并封档，当日不许改口。"""
    day = datetime.now().date().isoformat()
    row = conn.execute("SELECT * FROM agg_card WHERE day=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
    if not row:
        conn.execute("INSERT INTO agg_card(day,method,sealed,created_at) VALUES (?,?,1,?)",
                     (day, CUTOFF_METHOD, now_iso()))
        record_agg_event(conn, "切断封档",
                         f"聚合切断卡封档：{CUTOFF_METHOD}路径（停用牌写入与水位剔除同库提交，失败整段撤销）")
        row = conn.execute("SELECT * FROM agg_card WHERE day=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
    return row

def recompute_waterline(conn):
    """水位聚合落库：只聚合未停用源，停用源不进绿灯。须与停用牌写入捆成同库提交。"""
    rows = conn.execute("SELECT output_mode, COUNT(*) c FROM sources WHERE disabled=0 GROUP BY output_mode").fetchall()
    counts = {r["output_mode"]: r["c"] for r in rows}
    conn.execute("""INSERT INTO waterline(id,total,writable,degraded,frozen,updated_at) VALUES (1,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET total=excluded.total, writable=excluded.writable,
                    degraded=excluded.degraded, frozen=excluded.frozen, updated_at=excluded.updated_at""",
                 (sum(counts.values()), counts.get("可写", 0), counts.get("降级只读", 0),
                  counts.get("冻结", 0), now_iso()))

def acquire_agg_lock(source, action):
    """停用与水位刷新同时进行须占聚合锁；占不到则后交只记停用聚合争用。"""
    if AGG_LOCK.acquire(blocking=False):
        return True
    with connect() as c:
        record_agg_event(c, "停用聚合争用", f"{action} {source} 未占聚合锁，后交只记停用聚合争用")
    return False

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(include_disabled: bool = False, authorization: str | None = Header(default=None)):
    require_user(authorization)
    # 源目录排除停用；史迹核查可用 include_disabled=1 取全量
    sql = "SELECT * FROM sources ORDER BY id" if include_disabled else \
          "SELECT * FROM sources WHERE disabled=0 ORDER BY id"
    with connect() as c:
        return {"items":[dict(r) for r in c.execute(sql).fetchall()]}

@app.get("/api/aggregate")
def aggregate(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        w = c.execute("SELECT * FROM waterline WHERE id=1").fetchone()
        disabled = c.execute("SELECT COUNT(*) c FROM sources WHERE disabled=1").fetchone()["c"]
    return {"total": w["total"], "writable": w["writable"], "degraded": w["degraded"],
            "frozen": w["frozen"], "disabled": disabled, "updated_at": w["updated_at"]}

class CutoffIn(BaseModel):
    method: str | None = None

def cutoff_guard(name, action, body):
    """占聚合锁、读切断卡、校验路径偏离。偏离则拒写并记聚合偏了。"""
    if not acquire_agg_lock(name, action):
        raise HTTPException(409, "停用聚合争用")
    try:
        with connect() as c:
            card = ensure_agg_card(c)
            src = c.execute("SELECT * FROM sources WHERE name=?", (name,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        requested = (body.method if body else None) or card["method"]
        if requested != card["method"]:
            with connect() as c2:
                record_agg_event(c2, "聚合偏了",
                                 f"{action} {name} 请求{requested}路径偏离切断卡{card['method']}路径，拒写")
            raise HTTPException(409, "聚合偏了")
        return card, src
    except Exception:
        AGG_LOCK.release()
        raise

@app.post("/api/sources/{name}/disable")
def disable_source(name: str, body: CutoffIn | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    card, src = cutoff_guard(name, "停用", body)
    try:
        if src["disabled"]:
            return {"ok": True, "disabled": True, "method": card["method"], "note": "已停用"}
        # 甲路径：停用牌写入与水位剔除捆成同库提交，失败则整段撤销；史迹只读不删
        with connect() as c:
            c.execute("UPDATE sources SET disabled=1 WHERE name=?", (name,))
            recompute_waterline(c)
            record_agg_event(c, "停用切断", f"{name} 停用牌写入与水位剔除同库提交（{card['method']}路径）")
        return {"ok": True, "disabled": True, "method": card["method"]}
    finally:
        AGG_LOCK.release()

@app.post("/api/sources/{name}/enable")
def enable_source(name: str, body: CutoffIn | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    card, src = cutoff_guard(name, "启用", body)
    try:
        if not src["disabled"]:
            return {"ok": True, "disabled": False, "method": card["method"], "note": "未停用"}
        with connect() as c:
            c.execute("UPDATE sources SET disabled=0 WHERE name=?", (name,))
            recompute_waterline(c)
            record_agg_event(c, "启用回挂", f"{name} 启用回挂与水位并入同库提交（{card['method']}路径）")
        return {"ok": True, "disabled": False, "method": card["method"]}
    finally:
        AGG_LOCK.release()

@app.get("/api/agg/card")
def agg_card(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return dict(ensure_agg_card(c))

@app.get("/api/agg/events")
def agg_events(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM agg_events ORDER BY id DESC").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    if not acquire_agg_lock(body.source, "水位刷新"):
        raise HTTPException(409, "停用聚合争用")
    try:
        with connect() as c:
            src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
            if not src: raise HTTPException(404, "未知源")
            if src["disabled"]: raise HTTPException(409, "源已停用，到达不入水位")
            # lag by minute rough parse
            def parse(t):
                return datetime.fromisoformat(t)
            lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
            if lag < 0: lag = 0
            lv = level_of(lag, src["sla_minutes"])
            mode = mode_of(lv)
            c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                      (body.source, body.source_clock, body.ingest_clock, lag, lv))
            c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
            recompute_waterline(c)
            return {"lag_minutes": lag, "level": lv, "output_mode": mode}
    finally:
        AGG_LOCK.release()

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    if not acquire_agg_lock(source, "补齐恢复"):
        raise HTTPException(409, "停用聚合争用")
    try:
        now = datetime.now().isoformat(timespec="seconds")
        with connect() as c:
            src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src: raise HTTPException(404, "未知源")
            if src["disabled"]: raise HTTPException(409, "源已停用，先启用再补齐")
            c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
            c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                      (source, "成功", "可写", "补齐后恢复可写", now))
            recompute_waterline(c)
            return {"ok": True, "output_mode": "可写"}
    finally:
        AGG_LOCK.release()

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
