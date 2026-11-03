import os
import re
import json
import time
import secrets
import sqlite3
import threading
from flask import Flask, send_from_directory, request, jsonify
from flask_sock import Sock
from werkzeug.security import generate_password_hash, check_password_hash

import game

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 20}   # mantem o WebSocket vivo no Render
sock = Sock(app)

# ---------- conta do criador ----------
# A senha NAO fica mais no codigo (o repositorio do Render pode ser lido por outras pessoas).
# Defina CREATOR_PASS nas variaveis de ambiente do Render. Sem ela, a conta do criador nao e criada.
CREATOR_NICK = os.environ.get("CREATOR_NICK", "OswaldinDEV")
CREATOR_PASS = os.environ.get("CREATOR_PASS", "")
CREATOR_DISPLAY = "Criador Oswaldo"

MAX_LEVEL = 150
MAX_XP = 100 * (MAX_LEVEL - 1) * MAX_LEVEL // 2   # xp acumulado para chegar no nivel 150
CREATOR_IDX = 7            # skin exclusiva do criador (personagem e arma)
N_ALL = 8                  # indices 0..7
ONLINE_WINDOW = 45         # segundos sem ping = offline
RESERVED = {"oswaldo", "criador", "criadoroswaldo", "admin", "adm", "moderador", "oswaldindev"}
NICK_RE = re.compile(r"^[A-Za-z0-9_.-]{3,14}$")
CL_RE = re.compile(r"^[fp]\d{1,2}$")

# ---------- banco: Postgres (Render) ou SQLite (local) ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 25, DATABASE_URL)
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)

    def _pg_conn():
        # bancos hospedados (Neon, Supabase...) derrubam conexoes ociosas: testa antes de usar
        for _ in range(4):
            raw = _pool.getconn()
            try:
                if raw.closed:
                    raise psycopg2.OperationalError("fechada")
                cur = raw.cursor()
                cur.execute("SELECT 1")
                cur.close()
                raw.rollback()
                return raw
            except Exception:
                _pool.putconn(raw, close=True)
        raise RuntimeError("Sem conexao com o banco de dados.")
else:
    DB_PATH = os.path.join(BASE, "royale_senai.db")
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


class DB:
    def __init__(self):
        if USE_PG:
            self.raw = _pg_conn()
        else:
            self.raw = sqlite3.connect(DB_PATH, timeout=10)
            self.raw.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_PG:
            cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def close(self):
        if USE_PG:
            try:
                self.raw.rollback()
            except Exception:
                pass
            _pool.putconn(self.raw)
        else:
            self.raw.close()


def db():
    return DB()


# ---------- saves ----------
def clamp_int(v, lo, hi, d=0):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return d
    return max(lo, min(hi, v))


def own_list(v, n):
    out = {0}
    if isinstance(v, list):
        for i in v:
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n:
                out.add(i)
    return sorted(out)


def clean_cl(v):
    if not isinstance(v, dict):
        return {}
    return {k: 1 for k in v if isinstance(k, str) and CL_RE.match(k)}


STAT_KEYS = ("k", "ws", "wd", "gs", "gd")   # abates, vitorias solo/dupla, partidas solo/dupla


def clean_stats(v):
    if not isinstance(v, dict):
        v = {}
    return {k: clamp_int(v.get(k), 0, 10 ** 9) for k in STAT_KEYS}


def creator_save(s):
    ec = clamp_int(s.get("ec"), 0, N_ALL - 1, CREATOR_IDX)
    ew = clamp_int(s.get("ew"), 0, N_ALL - 1, CREATOR_IDX)
    return {
        "xp": MAX_XP,
        "coins": 999999999,
        "cown": list(range(N_ALL)),
        "wown": list(range(N_ALL)),
        "ec": ec,
        "ew": ew,
        "pass": 1,
        "cl": clean_cl(s.get("cl")),
        "mute": 1 if s.get("mute") else 0,
        "stats": clean_stats(s.get("stats")),
    }


def clean_save(s, is_creator):
    if not isinstance(s, dict):
        s = {}
    if is_creator:
        return creator_save(s)
    cown = own_list(s.get("cown"), CREATOR_IDX)   # 0..6 (7 e so do criador)
    wown = own_list(s.get("wown"), CREATOR_IDX)
    ec = clamp_int(s.get("ec"), 0, CREATOR_IDX - 1)
    ew = clamp_int(s.get("ew"), 0, CREATOR_IDX - 1)
    return {
        "xp": clamp_int(s.get("xp"), 0, MAX_XP),
        "coins": clamp_int(s.get("coins"), 0, 10 ** 9),
        "cown": cown,
        "wown": wown,
        "ec": ec if ec in cown else 0,
        "ew": ew if ew in wown else 0,
        "pass": 1 if s.get("pass") else 0,
        "cl": clean_cl(s.get("cl")),
        "mute": 1 if s.get("mute") else 0,
        "stats": clean_stats(s.get("stats")),
    }


def is_creator_row(row):
    return row["nick"].lower() == CREATOR_NICK.lower()


def row_save(row):
    try:
        raw = json.loads(row["save"])
    except ValueError:
        raw = {}
    return clean_save(raw, is_creator_row(row))


def payload(row, token):
    c = is_creator_row(row)
    return {
        "token": token,
        "nick": row["nick"],
        "display": CREATOR_DISPLAY if c else row["nick"],
        "creator": c,
        "save": row_save(row),
    }


# ---------- banco: tabelas ----------
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nick TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw TEXT NOT NULL,
  save TEXT NOT NULL,
  created INTEGER NOT NULL,
  last_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_seen ON users(last_seen);
"""
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users(
  id SERIAL PRIMARY KEY,
  nick TEXT NOT NULL,
  pw TEXT NOT NULL,
  save TEXT NOT NULL,
  created BIGINT NOT NULL,
  last_seen BIGINT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_nick_lower ON users(LOWER(nick));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  created BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_seen ON users(last_seen);
"""


def insert_user(con, nick, pw_hash, save_json, created, last_seen=0):
    sql = "INSERT INTO users(nick, pw, save, created, last_seen) VALUES(?,?,?,?,?)"
    if USE_PG:
        cur = con.execute(sql + " RETURNING id", (nick, pw_hash, save_json, created, last_seen))
        return cur.fetchone()["id"]
    return con.execute(sql, (nick, pw_hash, save_json, created, last_seen)).lastrowid


def init_db():
    con = db()
    try:
        if USE_PG:
            con.execute(SCHEMA_PG)
        else:
            con.raw.executescript(SCHEMA_SQLITE)
        if CREATOR_PASS:
            row = con.execute("SELECT id, pw FROM users WHERE LOWER(nick)=LOWER(?)", (CREATOR_NICK,)).fetchone()
            h = lambda: generate_password_hash(CREATOR_PASS, method="pbkdf2:sha256")
            if row is None:
                insert_user(con, CREATOR_NICK, h(), json.dumps(creator_save({})), int(time.time()))
            elif not check_password_hash(row["pw"], CREATOR_PASS):
                con.execute("UPDATE users SET pw=? WHERE id=?", (h(), row["id"]))
        else:
            print("AVISO: CREATOR_PASS nao definida - conta do criador desativada.", flush=True)
        con.commit()
    finally:
        con.close()


init_db()
game.start_loop()


# ---------- helpers ----------
def err(msg, code=400):
    return jsonify({"error": msg}), code


def new_session(con, uid):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    con.execute("DELETE FROM sessions WHERE created<?", (now - 60 * 86400,))
    con.execute("INSERT INTO sessions(token, uid, created) VALUES(?,?,?)", (token, uid, now))
    return token


def user_from_token(con, t=None):
    if t is None:
        t = request.headers.get("X-Token", "")
    if not t:
        return None
    return con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.uid WHERE s.token=?", (t,)
    ).fetchone()


def online_count(con):
    return con.execute(
        "SELECT COUNT(*) AS c FROM users WHERE last_seen>?", (int(time.time()) - ONLINE_WINDOW,)
    ).fetchone()["c"]


_fails = {}


def too_many_fails():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    lst = [t for t in _fails.get(ip, []) if now - t < 60]
    _fails[ip] = lst
    return ip, len(lst) >= 10


# ---------- rotas ----------
@app.route("/")
def home():
    r = send_from_directory(os.path.join(BASE, "static"), "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/api/stats")
def stats():
    return jsonify(game.stats())


@app.after_request
def no_store(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(Exception)
def on_error(e):
    from werkzeug.exceptions import HTTPException
    if request.path.startswith("/api/"):
        if isinstance(e, HTTPException):
            return err(e.description or "Erro", e.code or 500)
        return err("Erro no servidor: " + str(e)[:120], 500)
    raise e


@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    nick = str(d.get("nick", "")).strip()
    pw = str(d.get("password", ""))
    if not NICK_RE.match(nick):
        return err("Nick: 3 a 14 caracteres (letras, números, _ . -), sem espaços.")
    if nick.lower() in RESERVED:
        return err("Esse nick não pode ser usado.")
    if len(pw) < 4 or len(pw) > 64:
        return err("A senha precisa ter de 4 a 64 caracteres.")
    con = db()
    try:
        try:
            uid = insert_user(con, nick, generate_password_hash(pw, method="pbkdf2:sha256"),
                              json.dumps(clean_save({}, False)), int(time.time()), int(time.time()))
        except INTEGRITY_ERRORS:
            return err("Esse nick já está em uso. Escolha outro.", 409)
        token = new_session(con, uid)
        con.commit()
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return jsonify(payload(row, token))
    finally:
        con.close()


@app.route("/api/login", methods=["POST"])
def login():
    ip, blocked = too_many_fails()
    if blocked:
        return err("Muitas tentativas. Espere 1 minuto.", 429)
    d = request.get_json(silent=True) or {}
    nick = str(d.get("nick", "")).strip()
    pw = str(d.get("password", ""))
    con = db()
    try:
        row = con.execute("SELECT * FROM users WHERE LOWER(nick)=LOWER(?)", (nick,)).fetchone()
        if row is None or not check_password_hash(row["pw"], pw):
            _fails.setdefault(ip, []).append(time.time())
            return err("Nick ou senha incorretos.", 401)
        token = new_session(con, row["id"])
        con.execute("UPDATE users SET last_seen=? WHERE id=?", (int(time.time()), row["id"]))
        con.commit()
        return jsonify(payload(row, token))
    finally:
        con.close()


@app.route("/api/me")
def me():
    con = db()
    try:
        row = user_from_token(con)
        if row is None:
            return err("Sessão expirada.", 401)
        con.execute("UPDATE users SET last_seen=? WHERE id=?", (int(time.time()), row["id"]))
        con.commit()
        return jsonify(payload(row, request.headers.get("X-Token")))
    finally:
        con.close()


@app.route("/api/save", methods=["POST"])
def save():
    d = request.get_json(silent=True) or {}
    con = db()
    try:
        row = user_from_token(con)
        if row is None:
            return err("Sessão expirada.", 401)
        s = clean_save(d.get("save"), is_creator_row(row))
        con.execute("UPDATE users SET save=? WHERE id=?", (json.dumps(s), row["id"]))
        con.commit()
        return jsonify({"ok": True})
    finally:
        con.close()


@app.route("/api/ping", methods=["POST"])
def ping():
    con = db()
    try:
        row = user_from_token(con)
        if row is None:
            return err("Sessão expirada.", 401)
        con.execute("UPDATE users SET last_seen=? WHERE id=?", (int(time.time()), row["id"]))
        con.commit()
        return jsonify({"online": online_count(con)})
    finally:
        con.close()


@app.route("/api/online")
def online():
    con = db()
    try:
        return jsonify({"online": online_count(con)})
    finally:
        con.close()


@app.route("/api/logout", methods=["POST"])
def logout():
    t = request.headers.get("X-Token", "")
    con = db()
    try:
        con.execute("UPDATE users SET last_seen=0 WHERE id IN (SELECT uid FROM sessions WHERE token=?)", (t,))
        con.execute("DELETE FROM sessions WHERE token=?", (t,))
        con.commit()
        return jsonify({"ok": True})
    finally:
        con.close()


# ---------- multiplayer (WebSocket) ----------
@sock.route("/ws")
def ws_route(ws):
    con = db()
    try:
        row = user_from_token(con, request.args.get("token", ""))
        if row is not None:
            sv = row_save(row)
            creator = is_creator_row(row)
            nick = CREATOR_DISPLAY if creator else row["nick"]
    finally:
        con.close()
    if row is None:
        ws.send(json.dumps({"t": "error", "e": "Sessão expirada. Entre de novo."}))
        return
    client = game.Client(ws, nick, creator, sv["cown"])
    try:
        while not client.closed:
            data = ws.receive(timeout=30)
            if data is not None:
                game.handle(client, data)
    except Exception:
        pass
    finally:
        game.leave(client)
        client.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
