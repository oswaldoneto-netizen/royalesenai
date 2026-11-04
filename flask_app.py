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
N_CH = game.N_SKINS        # personagens: indices 0..N_CH-1 (7 = so do criador)
N_WP = 13                  # skins de arma
PANDA_CH = 13              # personagem Panda (so quem comprou o Pacote Panda via Pix)
PANDA_WP = 12              # skin de arma Panda (idem)

# ---------- Pix (Mercado Pago) ----------
# Defina no Render (Environment): MP_ACCESS_TOKEN = token de PRODUCAO da sua conta Mercado Pago.
# PUBLIC_URL (opcional) = endereco do jogo, ex.: https://royalesenai.onrender.com  (habilita o aviso automatico do pagamento)
MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
PANDA_PRICE = 5.00
N_EM = game.N_EMOTES       # emotes de danca (0 = gratis, todo mundo tem)
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


def own_list(v, n, banned=()):
    out = {0}
    if isinstance(v, list):
        for i in v:
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n and i != CREATOR_IDX and i not in banned:
                out.add(i)
    return sorted(out)


def own_emotes(v):
    out = {0}
    if isinstance(v, list):
        for i in v:
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < N_EM:
                out.add(i)
    return sorted(out)


def clean_cl(v):
    if not isinstance(v, dict):
        return {}
    return {k: 1 for k in v if isinstance(k, str) and CL_RE.match(k)}


STAT_KEYS = ("k", "ws", "wd", "wq", "gs", "gd", "gq")   # abates, vitorias e partidas: solo/dupla/esquadrao


def clean_stats(v):
    if not isinstance(v, dict):
        v = {}
    return {k: clamp_int(v.get(k), 0, 10 ** 9) for k in STAT_KEYS}


def creator_save(s):
    ec = clamp_int(s.get("ec"), 0, N_CH - 1, CREATOR_IDX)
    ew = clamp_int(s.get("ew"), 0, N_WP - 1, CREATOR_IDX)
    return {
        "xp": MAX_XP,
        "px": 10 ** 6,
        "coins": 999999999,
        "cown": list(range(N_CH)),
        "wown": list(range(N_WP)),
        "eown": list(range(N_EM)),
        "ee": clamp_int(s.get("ee"), 0, N_EM - 1),
        "ec": ec,
        "ew": ew,
        "pass": 1,
        "cl": clean_cl(s.get("cl")),
        "mute": 1 if s.get("mute") else 0,
        "stats": clean_stats(s.get("stats")),
    }


def clean_save(s, is_creator, panda=False):
    if not isinstance(s, dict):
        s = {}
    if is_creator:
        return creator_save(s)
    # o indice 7 (criador) nunca entra; o Panda so entra se o servidor registrou o pagamento
    cown = own_list(s.get("cown"), N_CH, () if panda else (PANDA_CH,))
    wown = own_list(s.get("wown"), N_WP, () if panda else (PANDA_WP,))
    if panda:
        if PANDA_CH not in cown:
            cown = sorted(cown + [PANDA_CH])
        if PANDA_WP not in wown:
            wown = sorted(wown + [PANDA_WP])
    eown = own_emotes(s.get("eown"))
    ee = clamp_int(s.get("ee"), 0, N_EM - 1)
    ec = clamp_int(s.get("ec"), 0, N_CH - 1)
    ew = clamp_int(s.get("ew"), 0, N_WP - 1)
    return {
        "xp": clamp_int(s.get("xp"), 0, MAX_XP),
        "px": clamp_int(s.get("px"), 0, 10 ** 7),
        "coins": clamp_int(s.get("coins"), 0, 10 ** 9),
        "cown": cown,
        "wown": wown,
        "eown": eown,
        "ee": ee if ee in eown else 0,
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
    try:
        panda = bool(row["panda"])
    except (KeyError, IndexError):
        panda = False
    return clean_save(raw, is_creator_row(row), panda)


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
CREATE TABLE IF NOT EXISTS records(
  nick TEXT PRIMARY KEY COLLATE NOCASE,
  kills INTEGER NOT NULL,
  ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pix(
  id TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  status TEXT NOT NULL,
  created INTEGER NOT NULL
);
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
CREATE TABLE IF NOT EXISTS records(
  nick TEXT NOT NULL,
  kills INTEGER NOT NULL,
  ts BIGINT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_records_nick ON records(LOWER(nick));
CREATE TABLE IF NOT EXISTS pix(
  id TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  status TEXT NOT NULL,
  created BIGINT NOT NULL
);
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
        # coluna "panda" (1 = comprou o Pacote Panda). Bancos antigos recebem a coluna aqui.
        con.commit()
        try:
            con.execute("ALTER TABLE users ADD COLUMN " + ("IF NOT EXISTS " if USE_PG else "")
                        + "panda INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except Exception:      # SQLite: a coluna ja existe
            pass
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


def record_kills(nick, kills):
    """Guarda a melhor partida (mais abates) de cada jogador."""
    con = db()
    try:
        row = con.execute("SELECT kills FROM records WHERE LOWER(nick)=LOWER(?)", (nick,)).fetchone()
        if row is None:
            con.execute("INSERT INTO records(nick, kills, ts) VALUES(?,?,?)", (nick, kills, int(time.time())))
        elif kills > row["kills"]:
            con.execute("UPDATE records SET kills=?, ts=? WHERE LOWER(nick)=LOWER(?)",
                        (kills, int(time.time()), nick))
        else:
            return
        con.commit()
    except Exception as e:
        print("erro ao salvar recorde:", repr(e), flush=True)
    finally:
        con.close()


game.RECORD_HOOK = record_kills
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
        s = clean_save(d.get("save"), is_creator_row(row), bool(row["panda"]))
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
        return jsonify({"online": online_count(con), "now": int(time.time() * 1000)})
    finally:
        con.close()


@app.route("/api/leaderboard")
def leaderboard():
    con = db()
    try:
        rows = con.execute("SELECT nick, kills FROM records ORDER BY kills DESC, ts ASC LIMIT 3").fetchall()
        return jsonify({"top": [{"nick": r["nick"], "kills": r["kills"]} for r in rows]})
    finally:
        con.close()


@app.route("/api/online")
def online():
    con = db()
    try:
        return jsonify({"online": online_count(con), "now": int(time.time() * 1000)})
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


# ---------- Pix: Pacote Panda (Mercado Pago) ----------
def mp_call(method, path, body=None, idem=None):
    import urllib.request
    import urllib.error
    req = urllib.request.Request("https://api.mercadopago.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + MP_TOKEN)
    req.add_header("Content-Type", "application/json")
    if idem:
        req.add_header("X-Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("mercadopago erro", e.code, e.read().decode()[:300], flush=True)
        return None
    except Exception as e:
        print("mercadopago falhou:", repr(e), flush=True)
        return None


def pix_settle(con, pay_id):
    """Confere o pagamento DIRETO no Mercado Pago. So libera o Panda se estiver aprovado, com o valor certo."""
    pr = con.execute("SELECT uid, status FROM pix WHERE id=?", (str(pay_id),)).fetchone()
    if pr is None:
        return None
    if pr["status"] == "approved":
        return "approved"
    p = mp_call("GET", "/v1/payments/" + str(pay_id))
    if not p:
        return pr["status"]
    st = p.get("status") or "pending"
    ok = (st == "approved" and p.get("external_reference") == "panda-%d" % pr["uid"]
          and float(p.get("transaction_amount") or 0) >= PANDA_PRICE)
    if ok:
        con.execute("UPDATE users SET panda=1 WHERE id=?", (pr["uid"],))
        con.execute("UPDATE pix SET status='approved' WHERE id=?", (str(pay_id),))
        con.commit()
        return "approved"
    if st != pr["status"]:
        con.execute("UPDATE pix SET status=? WHERE id=?", (st, str(pay_id)))
        con.commit()
    return st


@app.route("/api/pix/create", methods=["POST"])
def pix_create():
    if not MP_TOKEN:
        return err("O pagamento por Pix ainda não está configurado.", 503)
    con = db()
    try:
        row = user_from_token(con)
        if row is None:
            return err("Sessão expirada.", 401)
        if row["panda"]:
            return err("Você já tem o Pacote Panda!", 409)
        n = con.execute("SELECT COUNT(*) AS c FROM pix WHERE uid=? AND created>?",
                        (row["id"], int(time.time()) - 600)).fetchone()["c"]
        if n >= 5:
            return err("Muitas tentativas. Espere alguns minutos.", 429)
        body = {
            "transaction_amount": PANDA_PRICE,
            "description": "Pacote Panda - Royale Senai",
            "payment_method_id": "pix",
            "payer": {"email": "jogador%d@email.com" % row["id"]},
            "external_reference": "panda-%d" % row["id"],
        }
        if PUBLIC_URL:
            body["notification_url"] = PUBLIC_URL + "/api/pix/webhook"
        p = mp_call("POST", "/v1/payments", body, idem=secrets.token_hex(16))
        td = ((p or {}).get("point_of_interaction") or {}).get("transaction_data") or {}
        if not p or not p.get("id") or not td.get("qr_code"):
            return err("Não consegui gerar o Pix agora. Tente de novo.", 502)
        con.execute("INSERT INTO pix(id, uid, status, created) VALUES(?,?,?,?)",
                    (str(p["id"]), row["id"], "pending", int(time.time())))
        con.commit()
        return jsonify({"id": str(p["id"]), "code": td["qr_code"], "qr": td.get("qr_code_base64", "")})
    finally:
        con.close()


@app.route("/api/pix/status")
def pix_status():
    pid = request.args.get("id", "")
    con = db()
    try:
        row = user_from_token(con)
        if row is None:
            return err("Sessão expirada.", 401)
        pr = con.execute("SELECT uid FROM pix WHERE id=?", (pid,)).fetchone()
        if pr is None or pr["uid"] != row["id"]:
            return err("Pagamento não encontrado.", 404)
        st = pix_settle(con, pid)
        return jsonify({"status": st})
    finally:
        con.close()


@app.route("/api/pix/webhook", methods=["POST"])
def pix_webhook():
    d = request.get_json(silent=True) or {}
    pid = str((d.get("data") or {}).get("id") or request.args.get("data.id") or request.args.get("id") or "")
    if pid.isdigit():
        con = db()
        try:
            pix_settle(con, pid)
        except Exception as e:
            print("erro no webhook:", repr(e), flush=True)
        finally:
            con.close()
    return "ok"


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
    client = game.Client(ws, nick, creator, sv["cown"], sv.get("eown"))
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
