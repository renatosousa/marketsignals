"""Testa se as metricas do livro/fluxo (book.db) antecipam o movimento do preco.

Para cada horizonte H (segundos), calcula o retorno futuro do mid (em pontos) e mede, para
cada feature: correlacao de Pearson e de Spearman (rank), e o hit-rate direcional
(sinal da feature == sinal do retorno, ignorando retorno zero). Tambem mostra o retorno
medio por quintil da feature.

Uso: python analisar_sinais.py [--db dados/book.db] [--symbol WIN$] [--horizontes 5 15 30 60]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

LACUNA_MAX_MS = 300_000  # salto > 5 min entre snapshots consecutivos = ts antigo/pausa
TOLERANCIA_S = 3         # tolerancia para achar o snapshot do horizonte
FEATURES = ["imbalance_total", "imbalance_near", "imbalance_pond", "micro_desvio", "delta", "delta_15s"]


def carregar(db, symbol):
    con = sqlite3.connect(db)
    df = pd.read_sql("SELECT * FROM snapshots WHERE symbol = ? ORDER BY sessao, ts_ms", con, params=(symbol,))
    con.close()
    if df.empty:
        raise SystemExit(f"nenhum snapshot de {symbol} em {db}")
    df["micro_desvio"] = df["microprice"] - df["mid"]
    n0 = len(df)
    df = df[df["spread"] > 0]  # livro cruzado/travado (leilao, abertura): metricas sem sentido
    # snapshots iniciais com hora velha (ultimo tick pre-abertura): o proximo ts esta muito a frente
    salto = df.groupby("sessao")["ts_ms"].diff(-1).abs()
    df = df[~(salto > LACUNA_MAX_MS)]
    if n0 - len(df):
        print(f"(descartados {n0 - len(df)} snapshots: spread<=0 ou hora velha)")
    return df.reset_index(drop=True)


def montar(df, h):
    partes = []
    for _, g in df.groupby("sessao"):
        g = g.copy()
        g["delta_15s"] = g["delta"].rolling(15, min_periods=5).sum()
        t = g.set_index(pd.to_datetime(g["ts_ms"], unit="ms"))
        # mid daqui a h segundos (primeiro snapshot com ts >= ts + h)
        alvo = t.index + pd.Timedelta(seconds=h)
        pos = t.index.searchsorted(alvo)
        ok = pos < len(t)
        # so vale se o snapshot futuro esta perto do alvo (nao atravessa lacunas/pausas na coleta)
        ts = t.index.to_numpy()
        ok[ok] = (ts[pos[ok]] - alvo.to_numpy()[ok]) <= np.timedelta64(int(TOLERANCIA_S * 1000), "ms")
        g["ret_fut"] = np.nan
        g.loc[ok, "ret_fut"] = t["mid"].to_numpy()[pos[ok]] - g["mid"].to_numpy()[ok]
        partes.append(g)
    return pd.concat(partes).dropna(subset=["ret_fut"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--symbol", default="WIN$")
    ap.add_argument("--horizontes", type=int, nargs="+", default=[5, 15, 30, 60])
    args = ap.parse_args()

    df = carregar(args.db, args.symbol)
    dur = (df["ts_ms"].max() - df["ts_ms"].min()) / 60000
    print(f"{len(df)} snapshots, {df['sessao'].nunique()} sessao(oes), {dur:.1f} min, "
          f"mid de {df['mid'].min():.0f} a {df['mid'].max():.0f}\n")

    for h in args.horizontes:
        d = montar(df, h)
        n = len(d)
        # amostras sobrepostas: nº efetivo de observacoes independentes ~ n / h
        n_ef = max(n // h, 1)
        print(f"=== horizonte {h}s | n={n} (independentes ~{n_ef}) | ret_fut medio {d['ret_fut'].mean():+.2f} "
              f"| desvio {d['ret_fut'].std():.1f} pts ===")
        linhas = []
        for f in FEATURES:
            x, y = d[f], d["ret_fut"]
            if x.isna().all() or x.std() == 0:  # ex.: fluxo de agressao em coleta --sem-trades
                continue
            pear = x.corr(y)
            spear = x.rank().corr(y.rank())
            m = (y != 0) & (x != 0)
            hit = (np.sign(x[m]) == np.sign(y[m])).mean() if m.sum() else np.nan
            # IC "significativo" grosseiro: |r| > 2/sqrt(n_ef)
            sig = "*" if abs(spear) > 2 / np.sqrt(n_ef) else ""
            linhas.append((f, pear, spear, hit, m.sum(), sig))
        print(f"{'feature':<18}{'pearson':>9}{'spearman':>10}{'hit-rate':>10}{'n':>7}  sig")
        for f, p, s, hr, k, sig in linhas:
            print(f"{f:<18}{p:>+9.3f}{s:>+10.3f}{hr:>10.1%}{k:>7}  {sig}")
        # quintis do imbalance ponderado
        try:
            q = pd.qcut(d["imbalance_pond"], 5, labels=False, duplicates="drop")
            print("ret medio por quintil de imbalance_pond (Q0=mais vendedor ... Q4=mais comprador):",
                  d.groupby(q)["ret_fut"].mean().round(2).to_dict())
        except ValueError:
            pass
        print()
    print("* = |spearman| > 2/sqrt(n independentes): indicio, nao prova. Com poucos minutos de dados "
          "qualquer resultado e ruido; repita em varios dias/regimes.")


if __name__ == "__main__":
    main()
