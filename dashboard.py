"""Dashboard local do Jev-trader: le o book.db (somente leitura) e serve uma pagina com graficos.

  python dashboard.py [--db dados/book.db] [--porta 8050]
  abrir http://127.0.0.1:8050

GET /api/dados?janela=<segundos|pregao>  -> JSON com status das coletas, WIN/BOVA11, opcoes e macro.
Todos os ts sao hora do servidor MT5 (Brasilia) codificada como epoch "UTC", como no banco.
"""
import argparse
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

AQUI = Path(__file__).parent
PONTOS = 600            # pontos por serie temporal (define o tamanho do balde)
CACHE_S = 3.0
INICIO_PREGAO_H = 9

# macro: graficos de variacao % (no maximo 4 series cada) e ativos da tabela de correlacao
MACRO_GRAFICOS = {
    "brasil_exterior": ["IND$", "WDO$", "IVVB11", "BEWZ39"],
    "commodities": ["VALE3", "PETR4", "PRIO3", "SUZB3"],
    "domestico": ["ITUB4", "BBAS3", "SMAL11", "GOLD11"],
}
DI_VERTICES = ["DI1F27", "DI1F28", "DI1F29", "DI1F31", "DI1F33", "DI1F35"]
CORREL = ["WDO$", "DI1F27", "DI1F29", "DI1F33", "IVVB11", "NASD11", "BEWZ39", "XINA11",
          "VALE3", "PETR4", "PRIO3", "ITUB4", "BBAS3", "SMAL11", "GOLD11", "BIT$"]


def agora_servidor_ms():
    """Relogio de parede de Brasilia (o do PC) codificado como epoch UTC, igual aos ts do banco."""
    return int(datetime.now().replace(tzinfo=timezone.utc).timestamp() * 1000)


def limpo(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) or math.isinf(v) else round(float(v), 6)
    return v


class Grade:
    """Grade de baldes de tempo [t0, t1] de largura b (ms)."""

    def __init__(self, t0, t1, b):
        self.t0, self.b = t0, b
        self.n = max(int((t1 - t0) // b) + 1, 1)

    def tempos(self):
        return [(self.t0 + i * self.b) / 1000 for i in range(self.n)]

    def ultimo(self, linhas, semente=None, ffill=True):
        """linhas: (ts_ms, valor) em ordem; ultimo valor de cada balde, com forward-fill."""
        out = [None] * self.n
        for ts, v in linhas:
            i = int((ts - self.t0) // self.b)
            if 0 <= i < self.n and v is not None:
                out[i] = v
        if ffill:
            atual = semente
            for i, v in enumerate(out):
                if v is None:
                    out[i] = atual
                else:
                    atual = v
        return out


def dados(con, janela):
    agora = agora_servidor_ms()
    ref = con.execute("SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'").fetchone()[0] or agora
    t1 = min(agora, ref) if agora - ref < 3600_000 else ref   # fora do pregao: ultima coleta
    if janela == "pregao":
        d = datetime.fromtimestamp(t1 / 1000, tz=timezone.utc)
        t0 = int(d.replace(hour=INICIO_PREGAO_H, minute=0, second=0, microsecond=0).timestamp() * 1000)
    else:
        t0 = t1 - int(janela) * 1000
    b = max(1000, ((t1 - t0) // PONTOS) // 1000 * 1000)
    g = Grade(t0, t1, b)
    q = lambda sql, *p: con.execute(sql, p).fetchall()

    # ---------------- status das coletas
    fontes = [
        ("WIN (livro + agressão)", "SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'"),
        ("BOVA11 (livro)", "SELECT max(ts_ms) FROM snapshots WHERE symbol='BOVA11'"),
        ("Opções BOVA11", "SELECT max(ts_ms) FROM opcoes_resumo WHERE subjacente='BOVA11'"),
        ("Macro", "SELECT max(ts_ms) FROM macro_cotacoes WHERE symbol='WDO$'"),
    ]
    status = []
    for nome, sql in fontes:
        try:
            ts = con.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            ts = None
        status.append(dict(nome=nome, ts=ts / 1000 if ts else None, idade_s=(agora - ts) / 1000 if ts else None))
    db_path = Path(con.execute("PRAGMA database_list").fetchone()[2])
    db_mb = sum(p.stat().st_size for p in (db_path, Path(f"{db_path}-wal")) if p.exists()) / 2**20

    # ---------------- WIN / BOVA11
    def livro(sym):
        rows = q("SELECT ts_ms, mid, imbalance_pond, delta FROM snapshots "
                 "WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND spread > 0 ORDER BY ts_ms", sym, t0, t1)
        mid = g.ultimo([(r[0], r[1]) for r in rows])
        # imbalance: media do balde; saldo: soma acumulada do delta dentro da janela
        soma, cont, delta = [0.0] * g.n, [0] * g.n, [0.0] * g.n
        for ts, _, imb, dl in rows:
            i = int((ts - t0) // b)
            if 0 <= i < g.n:
                if imb is not None:
                    soma[i] += imb
                    cont[i] += 1
                delta[i] += dl or 0.0
        imb = [s / c if c else None for s, c in zip(soma, cont)]
        saldo = list(np.cumsum(delta)) if any(r[3] is not None for r in rows) else None
        return dict(mid=mid, imb=imb, saldo=saldo)

    win, bova = livro("WIN$"), livro("BOVA11")

    # ---------------- opcoes
    res = q("SELECT ts_ms, vencimento, atm_iv, rr25, neg_call_compra - neg_call_venda, "
            "neg_put_compra - neg_put_venda FROM opcoes_resumo WHERE subjacente='BOVA11' "
            "AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", t0, t1)
    vencs = sorted({r[1] for r in res})[:3]
    atm = {v: g.ultimo([(r[0], r[2]) for r in res if r[1] == v]) for v in vencs}
    rr = {v: g.ultimo([(r[0], r[3]) for r in res if r[1] == v]) for v in vencs}
    fc, fp = [0.0] * g.n, [0.0] * g.n
    for ts, _, _, _, c, p in res:
        i = int((ts - t0) // b)
        if 0 <= i < g.n:
            fc[i] += c or 0
            fp[i] += p or 0
    tiles = []
    for v in vencs:
        serie = [x for x in atm[v] if x is not None]
        rrs = [x for x in rr[v] if x is not None]
        tiles.append(dict(venc=v, atm=serie[-1] if serie else None, atm_ini=serie[0] if serie else None,
                          rr25=rrs[-1] if rrs else None))
    # smile: ultima cotacao de cada serie nos ultimos 10 min; OTM (put abaixo do forward, call acima)
    smile = {}
    for v in vencs:
        rows = q("SELECT symbol, tipo, strike, iv, spot FROM opcoes_cotacoes WHERE rowid IN ("
                 " SELECT max(rowid) FROM opcoes_cotacoes WHERE subjacente='BOVA11' AND vencimento=? "
                 " AND ts_ms BETWEEN ? AND ? GROUP BY symbol)", v, t1 - 600_000, t1)
        pts = {}
        for _, tipo, k, iv, s in rows:
            if iv is not None and ((tipo == "P" and k <= s) or (tipo == "C" and k > s)):
                pts[k] = iv
        smile[v] = sorted(pts.items())

    # ---------------- macro
    todos = sorted({s for l in MACRO_GRAFICOS.values() for s in l} | set(DI_VERTICES) | set(CORREL))
    marcadores = ",".join("?" * len(todos))
    semente = {s: m for s, m, _ in q(f"SELECT symbol, mid, max(ts_ms) FROM macro_cotacoes WHERE symbol IN "
                                     f"({marcadores}) AND ts_ms < ? AND ts_ms > ? GROUP BY symbol",
                                     *todos, t0, t0 - 86400_000)}
    rows = q(f"SELECT symbol, (ts_ms - ?) / ? AS i, mid, max(ts_ms) FROM macro_cotacoes WHERE symbol IN "
             f"({marcadores}) AND ts_ms BETWEEN ? AND ? GROUP BY symbol, i", t0, b, *todos, t0, t1)
    por_sym = {}
    for s, i, m, ts in rows:
        por_sym.setdefault(s, []).append((ts, m))
    macro_niv = {s: g.ultimo(por_sym.get(s, []), semente.get(s)) for s in todos}
    graficos = {}
    for nome, syms in MACRO_GRAFICOS.items():
        series = {}
        for s in syms:
            niv = macro_niv[s]
            base = next((x for x in niv if x), None)
            series[s] = [(x / base - 1) * 100 if x and base else None for x in niv]
        graficos[nome] = series
    di_ini = [next((x for x in macro_niv[s] if x is not None), None) for s in DI_VERTICES]
    di_fim = [next((x for x in reversed(macro_niv[s]) if x is not None), None) for s in DI_VERTICES]
    incl = [(a - c) * 100 if a is not None and c is not None else None
            for a, c in zip(macro_niv["DI1F33"], macro_niv["DI1F27"])]

    # correlacao de retornos de 1 min com o WIN (DI: variacao da taxa), contemporanea e com 1 min de antecedencia
    gm = Grade(t0, t1, 60_000)
    win_min = gm.ultimo(q("SELECT ts_ms, mid FROM snapshots WHERE symbol='WIN$' AND spread > 0 "
                          "AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", t0, t1))
    rw = np.diff(np.log(np.array([x or np.nan for x in win_min], dtype=float)))
    correl = []
    for s in CORREL:
        niv = gm.ultimo(por_sym.get(s, []) if b <= 60_000 else
                        q("SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                          "ORDER BY ts_ms", s, t0, t1), semente.get(s))
        x = np.array([v if v else np.nan for v in niv], dtype=float)
        rx = np.diff(x) if s.startswith("DI1") else np.diff(np.log(x))
        ok = ~np.isnan(rw) & ~np.isnan(rx)
        cont = np.corrcoef(rw[ok], rx[ok])[0, 1] if ok.sum() >= 10 and np.std(rx[ok]) > 0 else None
        ok2 = ~np.isnan(rw[1:]) & ~np.isnan(rx[:-1])
        lead = (np.corrcoef(rw[1:][ok2], rx[:-1][ok2])[0, 1]
                if ok2.sum() >= 10 and np.std(rx[:-1][ok2]) > 0 else None)
        correl.append(dict(symbol=s, corr=limpo(cont), lead=limpo(lead), n=int(ok.sum())))

    return dict(
        agora=agora / 1000, t0=t0 / 1000, t1=t1 / 1000, bucket_s=b / 1000, tempos=g.tempos(),
        status=status, db_mb=db_mb,
        win=win, bova=bova,
        opcoes=dict(vencs=vencs, atm=atm, rr25=rr, fluxo_call=list(np.cumsum(fc)), fluxo_put=list(np.cumsum(fp)),
                    tiles=tiles, smile=smile),
        macro=dict(graficos=graficos, di=dict(vertices=DI_VERTICES, inicio=di_ini, fim=di_fim),
                   inclinacao=incl, correl=correl,
                   ultimo={s: next((x for x in reversed(macro_niv[s]) if x is not None), None) for s in todos}),
    )


def json_limpo(o):
    if isinstance(o, dict):
        return {k: json_limpo(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_limpo(v) for v in o]
    return limpo(o)


class App:
    def __init__(self, db):
        self.db = db
        self.cache = {}
        self.lock = threading.Lock()

    def obter(self, janela):
        with self.lock:
            hit = self.cache.get(janela)
            if hit and time.time() - hit[0] < CACHE_S:
                return hit[1]
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
            try:
                corpo = json.dumps(json_limpo(dados(con, janela)), ensure_ascii=False).encode()
            finally:
                con.close()
            self.cache[janela] = (time.time(), corpo)
            return corpo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--porta", type=int, default=8050)
    args = ap.parse_args()
    app = App(Path(args.db).resolve())

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def enviar(self, codigo, corpo, tipo):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self.enviar(200, (AQUI / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/dados":
                janela = parse_qs(u.query).get("janela", ["3600"])[0]
                if janela != "pregao" and not janela.isdigit():
                    return self.enviar(400, b'{"erro":"janela invalida"}', "application/json")
                try:
                    self.enviar(200, app.obter(janela), "application/json; charset=utf-8")
                except Exception as e:  # mostra o erro na pagina em vez de derrubar o servidor
                    self.enviar(500, json.dumps({"erro": f"{e.__class__.__name__}: {e}"}).encode(),
                                "application/json")
            else:
                self.enviar(404, b"nao encontrado", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", args.porta), H)
    print(f"Dashboard em http://127.0.0.1:{args.porta}  (db {app.db})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
