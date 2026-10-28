"""Recalcula pelos ticks do MT5 a agressao gravada antes da correcao de 27-28/10/2026 (ver ticks_trade.py).

Os coletores contavam como compra os ticks sem agressor (as duas flags) e as impressoes de leilao, descartavam os
ticks repetidos no mesmo milissegundo (~45% do volume do WIN) e liam as acoes so pelo ultimo tick (~60% do volume).
Aqui, para cada dia com ticks no MT5:
  snapshots      WIN$: n_trades, vol_compra_agr, vol_venda_agr, delta (ticks entre o snapshot anterior e o atual) e
                 saldo_acum (acumulado da sessao do coletor, como antes)
  agressao_1min  WDO$ e acoes da cesta: as linhas do dia sao trocadas por uma por minuto (sessao 'recalc_AAAA-MM-DD')
Mesmas regras dos coletores atuais (ticks_trade.py): so ticks com exatamente uma flag, leilao sem agressor, futuros a
partir de 09:05 e acoes de 10:00 a 16:55.
O MT5 guarda ticks so de algumas semanas, e a primeira leitura de um dia pode falhar enquanto o terminal baixa o
historico (tenta 4 vezes; o terminal segue baixando entre as tentativas). Dia sem tick fica como esta e e listado. Na aplicacao de 28/10/2026 os snapshots do WIN
anteriores a 13/10 (sem ticks no MT5) tiveram a agressao anulada.

Uso: python recalcular_agressao.py [--db dados/book.db] [--desde AAAA-MM-DD] [--ate AAAA-MM-DD] [--ativos A B ...]
                                   [--gravar]
     sem --gravar so compara o antigo com o recalculado. Padrao: todos os dias com coleta ate ontem, todos os ativos.
"""
import argparse
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import banco
from coletor_agressao import CESTA, FUTURO
from ticks_trade import INICIO_MS, JANELA_ACOES, sinais

DIA_MS = 86_400_000


def _ms(dia, hhmm="00:00"):
    return int(pd.Timestamp(f"{dia} {hhmm}", tz="UTC").timestamp() * 1000)


def ticks_do_dia(sym, dia, tentativas=4):
    """DataFrame (ms, sinal, vol, preco) da janela de contagem do ativo; sinal +1 compra, -1 venda, 0 sem agressor
    ou leilao. None sem ticks."""
    ini = datetime.fromisoformat(f"{dia} 08:30").replace(tzinfo=timezone.utc)
    for k in range(tentativas):
        t = mt5.copy_ticks_range(sym, ini, ini + timedelta(hours=10, minutes=30), mt5.COPY_TICKS_TRADE)
        if t is not None and len(t):
            break
        time.sleep(5 * (k + 1))
    else:
        return None
    vol = np.where(t["volume_real"] > 0, t["volume_real"], t["volume"]).astype(float)
    d = pd.DataFrame(dict(ms=t["time_msc"].astype(np.int64), sinal=sinais(t["time_msc"], t["flags"]),
                          vol=vol, preco=t["last"].astype(float)))
    lo, hi = JANELA_ACOES if sym in CESTA else (INICIO_MS, DIA_MS)
    h = d.ms % DIA_MS
    return d[(h >= lo) & (h < hi)].reset_index(drop=True)


def recalcular_win(con, dia, tk):
    """Linhas (n, compra, venda, delta, saldo, sessao, symbol, ts_ms) para o UPDATE de snapshots do WIN$."""
    s = pd.read_sql("SELECT sessao, ts_ms, delta FROM snapshots WHERE symbol='WIN$' AND ts_ms >= ? AND ts_ms < ? "
                    "AND delta IS NOT NULL ORDER BY ts_ms", con, params=(_ms(dia), _ms(dia) + DIA_MS))
    if s.empty:
        return s, []
    # ticks em (snapshot anterior, snapshot atual]: somas acumuladas cortadas na hora de cada snapshot
    corte = np.searchsorted(tk.ms.to_numpy(), s.ts_ms.to_numpy(), side="right")
    acum = lambda x: np.concatenate([[0.0], np.cumsum(x)])[corte]
    n = np.diff(np.concatenate([[0], corte]))
    comp = np.diff(np.concatenate([[0.0], acum(np.where(tk.sinal > 0, tk.vol, 0.0))]))
    vend = np.diff(np.concatenate([[0.0], acum(np.where(tk.sinal < 0, tk.vol, 0.0))]))
    s["n"], s["comp"], s["vend"], s["novo"] = n, comp, vend, comp - vend
    s["saldo"] = s.groupby("sessao").novo.cumsum()
    linhas = [(int(r.n), float(r.comp), float(r.vend), float(r.novo), float(r.saldo), r.sessao, "WIN$", int(r.ts_ms))
              for r in s.itertuples()]
    return s, linhas


def recalcular_minutos(sym, dia, tk):
    """Linhas de agressao_1min (uma por minuto). Acoes com financeiro (R$); WDO sem fin_*."""
    fin = sym in CESTA
    x = tk.assign(minuto=tk.ms // 60000 * 60000, c=np.where(tk.sinal > 0, tk.vol, 0.0),
                  v=np.where(tk.sinal < 0, tk.vol, 0.0), nt=np.where(tk.sinal == 0, tk.vol, 0.0))
    x["fc"], x["fv"] = x.c * x.preco, x.v * x.preco
    m = x.groupby("minuto").agg(n=("vol", "size"), c=("c", "sum"), v=("v", "sum"), nt=("nt", "sum"), fc=("fc", "sum"),
                                fv=("fv", "sum"), maior=("vol", "max"), preco=("preco", "last"))
    return [(int(k), f"recalc_{dia}", sym, int(r.n), float(r.c), float(r.v), float(r.nt),
             float(r.fc) if fin else None, float(r.fv) if fin else None, float(r.maior), float(r.preco))
            for k, r in m.iterrows()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--desde")
    ap.add_argument("--ate", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--ativos", nargs="+", default=["WIN$", FUTURO] + CESTA)
    ap.add_argument("--gravar", action="store_true")
    args = ap.parse_args()
    if not mt5.initialize():
        raise SystemExit(f"MT5: {mt5.last_error()}")
    for s in args.ativos:
        mt5.symbol_select(s, True)

    db = banco.abrir(args.db, "")
    dias = [r[0] for r in db.execute(
        "SELECT DISTINCT date(ts_ms/1000, 'unixepoch') FROM snapshots WHERE symbol='WIN$' AND delta IS NOT NULL "
        "UNION SELECT DISTINCT date(ts_ms/1000, 'unixepoch') FROM agressao_1min ORDER BY 1")]
    dias = [d for d in dias if (not args.desde or d >= args.desde) and d <= args.ate]
    sem_tick = []
    print(f"{'dia':10} {'ativo':7} | {'antigo':>12} {'novo':>12} | unidade")
    for dia in dias:
        a, b = _ms(dia), _ms(dia) + DIA_MS
        for sym in args.ativos:
            if sym == "WIN$":
                tem = db.execute("SELECT count(*) FROM snapshots WHERE symbol='WIN$' AND ts_ms >= ? AND ts_ms < ? "
                                 "AND delta IS NOT NULL", (a, b)).fetchone()[0]
            else:
                tem = db.execute("SELECT count(*) FROM agressao_1min WHERE symbol=? AND ts_ms >= ? AND ts_ms < ?",
                                 (sym, a, b)).fetchone()[0]
            if not tem:
                continue
            tk = ticks_do_dia(sym, dia)
            if tk is None:
                sem_tick.append(f"{dia} {sym}")
                print(f"{dia} {sym:7} | sem ticks no MT5", flush=True)
                continue
            if sym == "WIN$":
                s, linhas = recalcular_win(db, dia, tk)
                print(f"{dia} {sym:7} | {s.delta.sum():>+12,.0f} {s.novo.sum():>+12,.0f} | contratos", flush=True)
            else:
                linhas = recalcular_minutos(sym, dia, tk)
                if sym in CESTA:
                    antigo = db.execute("SELECT sum(fin_compra - fin_venda) FROM agressao_1min WHERE symbol=? AND "
                                        "ts_ms >= ? AND ts_ms < ?", (sym, a, b)).fetchone()[0] or 0
                    novo, un = sum(l[7] - l[8] for l in linhas), "R$ mi"
                    antigo, novo = antigo / 1e6, novo / 1e6
                else:
                    antigo = db.execute("SELECT sum(vol_compra - vol_venda) FROM agressao_1min WHERE symbol=? AND "
                                        "ts_ms >= ? AND ts_ms < ?", (sym, a, b)).fetchone()[0] or 0
                    novo, un = sum(l[4] - l[5] for l in linhas), "contratos"
                print(f"{dia} {sym:7} | {antigo:>+12,.1f} {novo:>+12,.1f} | {un}", flush=True)
            if not args.gravar:
                continue
            with db:
                if sym == "WIN$":
                    db.executemany("UPDATE snapshots SET n_trades=?, vol_compra_agr=?, vol_venda_agr=?, delta=?, "
                                   "saldo_acum=? WHERE sessao=? AND symbol=? AND ts_ms=?", linhas)
                else:
                    db.execute("DELETE FROM agressao_1min WHERE symbol=? AND ts_ms >= ? AND ts_ms < ?", (sym, a, b))
                    db.executemany("INSERT INTO agressao_1min VALUES (?,?,?,?,?,?,?,?,?,?,?)", linhas)
    db.close()
    mt5.shutdown()
    if sem_tick:
        print(f"sem ticks no MT5 (ficaram como estavam): {', '.join(sem_tick)}")
    print("gravado" if args.gravar else "(use --gravar para aplicar)")


if __name__ == "__main__":
    main()
