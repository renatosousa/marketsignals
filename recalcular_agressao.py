"""Recalcula pelos ticks do MT5 o saldo de agressao gravado antes da correcao de 27/10/2026 (ver ticks_trade.py).

Os coletores contavam como compra os ticks sem agressor (as duas flags) e descartavam os ticks repetidos no mesmo
milissegundo (~45% do volume do WIN). Aqui, para cada dia com ticks no MT5:
  snapshots      WIN$: n_trades, vol_compra_agr, vol_venda_agr, delta (ticks entre o snapshot anterior e o atual) e
                 saldo_acum (acumulado da sessao do coletor, como antes)
  agressao_1min  WDO$: as linhas do dia sao trocadas por uma por minuto (sessao 'recalc_AAAA-MM-DD')
So o pregao continuo (a partir de 09:05) e so ticks com exatamente uma flag, como os coletores atuais.
O MT5 guarda ticks so de algumas semanas: dia sem tick fica como esta e e listado. Na aplicacao de 28/10/2026 os
snapshots do WIN anteriores a 13/10 (sem ticks no MT5) tiveram a agressao anulada; o WDO foi recalculado desde 08/10.

Uso: python recalcular_agressao.py [--db dados/book.db] [--desde AAAA-MM-DD] [--ate AAAA-MM-DD] [--gravar]
     sem --gravar so compara o antigo com o recalculado. Padrao: todos os dias com coleta ate ontem.
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import banco
from ticks_trade import BUY, INICIO_MS, SELL

DIA_MS = 86_400_000


def _ms(dia, hhmm="00:00"):
    return int(pd.Timestamp(f"{dia} {hhmm}", tz="UTC").timestamp() * 1000)


def ticks_do_dia(sym, dia):
    """DataFrame (ms, sinal, vol, preco) do pregao continuo; sinal +1 compra, -1 venda, 0 sem agressor. None sem ticks."""
    ini = datetime.fromisoformat(f"{dia} 08:30").replace(tzinfo=timezone.utc)
    t = mt5.copy_ticks_range(sym, ini, ini + timedelta(hours=10, minutes=30), mt5.COPY_TICKS_TRADE)
    if t is None or len(t) == 0:
        return None
    fl = t["flags"].astype(np.int64)
    c, v = (fl & BUY) > 0, (fl & SELL) > 0
    vol = np.where(t["volume_real"] > 0, t["volume_real"], t["volume"]).astype(float)
    d = pd.DataFrame(dict(ms=t["time_msc"].astype(np.int64), sinal=np.where(c & ~v, 1, np.where(v & ~c, -1, 0)),
                          vol=vol, preco=t["last"].astype(float)))
    return d[d.ms % DIA_MS >= INICIO_MS].reset_index(drop=True)


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


def recalcular_wdo(dia, tk):
    """Linhas de agressao_1min do WDO$ (uma por minuto)."""
    tk = tk.assign(minuto=tk.ms // 60000 * 60000)
    g = tk.groupby("minuto")
    m = pd.DataFrame(dict(
        n=g.size(), c=g.apply(lambda x: x.vol[x.sinal > 0].sum()), v=g.apply(lambda x: x.vol[x.sinal < 0].sum()),
        nt=g.apply(lambda x: x.vol[x.sinal == 0].sum()), maior=g.vol.max(), preco=g.preco.last()))
    return [(int(k), f"recalc_{dia}", "WDO$", int(r.n), float(r.c), float(r.v), float(r.nt), None, None,
             float(r.maior), float(r.preco)) for k, r in m.iterrows()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--desde")
    ap.add_argument("--ate", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--gravar", action="store_true")
    args = ap.parse_args()
    if not mt5.initialize():
        raise SystemExit(f"MT5: {mt5.last_error()}")
    for s in ("WIN$", "WDO$"):
        mt5.symbol_select(s, True)

    db = banco.abrir(args.db, "")
    dias = [r[0] for r in db.execute(
        "SELECT DISTINCT date(ts_ms/1000, 'unixepoch') FROM snapshots WHERE symbol='WIN$' AND delta IS NOT NULL "
        "UNION SELECT DISTINCT date(ts_ms/1000, 'unixepoch') FROM agressao_1min WHERE symbol='WDO$' ORDER BY 1")]
    dias = [d for d in dias if (not args.desde or d >= args.desde) and d <= args.ate]
    sem_tick = []
    print(f"{'dia':10} | {'WIN saldo antigo':>16} {'novo':>10} {'snaps':>6} | {'WDO saldo antigo':>16} {'novo':>9}")
    for dia in dias:
        tw, td = ticks_do_dia("WIN$", dia), ticks_do_dia("WDO$", dia)
        txt = f"{dia} |"
        if tw is None:
            txt += f" {'sem ticks no MT5':>34} |"
            linhas_win = []
        else:
            s, linhas_win = recalcular_win(db, dia, tw)
            txt += (f" {s.delta.sum():>+16,.0f} {s.novo.sum():>+10,.0f} {len(s):>6} |" if len(s)
                    else f" {'sem snapshots':>34} |")
        antigo = db.execute("SELECT sum(vol_compra - vol_venda), count(*) FROM agressao_1min WHERE symbol='WDO$' "
                            "AND ts_ms >= ? AND ts_ms < ?", (_ms(dia), _ms(dia) + DIA_MS)).fetchone()
        linhas_wdo = [] if td is None or not antigo[1] else recalcular_wdo(dia, td)
        if td is None:
            txt += f" {'sem ticks no MT5':>26}"
        elif not antigo[1]:
            txt += f" {'sem coleta':>26}"
        else:
            txt += f" {antigo[0] or 0:>+16,.0f} {sum(l[4] - l[5] for l in linhas_wdo):>+9,.0f}"
        print(txt, flush=True)
        if tw is None and td is None:
            sem_tick.append(dia)
        if not args.gravar:
            continue
        a, b = _ms(dia), _ms(dia) + DIA_MS
        with db:
            if linhas_win:
                db.executemany("UPDATE snapshots SET n_trades=?, vol_compra_agr=?, vol_venda_agr=?, delta=?, "
                               "saldo_acum=? WHERE sessao=? AND symbol=? AND ts_ms=?", linhas_win)
            if linhas_wdo:
                db.execute("DELETE FROM agressao_1min WHERE symbol='WDO$' AND ts_ms >= ? AND ts_ms < ?", (a, b))
                db.executemany("INSERT INTO agressao_1min VALUES (?,?,?,?,?,?,?,?,?,?,?)", linhas_wdo)
    db.close()
    mt5.shutdown()
    if sem_tick:
        print(f"sem ticks no MT5 (ficaram como estavam): {', '.join(sem_tick)}")
    print("gravado" if args.gravar else "(use --gravar para aplicar)")


if __name__ == "__main__":
    main()
