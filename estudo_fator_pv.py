"""Fator de reversao da mudanca na estrutura preco-volume, adaptado para serie temporal do WIN.

Original (cross-sectional, Alpha101): -1 * DELTA(CORR(HIGH, VOLUME, 5), 5) * RANK(STD(CLOSE, 20))
  CORR/DELTA/STD: janelas moveis de barras; RANK: posicao entre acoes no mesmo dia.
Aqui ha um ativo so, entao o RANK vira a posicao da vol atual no historico do proprio WIN
(percentil movel). Sinal: fator alto -> espera-se alta no horizonte (reversao).

Teste honesto: comparar com a reversao simples (-retorno das ultimas 5 barras), da qual o fator
pode ser so um disfarce, e medir o que sobra dele depois de descontar essa reversao.

  diario  : barras D1 do WIN$ no MT5 (~5 anos), horizontes de 1, 5 e 10 dias
  intraday: barras de 5 min (tabela barras_1m), dentro do pregao, horizontes de 1, 3 e 6 barras

Uso: python estudo_fator_pv.py [--db dados/book.db] [--parte diario|intraday|ambos]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd


def fator(df, janela_rank):
    c = df["high"].rolling(5).corr(df["volume"])
    vol = df["close"].rolling(20).std()
    rank_vol = vol.rolling(janela_rank, min_periods=janela_rank // 2).rank(pct=True)
    return -(c - c.shift(5)) * rank_vol


def spear(a, b):
    m = a.notna() & b.notna() & np.isfinite(a) & np.isfinite(b)
    return a[m].rank().corr(b[m].rank()) if m.sum() > 30 else np.nan, int(m.sum())


def residuo(y, x):
    """parte de y (ranks) nao explicada linearmente por x (ranks)."""
    m = y.notna() & x.notna() & np.isfinite(y) & np.isfinite(x)
    ry, rx = y[m].rank(), x[m].rank()
    b = np.polyfit(rx, ry, 1)
    return pd.Series(ry - np.polyval(b, rx), index=ry.index).reindex(y.index)


def relatorio(df, horizontes, periodo, unidade, escala):
    """df: fator, rev, fut_h (retornos futuros em log), preco. periodo: rotulo p/ estabilidade."""
    print(f"  corr(fator, reversao simples) = {spear(df['fator'], df['rev'])[0]:+.3f}")
    res_f = residuo(df["fator"], df["rev"])
    print(f"  {'h':>4} {'IC fator':>9} {'IC reversao':>12} {'IC fator s/ reversao':>21} {'periodos c/ mesmo sinal':>24}"
          f" {'ganho decil (' + unidade + ')':>18}   n")
    for h in horizontes:
        y = df[f"fut_{h}"]
        ic, n = spear(df["fator"], y)
        ic_r, _ = spear(df["rev"], y)
        ic_res, _ = spear(res_f, y)
        por = df.groupby(periodo).apply(lambda g: spear(g["fator"], g[f"fut_{h}"])[0]).dropna()
        mesmo = (np.sign(por) == np.sign(ic)).mean() if len(por) else np.nan
        m = df["fator"].notna() & y.notna()
        q = df.loc[m, "fator"].quantile([0.1, 0.9])
        alto = y[m & (df["fator"] >= q.iloc[1])].mean()
        baixo = y[m & (df["fator"] <= q.iloc[0])].mean()
        ganho = (alto - baixo) / 2 * escala
        n_ind = n // h
        sig = "*" if abs(ic) > 2 / np.sqrt(max(n_ind, 1)) else " "
        print(f"  {h:>4} {ic:>+8.3f}{sig} {ic_r:>+12.3f} {ic_res:>+21.3f} {mesmo:>17.0%} de {len(por):<4}"
              f" {ganho:>+14.2f}   {n}")


def diario():
    import MetaTrader5 as mt5
    mt5.initialize()
    mt5.symbol_select("WIN$", True)
    r = mt5.copy_rates_from_pos("WIN$", mt5.TIMEFRAME_D1, 1, 6000)  # pos 1: exclui o dia em andamento
    mt5.shutdown()
    df = pd.DataFrame(r)
    df["data"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("data").rename(columns={"real_volume": "volume"})[["high", "close", "volume"]].astype(float)
    df["fator"] = fator(df, 250)
    df["rev"] = -np.log(df["close"]).diff(5)
    for h in (1, 5, 10):
        df[f"fut_{h}"] = np.log(df["close"]).shift(-h) - np.log(df["close"])
    print(f"\n######## DIARIO: WIN$ D1, {df.index.min():%d/%m/%Y} a {df.index.max():%d/%m/%Y} ({len(df)} pregoes) ########")
    print("  (ganho decil = retorno medio de comprar no decil alto e vender no baixo, por operacao, em %)")
    relatorio(df.dropna(subset=["fator"]), (1, 5, 10), df.dropna(subset=["fator"]).index.year, "%", 100)


def intraday(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    b = pd.read_sql("SELECT ts_ms, open, high, low, close, volume FROM barras_1m WHERE symbol='WIN$' ORDER BY ts_ms", con)
    b.index = pd.to_datetime(b["ts_ms"], unit="ms")
    b = b.between_time("09:05", "18:14")
    m5 = b.resample("5min").agg({"high": "max", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    m5 = m5[m5["volume"] > 0]
    dia = m5.index.normalize()
    partes = []
    for _, g in m5.groupby(dia):  # janelas moveis nao atravessam o dia
        g = g.copy()
        c = g["high"].rolling(5).corr(g["volume"])
        g["dcorr"] = -(c - c.shift(5))
        g["vol20"] = g["close"].rolling(20).std()
        g["rev"] = -np.log(g["close"]).diff(5)
        for h in (1, 3, 6):
            g[f"fut_{h}"] = np.log(g["close"]).shift(-h) - np.log(g["close"])
        partes.append(g)
    df = pd.concat(partes)
    # RANK da vol: percentil no historico recente do proprio WIN (~5 pregoes de barras de 5 min)
    df["fator"] = df["dcorr"] * df["vol20"].rolling(500, min_periods=200).rank(pct=True)
    df = df.dropna(subset=["fator"])
    print(f"\n######## INTRADAY: WIN$ 5 min, {df.index.min():%d/%m} a {df.index.max():%d/%m} ({len(df)} barras) ########")
    print("  (ganho decil em pontos do WIN por operacao, antes de custos: ida e volta ~5 pts + taxas)")
    preco = df["close"].median()
    relatorio(df, (1, 3, 6), df.index.to_period("M"), "pts", preco)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--parte", choices=["diario", "intraday", "ambos"], default="ambos")
    args = ap.parse_args()
    if args.parte in ("diario", "ambos"):
        diario()
    if args.parte in ("intraday", "ambos"):
        intraday(args.db)
    print("\n* = |IC| > 2/sqrt(n/h). 'IC fator s/ reversao' = o que sobra do fator depois de tirar a\n"
          "reversao simples; perto de zero = o fator e so reversao disfarcada.")


if __name__ == "__main__":
    main()
