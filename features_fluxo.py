"""Variaveis diarias (features) para estimar o fluxo de estrangeiros do dia.

Le agressao_1min (coletor_agressao.py) e as barras de 1 min (WIN$, WDO$, DI1F29) e grava uma linha por
pregao em features_fluxo_dia. O rotulo (saldo oficial da B3, D+2) fica em fluxo_diario e e cruzado em
estudo_nowcast.py.

Variaveis:
  cobertura_acoes          fracao dos minutos 10:00-17:55 em que a cesta foi observada (>= 8 de 12 ativos)
  cesta_fin_bruto_mi       compra+venda agressoras da cesta (R$ mi)
  cesta_fin_liq_mi         compra - venda agressoras da cesta (R$ mi)      <- candidata principal
  cesta_liq_pct            liquido / bruto (%)
  cesta_liq_abertura_mi / _meio_mi / _fim_mi   liquido por faixa (10:00-10:30, 10:30-17:00, 17:00-18:25)
  n_acoes_comprando        quantas das 12 tiveram liquido comprador
  lote_medio_fin           R$ medio por negocio da cesta
  wdo_liq_contratos / wdo_liq_pct   agressao liquida do WDO
  wdo_ret_pct, win_ret_pct, win_amp_pct, di29_bps   retorno/amplitude do dia (barras)
  win_vol_rel              volume do WIN no dia / mediana dos 20 pregoes anteriores
  captura_vol              volume capturado pelo coletor / volume real das barras de 1 min (qualidade; ~0,5)
  cesta_fin_liq_esc_mi     liquido da cesta CORRIGIDO pelo volume real: em cada minuto, (compra-venda)/total
                           capturado x volume da barra x preco (usa so a proporcao amostrada, nao o nivel)
  cesta_liq_esc_pct        liquido corrigido / volume financeiro real da cesta (%)

Uso: python features_fluxo.py [--db dados/book.db] [--data AAAA-MM-DD]   (sem --data: todos os dias com coleta)
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

import banco
from estudo_fluxo import diario

SCHEMA = """
CREATE TABLE IF NOT EXISTS features_fluxo_dia (
    data TEXT PRIMARY KEY, cobertura_acoes REAL, cesta_fin_bruto_mi REAL, cesta_fin_liq_mi REAL,
    cesta_liq_pct REAL, cesta_liq_abertura_mi REAL, cesta_liq_meio_mi REAL, cesta_liq_fim_mi REAL,
    n_acoes_comprando INTEGER, lote_medio_fin REAL, wdo_liq_contratos REAL, wdo_liq_pct REAL,
    wdo_ret_pct REAL, win_ret_pct REAL, win_amp_pct REAL, di29_bps REAL, win_vol_rel REAL,
    captura_vol REAL, cesta_fin_liq_esc_mi REAL, cesta_liq_esc_pct REAL
);
"""
COLUNAS = ["data", "cobertura_acoes", "cesta_fin_bruto_mi", "cesta_fin_liq_mi", "cesta_liq_pct",
           "cesta_liq_abertura_mi", "cesta_liq_meio_mi", "cesta_liq_fim_mi", "n_acoes_comprando", "lote_medio_fin",
           "wdo_liq_contratos", "wdo_liq_pct", "wdo_ret_pct", "win_ret_pct", "win_amp_pct", "di29_bps", "win_vol_rel",
           "captura_vol", "cesta_fin_liq_esc_mi", "cesta_liq_esc_pct"]
MIN_ACOES = 8
MINUTOS_ESPERADOS = 475  # 10:00 a 17:55


def construir_dia(con, data, barras):
    t0 = int(pd.Timestamp(f"{data} 00:00", tz="UTC").timestamp() * 1000)
    h = lambda hhmm: t0 + (int(hhmm[:2]) * 60 + int(hhmm[3:])) * 60000
    a = pd.read_sql("SELECT ts_ms, symbol, n, vol_compra, vol_venda, vol_neutro, fin_compra, fin_venda, preco "
                    "FROM agressao_1min WHERE ts_ms BETWEEN ? AND ?", con, params=(h("09:00"), h("18:30")))
    if a.empty:
        return None
    # uma linha por (symbol, minuto): se houve mais de uma sessao no dia, fica a de mais negocios
    a = a.sort_values("n").drop_duplicates(["symbol", "ts_ms"], keep="last")
    acoes = a[a.symbol != "WDO$"].copy()
    wdo = a[a.symbol == "WDO$"]
    acoes["liq"] = (acoes.fin_compra - acoes.fin_venda) / 1e6
    acoes["bruto"] = (acoes.fin_compra + acoes.fin_venda) / 1e6
    por_min = acoes[(acoes.ts_ms >= h("10:00")) & (acoes.ts_ms < h("17:56"))].groupby("ts_ms").symbol.nunique()
    cobertura = float((por_min >= MIN_ACOES).sum() / MINUTOS_ESPERADOS)
    faixa = lambda lo, hi: float(acoes[(acoes.ts_ms >= h(lo)) & (acoes.ts_ms < h(hi))].liq.sum())
    liq, bruto = float(acoes.liq.sum()), float(acoes.bruto.sum())
    por_ativo = acoes.groupby("symbol").liq.sum()
    r = dict(data=data, cobertura_acoes=cobertura, cesta_fin_bruto_mi=bruto, cesta_fin_liq_mi=liq,
             cesta_liq_pct=liq / bruto * 100 if bruto else np.nan,
             cesta_liq_abertura_mi=faixa("10:00", "10:30"), cesta_liq_meio_mi=faixa("10:30", "17:00"),
             cesta_liq_fim_mi=faixa("17:00", "18:26"), n_acoes_comprando=int((por_ativo > 0).sum()),
             lote_medio_fin=float(acoes.bruto.sum() * 1e6 / acoes.n.sum()) if acoes.n.sum() else np.nan)
    # correcao pelo volume real do minuto (barras de 1 min das mesmas acoes)
    bar = pd.read_sql("SELECT symbol, ts_ms, volume AS vol_barra FROM barras_1m WHERE ts_ms BETWEEN ? AND ? "
                      "AND symbol != 'WIN$' AND symbol != 'WDO$' AND symbol NOT LIKE 'DI1%' AND symbol NOT LIKE 'IB:%'",
                      con, params=(h("09:00"), h("18:30")))
    m = acoes.merge(bar, on=["symbol", "ts_ms"], how="inner")
    m["tot"] = m.vol_compra + m.vol_venda + m.vol_neutro
    m = m[(m.tot > 0) & (m.vol_barra > 0)]
    if len(m):
        share = (m.vol_compra - m.vol_venda) / m.tot
        esc = float((share * m.vol_barra * m.preco).sum() / 1e6)
        real = float((m.vol_barra * m.preco).sum() / 1e6)
        r.update(captura_vol=float(m.tot.sum() / m.vol_barra.sum()), cesta_fin_liq_esc_mi=esc,
                 cesta_liq_esc_pct=esc / real * 100 if real else np.nan)
    else:
        r.update(captura_vol=np.nan, cesta_fin_liq_esc_mi=np.nan, cesta_liq_esc_pct=np.nan)
    wl = float((wdo.vol_compra - wdo.vol_venda).sum()) if not wdo.empty else np.nan
    wb = float((wdo.vol_compra + wdo.vol_venda).sum()) if not wdo.empty else np.nan
    r.update(wdo_liq_contratos=wl, wdo_liq_pct=wl / wb * 100 if wb else np.nan)
    win, wdo_b, di = barras["WIN$"], barras["WDO$"], barras["DI1F29"]
    r["win_ret_pct"] = float(win.close.pct_change().get(data, np.nan) * 100)
    r["wdo_ret_pct"] = float(wdo_b.close.pct_change().get(data, np.nan) * 100)
    r["di29_bps"] = float(di.close.diff().get(data, np.nan) * 100)
    r["win_amp_pct"] = float((win.high / win.low - 1).get(data, np.nan) * 100)
    return r


def construir(db_path, data=None):
    """Recalcula features de um dia (ou de todos os dias com coleta). -> numero de linhas gravadas."""
    con = banco.abrir(db_path, SCHEMA)
    try:
        dias = [data] if data else [r[0] for r in con.execute(
            "SELECT DISTINCT date(ts_ms/1000, 'unixepoch') FROM agressao_1min ORDER BY 1")]
        if not dias:
            return 0
        leitura = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        barras = {s: diario(leitura, s) for s in ("WIN$", "WDO$", "DI1F29")}
        # volume do WIN relativo a mediana dos 20 pregoes anteriores
        vol = pd.read_sql("SELECT ts_ms, volume FROM barras_1m WHERE symbol='WIN$' ORDER BY ts_ms", leitura)
        vol.index = pd.to_datetime(vol.ts_ms, unit="ms")
        vd = vol.between_time("09:00", "18:25").volume.groupby(lambda t: t.date().isoformat()).sum()
        vrel = vd / vd.shift(1).rolling(20, min_periods=10).median()
        linhas = []
        for d in dias:
            r = construir_dia(leitura, d, barras)
            if r:
                r["win_vol_rel"] = float(vrel.get(d, np.nan))
                linhas.append(tuple(None if (isinstance(r[c], float) and np.isnan(r[c])) else r[c] for c in COLUNAS))
        leitura.close()
        con.executemany(f"INSERT OR REPLACE INTO features_fluxo_dia VALUES ({','.join('?' * len(COLUNAS))})", linhas)
        con.commit()
        return len(linhas)
    finally:
        con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(banco.Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--data")
    a = ap.parse_args()
    n = construir(a.db, a.data)
    print(f"{n} dia(s) de features gravados")
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    print(pd.read_sql("SELECT * FROM features_fluxo_dia ORDER BY data DESC LIMIT 10", c).round(2).T.to_string())
