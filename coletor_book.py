"""Coletor de metricas do livro de ofertas + fluxo de agressao -> SQLite.

Grava periodicamente (default 1 s) metricas agregadas e o topo do livro, e registra
eventos de mudancas relevantes (paredes de volume que surgem/somem). Outro processo
pode ler o banco em paralelo (modo WAL).

Tabelas (todas com ts_ms = hora do servidor MT5, em ms):
  snapshots  1 linha por intervalo: preco, spread, pressao compradora/vendedora, imbalance,
             microprice, fluxo de agressao no intervalo e saldo acumulado da sessao.
  niveis     os N niveis mais proximos do preco, de cada lado, por snapshot.
  eventos    PAREDE_NOVA / PAREDE_REMOVIDA (nivel cujo volume cruza o limiar de "parede").

Obs.: o MT5 nao informa numero de ordens; so o volume agregado por nivel.

Uso: python coletor_book.py [SIMBOLO] [SEGUNDOS] [--db dados/book.db] [--intervalo 1.0]
                            [--niveis 5] [--parede-mult 3.0] [--parede-min 200]
     (SEGUNDOS=0 roda ate Ctrl+C)
"""
import argparse
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL,
    bid REAL, ask REAL, mid REAL, spread REAL, last REAL,
    vol_bid_total REAL, vol_ask_total REAL,
    vol_bid_near REAL, vol_ask_near REAL,
    imbalance_total REAL, imbalance_near REAL, imbalance_pond REAL,
    microprice REAL, niveis_bid INTEGER, niveis_ask INTEGER,
    n_trades INTEGER, vol_compra_agr REAL, vol_venda_agr REAL, delta REAL,
    saldo_acum REAL,
    PRIMARY KEY (sessao, symbol, ts_ms)
);
CREATE TABLE IF NOT EXISTS niveis (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL,
    lado TEXT NOT NULL, dist INTEGER NOT NULL, preco REAL, volume REAL
);
CREATE INDEX IF NOT EXISTS ix_niveis ON niveis (sessao, symbol, ts_ms);
CREATE TABLE IF NOT EXISTS eventos (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL,
    tipo TEXT NOT NULL, lado TEXT NOT NULL, preco REAL,
    vol_antes REAL, vol_depois REAL, dist INTEGER, limiar REAL
);
CREATE INDEX IF NOT EXISTS ix_eventos ON eventos (symbol, ts_ms);
"""


HISTERESE = 0.7


def utc(ms):
    """copy_ticks_* exige datetime tz-aware; o epoch do servidor e 'tipo UTC' (hora de Brasilia)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def separar_livro(livro):
    """-> (bids do melhor para o pior, asks do melhor para o pior) como listas (preco, volume)."""
    bids = sorted(((b.price, float(b.volume)) for b in livro
                   if b.type in (mt5.BOOK_TYPE_BUY, mt5.BOOK_TYPE_BUY_MARKET)), reverse=True)
    asks = sorted((b.price, float(b.volume)) for b in livro
                  if b.type in (mt5.BOOK_TYPE_SELL, mt5.BOOK_TYPE_SELL_MARKET))
    return bids, asks


def imbalance(b, a):
    return (b - a) / (b + a) if (b + a) else 0.0


def metricas_livro(bids, asks, n):
    vb, va = sum(v for _, v in bids), sum(v for _, v in asks)
    nb, na = bids[:n], asks[:n]
    vb_n, va_n = sum(v for _, v in nb), sum(v for _, v in na)
    # ponderado: nivel i pesa 1/i (o que esta mais perto do preco importa mais)
    pb = sum(v / i for i, (_, v) in enumerate(nb, 1))
    pa = sum(v / i for i, (_, v) in enumerate(na, 1))
    bid, ask = bids[0][0], asks[0][0]
    vbid1, vask1 = bids[0][1], asks[0][1]
    micro = (bid * vask1 + ask * vbid1) / (vbid1 + vask1) if (vbid1 + vask1) else (bid + ask) / 2
    return dict(bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
                vol_bid_total=vb, vol_ask_total=va, vol_bid_near=vb_n, vol_ask_near=va_n,
                imbalance_total=imbalance(vb, va), imbalance_near=imbalance(vb_n, va_n),
                imbalance_pond=imbalance(pb, pa), microprice=micro,
                niveis_bid=len(bids), niveis_ask=len(asks))


def detectar_eventos(ts, ant, atual, limiar, lado):
    """ant/atual: {preco: volume} de um lado. Retorna eventos de parede nova/removida."""
    eventos = []
    if not atual or not ant:
        return eventos
    ordem = sorted(atual, reverse=(lado == "COMPRA"))
    dist_de = {p: i for i, p in enumerate(ordem, 1)}
    lo, hi = min(atual), max(atual)
    lo_ant, hi_ant = min(ant), max(ant)
    baixo = limiar * HISTERESE  # evita piscar quando o volume oscila em torno do limiar
    for p, v in atual.items():
        antes = ant.get(p, 0.0)
        # nivel que so entrou pela borda da janela (preco andou) nao e ordem nova
        if v >= limiar and antes < baixo and lo_ant <= p <= hi_ant:
            eventos.append((ts, "PAREDE_NOVA", lado, p, antes, v, dist_de[p], limiar))
    for p, antes in ant.items():
        if antes >= limiar:
            v = atual.get(p, 0.0)
            # nivel que saiu da janela visivel do livro nao e remocao
            if v < baixo and lo <= p <= hi:
                eventos.append((ts, "PAREDE_REMOVIDA", lado, p, antes, v, dist_de.get(p), limiar))
    return eventos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", default="WIN$")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=1.0)
    ap.add_argument("--niveis", type=int, default=5)
    ap.add_argument("--parede-mult", type=float, default=2.0,
                    help="parede = nivel com volume >= mult x mediana dos niveis do livro")
    ap.add_argument("--parede-min", type=float, default=200.0, help="volume minimo absoluto de uma parede")
    args = ap.parse_args()
    sym = args.symbol

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)

    if not mt5.initialize():
        raise SystemExit(f"initialize falhou: {mt5.last_error()}")
    if not mt5.symbol_select(sym, True) or not mt5.market_book_add(sym):
        raise SystemExit(f"nao foi possivel assinar o livro de {sym}: {mt5.last_error()}")

    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    ultimo_msc = int(mt5.symbol_info_tick(sym).time_msc)
    saldo = 0.0
    ant_b, ant_a = {}, {}
    limiar = None
    ev_pendentes = []
    acc = dict(n=0, comp=0.0, vend=0.0)
    prox = 0.0
    n_snap = n_ev = 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Coletando {sym} -> {args.db} (sessao {sessao}, a cada {args.intervalo}s). Ctrl+C para parar.")

    try:
        while time.time() < fim:
            tk = mt5.symbol_info_tick(sym)
            if tk is None:  # terminal desconectado / sem cotacao: espera e tenta de novo
                time.sleep(1)
                continue
            agora = int(tk.time_msc)

            ticks = mt5.copy_ticks_range(sym, utc(ultimo_msc), utc(agora + 1000), mt5.COPY_TICKS_TRADE)
            if ticks is not None:
                for t in ticks:
                    ms = int(t["time_msc"])
                    if ms <= ultimo_msc:
                        continue
                    ultimo_msc = ms
                    fl = int(t["flags"])
                    vol = float(t["volume_real"]) or float(t["volume"])
                    acc["n"] += 1
                    if fl & mt5.TICK_FLAG_BUY:
                        acc["comp"] += vol
                    elif fl & mt5.TICK_FLAG_SELL:
                        acc["vend"] += vol

            livro = mt5.market_book_get(sym)
            bids = asks = None
            if livro:
                bids, asks = separar_livro(livro)
                if bids and asks:
                    cur_b, cur_a = dict(bids), dict(asks)
                    if limiar is None or time.time() >= prox:  # limiar fixo dentro do intervalo
                        vols = [v for _, v in bids + asks]
                        novo = max(args.parede_min, args.parede_mult * statistics.median(vols))
                        limiar = novo if limiar is None else 0.8 * limiar + 0.2 * novo
                    if ant_b or ant_a:
                        ev_pendentes += detectar_eventos(agora, ant_b, cur_b, limiar, "COMPRA")
                        ev_pendentes += detectar_eventos(agora, ant_a, cur_a, limiar, "VENDA")
                    ant_b, ant_a = cur_b, cur_a

            if time.time() >= prox and bids and asks:
                prox = time.time() + args.intervalo
                m = metricas_livro(bids, asks, args.niveis)
                delta = acc["comp"] - acc["vend"]
                saldo += delta
                db.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (agora, sessao, sym, m["bid"], m["ask"], m["mid"], m["spread"], tk.last,
                     m["vol_bid_total"], m["vol_ask_total"], m["vol_bid_near"], m["vol_ask_near"],
                     m["imbalance_total"], m["imbalance_near"], m["imbalance_pond"], m["microprice"],
                     m["niveis_bid"], m["niveis_ask"], acc["n"], acc["comp"], acc["vend"], delta, saldo))
                db.executemany(
                    "INSERT INTO niveis VALUES (?,?,?,?,?,?,?)",
                    [(agora, sessao, sym, lado, i, p, v)
                     for lado, lista in (("COMPRA", bids), ("VENDA", asks))
                     for i, (p, v) in enumerate(lista[:args.niveis], 1)])
                if ev_pendentes:
                    db.executemany(
                        "INSERT INTO eventos VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(ts, sessao, sym, tipo, lado, p, a, d, dist, lim)
                         for ts, tipo, lado, p, a, d, dist, lim in ev_pendentes])
                    n_ev += len(ev_pendentes)
                    ev_pendentes = []
                db.commit()
                n_snap += 1
                acc = dict(n=0, comp=0.0, vend=0.0)
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        mt5.market_book_release(sym)
        mt5.shutdown()
        db.commit()
        db.close()
    print(f"Snapshots: {n_snap} | eventos: {n_ev} | saldo de agressao da sessao: {saldo:+.0f}")


if __name__ == "__main__":
    main()
