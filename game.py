"""
Servidor de jogo do Royale Senai (multiplayer).

- Salas de ate 12 jogadores. Quem aperta JOGAR entra numa sala em espera;
  quando o tempo de espera acaba (ou a sala enche) a partida comeca e as vagas
  que sobraram sao preenchidas com bots.
- Toda a logica (tiros, dano, zona, loot, bots) roda aqui no servidor.
  O navegador so manda a posicao/acoes e desenha o que o servidor manda.
"""
import json
import math
import os
import queue
import random
import threading
import time
from collections import deque

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------- configuracao ----------------
MAX_PLAYERS = 12                                   # humanos + bots por sala
WAIT_SECS = int(os.environ.get("WAIT_SECS", "12"))  # espera por mais jogadores antes de comecar
TICK = 0.05                                        # 20 atualizacoes por segundo
PLANE_MS = 20000
GRACE = 40000
LAKE_R, ISL_R = 50, 25
POIS = [(250, 250, 90, 30, 5), (-250, -250, 55, 22, 4), (150, 24, 12, 8, 1), (24, -170, 12, 8, 1)]  # x,z,s,loot,chests
ISLC = [(8, 8), (-8, 8), (8, -8), (-8, -8)]
WS = [
    dict(d=18, cd=350, sp=.01, pe=1, r=120),
    dict(d=12, cd=110, sp=.03, pe=1, r=140),
    dict(d=11, cd=900, sp=.09, pe=6, r=50),
    dict(d=80, cd=1500, sp=0, pe=1, r=300),
]
DM = [1, 1.15, 1.3, 1.5]
BOT_NAMES = ["Raio", "Tigre", "Lobo", "Nina", "Zeca", "Bia", "Dudu", "Kira", "Maverick", "Luna", "Fênix",
             "Pedro", "Duda", "Thor", "Sol", "Neo"]

# ---------------- mapa (gerado do index.html por tools/gen_map) ----------------
with open(os.path.join(BASE, "map_data.json"), encoding="utf-8") as _f:
    _M = json.load(_f)
MAP_SEED = _M["seed"]
OB = _M["ob"]
CELL = 16.0
GRID = {}
for _i, _o in enumerate(OB):
    if _o["k"] == 0:
        _x0, _x1, _z0, _z1 = _o["x"] - _o["r"], _o["x"] + _o["r"], _o["z"] - _o["r"], _o["z"] + _o["r"]
    else:
        _x0, _x1, _z0, _z1 = _o["x"] - _o["hx"], _o["x"] + _o["hx"], _o["z"] - _o["hz"], _o["z"] + _o["hz"]
    for _cx in range(int(_x0 // CELL), int(_x1 // CELL) + 1):
        for _cz in range(int(_z0 // CELL), int(_z1 // CELL) + 1):
            GRID.setdefault((_cx, _cz), []).append(_i)


def rn(a, b):
    return a + random.random() * (b - a)


def jround(x):
    return int(math.floor(x + .5))


def in_lake(x, z):
    h = math.hypot(x, z)
    return ISL_R < h < LAKE_R and abs(x) > 2.7 and abs(z) > 2.7


def rp(m):
    for _ in range(40):
        x, z = rn(-m, m), rn(-m, m)
        if math.hypot(x, z) > LAKE_R + 8:
            return x, z
    return m * .8, m * .8


def roll_r(b=0):
    x = random.random() - b
    return 0 if x < .5 else 1 if x < .8 else 2 if x < .95 else 3


def roll_w():
    return random.choice([0, 0, 0, 1, 1, 1, 2, 2, 3])


def num(v):
    """Numero finito ou None (protege contra lixo vindo do cliente)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


# ---------------- conexao de um jogador ----------------
class Client:
    """Um navegador conectado. Envia por uma fila + thread propria para
    um cliente lento nunca travar o loop do jogo."""

    def __init__(self, ws, nick, creator, cown):
        self.ws = ws
        self.nick = nick
        self.creator = creator
        self.cown = cown
        self.room = None
        self.player = None
        self.q = queue.Queue()
        self.closed = False
        threading.Thread(target=self._writer, daemon=True).start()

    def put(self, s):
        if self.closed:
            return
        if self.q.qsize() > 400:      # cliente nao esta aguentando: derruba
            self.close()
            return
        self.q.put(s)

    def send(self, obj):
        self.put(json.dumps(obj, separators=(",", ":")))

    def _writer(self):
        while True:
            s = self.q.get()
            if s is None:
                return
            try:
                self.ws.send(s)
            except Exception:
                self.closed = True
                return

    def close(self):
        self.closed = True
        self.q.put(None)
        try:
            self.ws.close()
        except Exception:
            pass


class Player:
    def __init__(self, pid, nick, skin, bot=False, client=None):
        self.id, self.nick, self.skin = pid, nick, skin
        self.bot, self.client = bot, client
        self.x = self.z = 0.0
        self.y = 130.0
        self.ry = 0.0
        self.hp = 100.0
        self.alive = True
        self.ph = "plane"
        self.w = -1
        self.r = 0
        self.k = 0
        self.last = 0
        self.vy = 0.0
        self.tg = None
        self.jt = 0
        self.inv = []          # itens que o servidor sabe que o humano tem: [w, r]
        self.last_heal = 0
        self.team = 0
        self.down = False       # caido (modo dupla)
        self.dhp = 0.0          # vida enquanto caido
        self.dby = None
        self.revp = 0.0         # progresso da cura (segundos)
        self.rv_by = None
        self.rv_t = 0.0


def now_ms():
    return time.time() * 1000


# ---------------- sala ----------------
class Room:
    def __init__(self, mode="solo", code=""):
        self.mode, self.code = mode, code
        self.maxp = MAX_PLAYERS if mode == "solo" else 16
        self.hmax = 2 if code else self.maxp
        self.state = "waiting"       # waiting -> playing -> over
        self.players = {}
        self.next_id = 1
        self.loot = {}
        self.chests = {}
        self.nid = 1000
        self.dead = set()
        self.hp = {}
        self.plane = None
        self.t0 = 0
        self.wait_until = time.time() + WAIT_SECS
        self.last_lobby = 0
        self.last_tick = time.time()

    # ---- util ----
    def humans(self):
        return [p for p in self.players.values() if not p.bot]

    def alive(self):
        return [p for p in self.players.values() if p.alive]

    def emit(self, obj):
        s = json.dumps(obj, separators=(",", ":"))
        for p in self.players.values():
            if not p.bot and p.client:
                p.client.put(s)

    # ---- entrada / saida ----
    def add(self, client, skin):
        pid = self.next_id
        self.next_id += 1
        p = Player(pid, client.nick, skin, client=client)
        self.players[pid] = p
        client.room, client.player = self, p
        client.send({"t": "init", "id": pid})
        self.send_lobby(True)
        if self.code and len(self.humans()) >= 2:
            self.wait_until = min(self.wait_until, time.time() + 3)
        if len(self.humans()) >= self.hmax and not self.code:
            self.start()

    def remove(self, client):
        p = client.player
        client.room = client.player = None
        if p is None or p.id not in self.players:
            return
        if self.state == "waiting":
            del self.players[p.id]
            self.send_lobby(True)
        elif self.state == "playing":
            if p.alive:
                p.alive = False
                self.emit({"t": "feed", "a": p.nick + " saiu"})
            p.client = None
            p.bot = True          # vira "fantasma" sem cliente (morto, so ocupa lugar)
            p.dead_human = True

    def send_lobby(self, force=False):
        n = time.time()
        if not force and n - self.last_lobby < 1:
            return
        self.last_lobby = n
        left = max(0, int(math.ceil(self.wait_until - n)))
        self.emit({"t": "lobby", "n": len(self.humans()), "left": left, "max": MAX_PLAYERS})

    # ---- inicio ----
    def start(self):
        if self.state != "waiting":
            return
        self.state = "playing"
        hn = {p.nick.lower() for p in self.players.values()}
        names = [b for b in BOT_NAMES if b.lower() not in hn]
        random.shuffle(names)
        i = 0
        while len(self.players) < self.maxp:
            pid = self.next_id
            self.next_id += 1
            b = Player(pid, names[i % len(names)] + ("" if i < len(names) else str(i)), i % 7, bot=True)
            i += 1
            b.jt = rn(2000, 17000)
            b.tg = [rn(-380, 380), rn(-380, 380)]
            self.players[pid] = b
        self.make_teams()
        self.plane = dict(ax=-320, az=rn(-150, 150), bx=320, bz=rn(-150, 150))
        self.t0 = now_ms()
        self.last_tick = time.time()
        self.gen_loot()
        self.emit({"t": "start", "seed": MAP_SEED, "plane": self.plane, "loot": list(self.loot.values()),
                   "chests": list(self.chests.values()), "dur": PLANE_MS})

    def add_loot(self, li, x, z):
        k = random.random()
        w = roll_w() if k < .6 else 4 if k < .86 else 5
        self.loot[li] = {"id": li, "x": x, "z": z, "w": w, "r": roll_r(0) if w < 4 else 0}

    def gen_loot(self):
        self.loot, self.chests, self.nid = {}, {}, 1000
        li = 0
        for _ in range(110):
            x, z = rp(400)
            self.add_loot(li, x, z)
            li += 1
        for (qx, qz, s, nl, nc) in POIS:
            for _ in range(nl):
                self.add_loot(li, qx + rn(-s, s), qz + rn(-s, s))
                li += 1
        for i in range(30):
            x, z = rp(400)
            self.chests["c%d" % i] = {"id": "c%d" % i, "x": x, "z": z, "open": 0}
        for j, (qx, qz, s, nl, nc) in enumerate(POIS):
            for i in range(nc):
                cid = "p%d_%d" % (j, i)
                self.chests[cid] = {"id": cid, "x": qx + rn(-s, s), "z": qz + rn(-s, s), "open": 0}
        for i, (qx, qz) in enumerate(ISLC):
            cid = "i%d" % i
            self.chests[cid] = {"id": cid, "x": qx, "z": qz, "open": 0, "leg": 1}
        for _ in range(2):          # empurra loot/baus pra fora de paredes
            for d in list(self.loot.values()) + list(self.chests.values()):
                d["x"], d["z"] = self.resolve(d["x"], d["z"], 1.3, 0)

    # ---- colisao ----
    def resolve(self, x, z, rad, y):
        for cx in range(int((x - rad) // CELL), int((x + rad) // CELL) + 1):
            for cz in range(int((z - rad) // CELL), int((z + rad) // CELL) + 1):
                for i in GRID.get((cx, cz), ()):
                    if i in self.dead:
                        continue
                    o = OB[i]
                    if y > o["h"]:
                        continue
                    dx, dz = x - o["x"], z - o["z"]
                    if o["k"] == 0:
                        m = o["r"] + rad
                        if abs(dx) > m or abs(dz) > m:
                            continue
                        d = math.hypot(dx, dz) or .001
                        if d < m:
                            x, z = o["x"] + dx / d * m, o["z"] + dz / d * m
                    else:
                        ex, ez = o["hx"] + rad, o["hz"] + rad
                        if abs(dx) >= ex or abs(dz) >= ez:
                            continue
                        if ex - abs(dx) < ez - abs(dz):
                            x = o["x"] + (-ex if dx < 0 else ex)
                        else:
                            z = o["z"] + (-ez if dz < 0 else ez)
        return x, z

    def ray_ob(self, o, d, mx):
        cand = set()
        t = 0.0
        step = CELL / 4
        while True:
            tt = min(t, mx)
            cell = (int((o[0] + d[0] * tt) // CELL), int((o[2] + d[2] * tt) // CELL))
            cand.update(GRID.get(cell, ()))
            if tt >= mx:
                break
            t += step
        bt, bo = mx, None
        for i in cand:
            if i in self.dead:
                continue
            b = OB[i]
            if b["k"] == 0:
                ox, oz = o[0] - b["x"], o[2] - b["z"]
                a = d[0] * d[0] + d[2] * d[2]
                if a < 1e-6:
                    continue
                bb = ox * d[0] + oz * d[2]
                c = ox * ox + oz * oz - b["r"] ** 2
                ds = bb * bb - a * c
                if ds < 0:
                    continue
                t = (-bb - math.sqrt(ds)) / a
            else:
                t0, t1 = -1e9, 1e9
                for oo, dd, cc, hh in ((o[0], d[0], b["x"], b["hx"]), (o[2], d[2], b["z"], b["hz"])):
                    if abs(dd) < 1e-6:
                        if abs(oo - cc) > hh:
                            t1 = -1
                    else:
                        a, e = (cc - hh - oo) / dd, (cc + hh - oo) / dd
                        if a > e:
                            a, e = e, a
                        t0, t1 = max(t0, a), min(t1, e)
                if t0 > t1 or t1 < 0:
                    continue
                t = t0
            if t < 0 or t >= bt:
                continue
            y = o[1] + d[1] * t
            if y < 0 or y > b["h"]:
                continue
            bt, bo = t, i
        return (bo, bt) if bo is not None else None

    def hit_ob(self, i, dm, o, v, t):
        b = OB[i]
        br = 0
        if b["hp"] < 1e8:
            hp = self.hp.get(i, b["hp"]) - dm
            self.hp[i] = hp
            if hp <= 0:
                self.dead.add(i)
                br = 1
        self.emit({"t": "ob", "i": i, "x": o[0] + v[0] * t, "y": o[1] + v[1] * t, "z": o[2] + v[2] * t, "b": br, "k": b["tp"]})

    # ---- loot ----
    def drop_loot(self, w, r, x, z):
        l = {"id": self.nid, "x": x, "z": z, "w": w, "r": r}
        self.nid += 1
        self.loot[l["id"]] = l
        self.emit({"t": "loot", "l": l, "dl": 1500})

    def pickup(self, p, lid):
        l = self.loot.get(lid)
        if not l or math.hypot(l["x"] - p.x, l["z"] - p.z) > 4.5:
            return
        if not p.bot and len(p.inv) >= 6:
            return
        del self.loot[lid]
        if not p.bot:
            p.inv.append([l["w"], l["r"] or 0])
            self.emit({"t": "pick", "id": lid})
            p.client.send({"t": "got", "w": l["w"], "r": l["r"] or 0})
            return
        if l["w"] >= 4:
            p.hp = min(100, p.hp + (15 if l["w"] == 4 else 50))
        else:
            p.w, p.r = l["w"], l["r"] or 0
        self.emit({"t": "pick", "id": lid})

    def open_chest(self, p, cid):
        c = self.chests.get(cid)
        if not c or c["open"] or math.hypot(c["x"] - p.x, c["z"] - p.z) > 5:
            return
        c["open"] = 1
        self.emit({"t": "chest", "id": cid})
        heal = lambda: 4 if random.random() < .5 else 5
        if c.get("leg"):
            lw = lambda: random.choice([0, 1, 1, 2, 2, 3, 3])
            self.drop_loot(lw(), 3, c["x"] - 1.3, c["z"] + .9)
            self.drop_loot(lw(), 3, c["x"] + 1.3, c["z"] + .9)
            self.drop_loot(heal(), 0, c["x"], c["z"] + 1.8)
            return
        self.drop_loot(roll_w(), roll_r(.25), c["x"] - 1.3, c["z"] + .9)
        self.drop_loot(roll_w(), roll_r(.25), c["x"] + 1.3, c["z"] + .9)
        self.drop_loot(heal(), 0, c["x"], c["z"] + 1.8)

    # ---- combate ----
    # ---- duplas ----
    def make_teams(self):
        ps = list(self.players.values())
        if self.mode == "solo":
            for p in ps:
                p.team = p.id
            return
        hs = [p for p in ps if not p.bot]
        bs = [p for p in ps if p.bot]
        tid = 1
        if self.mode == "duo":
            i = 0
            while i < len(hs):
                hs[i].team = tid
                if i + 1 < len(hs):
                    hs[i + 1].team = tid
                    i += 2
                else:
                    if bs:
                        bs.pop().team = tid
                    i += 1
                tid += 1
        else:
            for h in hs:
                h.team = tid
                tid += 1
        while bs:
            bs.pop().team = tid
            if bs:
                bs.pop().team = tid
            tid += 1

    def can_down(self, q):
        return self.mode != "solo" and not q.down and any(
            t.alive and not t.down and t.team == q.team and t is not q for t in self.players.values())

    def knock(self, q, killer):
        q.down, q.dhp, q.hp, q.revp, q.dby = True, 100.0, 0.0, 0.0, killer
        if killer and killer is not q:
            self.emit({"t": "feed", "x": killer.nick + " deixou " + q.nick + " atordoado", "dn": 1})
        else:
            self.emit({"t": "feed", "x": q.nick + " caiu", "dn": 1})

    def teams_alive(self):
        return len({p.team for p in self.players.values() if p.alive})

    def revive_msg(self, p, m):
        i = m.get("id")
        t = self.players.get(i) if isinstance(i, int) else None
        if p.down or t is None or t is p or not t.down or not t.alive or t.team != p.team:
            return
        if math.hypot(t.x - p.x, t.z - p.z) > 4:
            return
        n = time.time()
        if t.rv_by == p.id and n - t.rv_t < .7:
            t.revp += n - t.rv_t
        else:
            t.revp, t.rv_by = 0.0, p.id
        t.rv_t = n

    def die(self, v, k):
        v.alive = False
        v.down = False
        if k and k is not v:
            k.k += 1
        if v.bot and v.w >= 0:
            self.drop_loot(v.w, v.r or 0, v.x + rn(-1, 1), v.z + rn(-1, 1))
        elif not v.bot:
            for w, r in v.inv:
                x, z = self.resolve(v.x + rn(-2, 2), v.z + rn(-2, 2), 1.2, 0)
                self.drop_loot(w, r, x, z)
            v.inv = []
            ta = self.teams_alive()
            v.client.send({"t": "dead", "place": ta if any(q.team == v.team for q in self.alive()) else ta + 1, "k": v.k})
        if self.mode != "solo" and k and k is not v:
            self.emit({"t": "feed", "x": k.nick + " abateu " + v.nick, "a": k.nick, "v": v.nick})
        else:
            self.emit({"t": "feed", "a": k.nick if k and k is not v else "Zona", "v": v.nick})

    def hit_ray(self, o, d, r, skip):
        best, bt = None, r
        sp = self.players.get(skip)
        for q in self.players.values():
            if not q.alive or q.id == skip or q.ph == "plane" or (sp is not None and q.team == sp.team):
                continue
            cx, cy, cz = q.x - o[0], q.y + .9 - o[1], q.z - o[2]
            t = cx * d[0] + cy * d[1] + cz * d[2]
            if t < 0 or t > bt:
                continue
            px, py, pz = cx - d[0] * t, cy - d[1] * t, cz - d[2] * t
            if px * px + py * py + pz * pz < .64:
                best, bt = q, t
        return best, (bt if best else 1e9)

    def fire(self, p, d):
        if not (0 <= p.w < 4) or p.ph == "plane" or p.down:
            return
        w = WS[p.w]
        n = now_ms()
        if n - p.last < w["cd"] * .9:
            return
        p.last = n
        dm = jround(w["d"] * DM[p.r or 0])
        self.emit({"t": "sh", "id": p.id, "w": p.w, "x": p.x, "y": p.y, "z": p.z, "d": d})
        o = [p.x, p.y + 1.6, p.z]
        acc = {}
        for _ in range(w["pe"]):
            v = [c + rn(-w["sp"], w["sp"]) for c in d]
            l = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1
            v = [c / l for c in v]
            q, ht = self.hit_ray(o, v, w["r"], p.id)
            ro = self.ray_ob(o, v, ht if q else w["r"])
            if ro:
                self.hit_ob(ro[0], dm, o, v, ro[1])
                q = None
            if q:
                acc[q] = acc.get(q, 0) + dm
                if q.down:
                    q.dhp -= dm
                    if q.dhp <= 0 and q.alive:
                        self.die(q, p)
                else:
                    q.hp -= dm
                    if q.hp <= 0 and q.alive:
                        if self.can_down(q):
                            self.knock(q, p)
                        else:
                            self.die(q, p)
        if not p.bot and p.client:
            for q, dmg in acc.items():
                p.client.send({"t": "hit", "d": dmg, "x": q.x, "y": q.y + 2.1, "z": q.z, "k": 0 if q.alive else 1})

    # ---- mensagens dos jogadores ----
    def on_msg(self, p, m):
        if self.state != "playing" or not p.alive or p.bot:
            return
        t = m.get("t")
        if p.down and t in ("shoot", "heal", "pick", "open", "drop", "eq"):
            return
        if t == "rv":
            self.revive_msg(p, m)
        elif t == "jump":
            if p.ph == "plane":
                f = min(1, (now_ms() - self.t0) / PLANE_MS)
                pl = self.plane
                p.x = pl["ax"] + (pl["bx"] - pl["ax"]) * f
                p.z = pl["az"] + (pl["bz"] - pl["az"]) * f
                p.y, p.vy, p.ph = 130.0, 0.0, "air"
        elif t == "pos":
            x, y, z, ry = num(m.get("x")), num(m.get("y")), num(m.get("z")), num(m.get("ry"))
            if None in (x, y, z, ry) or p.ph == "plane":
                return
            p.x, p.z = max(-460, min(460, x)), max(-460, min(460, z))
            p.y = max(0, min(140, y))
            p.ry = ry
            if p.ph == "air" and p.y <= .1:
                p.ph = "ground"
        elif t == "pick":
            if isinstance(m.get("id"), int) and p.ph != "plane":
                self.pickup(p, m["id"])
        elif t == "open":
            if isinstance(m.get("id"), str) and p.ph != "plane":
                self.open_chest(p, m["id"])
        elif t == "eq":
            w, r = m.get("w"), m.get("r", 0)
            if w == -1:
                p.w, p.r = -1, 0
            elif isinstance(w, int) and isinstance(r, int) and [w, r] in p.inv and 0 <= w < 4:
                p.w, p.r = w, r
        elif t == "shoot":
            w, r, d = m.get("w"), m.get("r", 0), m.get("d")
            if not (isinstance(w, int) and isinstance(r, int) and 0 <= w < 4 and [w, r] in p.inv):
                return
            if not (isinstance(d, list) and len(d) == 3):
                return
            d = [num(c) for c in d]
            if None in d:
                return
            l = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) or 1
            p.w, p.r = w, r
            self.fire(p, [c / l for c in d])
        elif t == "heal":
            a, c = m.get("a"), m.get("c")
            kit = 4 if (a, c) == (15, 75) else 5 if (a, c) == (50, 100) else None
            n = now_ms()
            if kit is None or [kit, 0] not in p.inv or n - p.last_heal < 800:
                return
            p.inv.remove([kit, 0])
            p.last_heal = n
            p.hp = max(p.hp, min(c, p.hp + a))
        elif t == "drop":
            w, r, x, z = m.get("w"), m.get("r", 0), num(m.get("x")), num(m.get("z"))
            if not (isinstance(w, int) and isinstance(r, int)) or x is None or z is None:
                return
            if [w, r] not in p.inv or math.hypot(x - p.x, z - p.z) > 7:
                return
            p.inv.remove([w, r])
            if p.w == w and p.r == r and [w, r] not in p.inv:
                p.w, p.r = -1, 0
            self.drop_loot(w, r, x, z)

    # ---- bots ----
    def bot_ai(self, p, dt, r):
        if p.ph == "plane" or p.down:
            return
        if p.ph == "ground":
            for q in self.players.values():
                if q.down and q.alive and q.team == p.team and q is not p:
                    d = math.hypot(q.x - p.x, q.z - p.z)
                    if d > 2.2:
                        nx, nz = p.x + (q.x - p.x) / d * 7 * dt, p.z + (q.z - p.z) / d * 7 * dt
                        p.ry = math.atan2(-(q.x - p.x), -(q.z - p.z))
                        p.x, p.z = self.resolve(nx, nz, .5, p.y)
                    else:
                        q.revp += dt
                        q.rv_t, q.rv_by = time.time(), p.id
                    return
        gx = gz = 0.0
        sp = 6
        tgt = None
        if p.ph == "air":
            p.vy = max(-30 if p.y > 50 else -8, p.vy - 25 * dt)
            p.y += p.vy * dt
            sp = 14
            gx, gz = p.tg
            if p.y <= 0:
                p.y, p.vy, p.ph = 0.0, 0.0, "ground"
        elif math.hypot(p.x, p.z) > r - 10:
            gx = gz = 0.0
            sp = 8
        else:
            bd = 70
            if p.w >= 0:
                for q in self.players.values():
                    if q is not p and q.alive and q.ph != "plane" and q.team != p.team:
                        d = math.hypot(q.x - p.x, q.z - p.z)
                        if d < bd:
                            bd, tgt = d, q
            if tgt:
                dx, dz = tgt.x - p.x, tgt.z - p.z
                dy = tgt.y + .9 - (p.y + 1.6)
                d = math.hypot(dx, dz) or 1
                L = math.sqrt(dx * dx + dy * dy + dz * dz) or 1
                p.ry = math.atan2(-dx, -dz)
                if d > 25:
                    gx, gz = tgt.x, tgt.z
                else:
                    gx, gz = p.x - dz * .5, p.z + dx * .5
                if d < WS[p.w]["r"] * .6 and random.random() < .5:
                    e = .06
                    self.fire(p, [dx / L + rn(-e, e), dy / L + rn(-e, e), dz / L + rn(-e, e)])
            else:
                best, b2 = None, 1e9
                if p.w < 0 or p.hp < 50:
                    want_gun = p.w < 0
                    for l in self.loot.values():
                        if (l["w"] < 4) != want_gun:
                            continue
                        d = math.hypot(l["x"] - p.x, l["z"] - p.z)
                        if d < b2:
                            b2, best = d, l
                if best:
                    gx, gz = best["x"], best["z"]
                    if b2 < 2.5:
                        self.pickup(p, best["id"])
                else:
                    if not p.tg or math.hypot(p.tg[0] - p.x, p.tg[1] - p.z) < 4:
                        p.tg = [rn(-320, 320), rn(-320, 320)]
                    gx, gz = p.tg
        if p.ph != "air" and in_lake(p.x, p.z):
            sp *= .5
        dx, dz = gx - p.x, gz - p.z
        d = math.hypot(dx, dz)
        if d > 1:
            nx, nz = p.x + dx / d * sp * dt, p.z + dz / d * sp * dt
            if not tgt:
                p.ry = math.atan2(-dx, -dz)
            p.x, p.z = self.resolve(nx, nz, .5, p.y)

    # ---- loop da sala ----
    def update(self):
        n = time.time()
        if self.state == "waiting":
            if n >= self.wait_until:
                self.start()
            else:
                self.send_lobby()
            return
        if self.state != "playing":
            return
        dt = max(.01, min(.2, n - self.last_tick))
        self.last_tick = n
        el = now_ms() - self.t0
        zt = el - GRACE
        r = 440 if zt < 0 else max(8, 440 - zt / 1000 * 2.2)
        f = min(1, el / PLANE_MS)
        pl = self.plane
        px, pz = pl["ax"] + (pl["bx"] - pl["ax"]) * f, pl["az"] + (pl["bz"] - pl["az"]) * f
        for p in list(self.players.values()):
            if not p.alive:
                continue
            if p.ph == "plane" and (el >= PLANE_MS or (p.bot and el >= p.jt)):
                p.ph = "air"
                p.x, p.z, p.y, p.vy = px, pz, 130.0, 0.0
            if p.bot:
                self.bot_ai(p, dt, r)
            if p.alive and p.down:
                p.dhp -= dt                      # cai 1 de vida por segundo
                if n - p.rv_t > .7:
                    p.revp = 0.0
                if p.revp >= 7:
                    p.down, p.hp, p.revp = False, 30.0, 0.0
                    self.emit({"t": "feed", "x": p.nick + " foi reanimado", "dn": 1})
                elif p.dhp <= 0 or not any(t.alive and not t.down and t.team == p.team and t is not p
                                           for t in self.players.values()):
                    self.die(p, p.dby)
            if p.alive and p.ph != "plane" and math.hypot(p.x, p.z) > r:
                dmg = (2 + max(0, zt) / 30000) * dt
                if p.down:
                    p.dhp -= dmg
                else:
                    p.hp -= dmg
                    if p.hp <= 0:
                        if self.can_down(p):
                            self.knock(p, None)
                        else:
                            self.die(p, None)
        a = self.alive()
        self.emit({"t": "s", "r": r, "a": len(a), "p": [
            [p.id, p.nick, round(p.x, 2), round(p.y, 2), round(p.z, 2), round(p.ry, 3),
             max(0, jround(p.dhp if p.down else p.hp)), 1 if p.alive else 0, p.skin, p.w,
             1 if p.ph == "plane" else 0, p.team, 1 if p.down else 0, round(p.revp, 1)] for p in self.players.values()]})
        humans_alive = [p for p in a if not p.bot]
        if len({p.team for p in a}) <= 1:
            for p in a:
                if not p.bot and p.client:
                    p.client.send({"t": "dead", "place": 1, "k": p.k})
            self.emit({"t": "end", "w": " + ".join(p.nick for p in a)})
            self.state = "over"
        elif not humans_alive:
            self.state = "over"      # so sobraram bots: nao ha mais ninguem para ver

    def finish(self):
        for p in self.humans():
            if p.client and p.client.room is self:
                p.client.room = p.client.player = None


# ---------------- matchmaking ----------------
LOCK = threading.RLock()
ROOMS = []


def join(client, skin, mode="solo", code=""):
    start_loop()
    with LOCK:
        leave(client)
        room = None
        for r in ROOMS:
            if r.state == "waiting" and r.mode == mode and r.code == code and len(r.humans()) < r.hmax:
                room = r
                break
        if room is None:
            room = Room(mode, code)
            ROOMS.append(room)
        room.add(client, skin)


def leave(client):
    with LOCK:
        room = client.room
        if room is not None:
            room.remove(client)


def handle(client, raw):
    if not isinstance(raw, str) or len(raw) > 4000:
        return
    try:
        m = json.loads(raw)
    except ValueError:
        return
    if not isinstance(m, dict):
        return
    t = m.get("t")
    with LOCK:
        if t == "join":
            skin = m.get("skin")
            skin = skin if isinstance(skin, int) and not isinstance(skin, bool) else 0
            top = 7 if client.creator else 6
            if not (0 <= skin <= top) or (skin not in client.cown and not client.creator):
                skin = 0
            mode = m.get("mode") if m.get("mode") in ("solo", "duo", "svd") else "solo"
            code = "".join(c for c in str(m.get("code", "")).upper() if c.isalnum())[:6] if mode == "duo" else ""
            join(client, skin, mode, code)
        elif t == "leave":
            leave(client)
        elif client.room is not None and client.player is not None:
            client.room.on_msg(client.player, m)


def loop():
    while True:
        t0 = time.time()
        with LOCK:
            for room in list(ROOMS):
                try:
                    room.update()
                except Exception as e:     # um erro numa sala nao derruba as outras
                    print("erro na sala:", repr(e), flush=True)
                    room.state = "over"
                keep = room.state in ("waiting", "playing") and (room.humans() or room.state == "playing" and any(
                    p.client for p in room.players.values()))
                if room.state == "waiting" and not room.humans():
                    keep = False
                if not keep:
                    room.finish()
                    ROOMS.remove(room)
        time.sleep(max(0.001, TICK - (time.time() - t0)))


def stats():
    with LOCK:
        return {"rooms": len(ROOMS), "players": sum(len(r.humans()) for r in ROOMS)}


_started_pid = None


def start_loop():
    # com "preload" o gunicorn importa o app no processo MASTER e depois faz fork:
    # threads nao passam pro worker. Por isso o loop e iniciado por processo (pid).
    global _started_pid
    if _started_pid != os.getpid():
        _started_pid = os.getpid()
        threading.Thread(target=loop, daemon=True).start()
