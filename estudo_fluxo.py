"""Como o fluxo estrangeiro (B3, D+2) se relaciona com WIN, WDO e DI.

Junta fluxo_diario (fluxo_estrangeiro.py) com os retornos diarios de barras_1m (fechamento do pregao
anterior ao fechamento do dia). Mostra a tabela, as correlacoes (Pearson e Spearman) no mesmo dia e com
defasagens, e repete sem o dia de 05/10 (outlier: +R$ 10 bi, WIN +7,6%).

Importante para uso em modelo: o fluxo do dia T so e conhecido em T+2. Logo:
  - mesmo dia (lag 0)  = descreve a relacao, NAO e usavel para prever
  - lag 2 (fluxo de T-2 explicando o dia T) e o que se sabe de manha

Uso: python estudo_fluxo.py [--db dados/book.db]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

import fluxo_estrangeiro as fe


def diario(con, sym):
    df = pd.read_sql("SELECT ts_ms, high, low, close FROM barras_1m WHERE symbol=? ORDER BY ts_ms", con, params=(sym,))
    df.index = pd.to_datetime(df.ts_ms, unit="ms")
    h = df.between_time("09:00", "18:25")
    g = h.groupby(h.index.date).agg(close=("close", "last"), high=("high", "max"), low=("low", "min"), n=("close", "size"))
    g = g[g.n >= 300]
    g.index = pd.Index([d.isoformat() for d in g.index])
    return g


def corr(a, b):
    m = a.notna() & b.notna()
    if m.sum() < 8:
        return np.nan, np.nan, int(m.sum())
    return float(np.corrcoef(a[m], b[m])[0, 1]), float(a[m].rank().corr(b[m].rank())), int(m.sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    est = fe.serie(a.db)
    inst = fe.serie(a.db, "Institucionais")["saldo_mil"]
    ind = fe.serie(a.db, "Investidores Individuais")["saldo_mil"]
    win, wdo, di29 = diario(con, "WIN$"), diario(con, "WDO$"), diario(con, "DI1F29")

    d = pd.DataFrame(index=est.index)
    d["saldo_bi"] = est.saldo_mil / 1e6
    d["bruto_bi"] = (est.compras_mil + est.vendas_mil) / 1e6
    d["saldo_pct"] = est.saldo_mil / (est.compras_mil + est.vendas_mil) * 100
    d["inst_bi"] = inst / 1e6
    d["indiv_bi"] = ind / 1e6
    d["win_%"] = win.close.pct_change().reindex(d.index) * 100
    d["wdo_%"] = wdo.close.pct_change().reindex(d.index) * 100
    d["di29_bps"] = di29.close.diff().reindex(d.index) * 100
    d["amp_win_%"] = ((win.high / win.low - 1) * 100).reindex(d.index)
    print(f"{len(d)} pregoes com fluxo ({d.index.min()} a {d.index.max()})\n")
    print(d.round(2).to_string())

    def bloco(titulo, dd):
        print(f"\n=== {titulo} (n={len(dd)}) ===")
        print(f"{'':32}{'Pearson':>9}{'Spearman':>10}{'n':>5}")
        for nome, x, y in (
            ("saldo x WIN (mesmo dia)", dd.saldo_bi, dd["win_%"]),
            ("saldo x WDO (mesmo dia)", dd.saldo_bi, dd["wdo_%"]),
            ("saldo x DI1F29 bps (mesmo dia)", dd.saldo_bi, dd.di29_bps),
            ("|saldo| x amplitude do WIN", dd.saldo_bi.abs(), dd["amp_win_%"]),
            ("bruto (vol. estrang.) x amplitude", dd.bruto_bi, dd["amp_win_%"]),
            ("saldo x institucionais", dd.saldo_bi, dd.inst_bi),
            ("saldo x individuais", dd.saldo_bi, dd.indiv_bi),
        ):
            p, s, n = corr(x, y)
            print(f"{nome:32}{p:>+9.2f}{s:>+10.2f}{n:>5}")
        # defasagens que existem de manha: saldo de T-2 explicando o dia T
        ds = pd.DataFrame({"saldo": dd.saldo_bi, "acum5": dd.saldo_bi.rolling(5).sum()})
        for lag in (1, 2, 3):
            for alvo, nome in (("win_%", "WIN"), ("wdo_%", "WDO")):
                p, s, n = corr(ds.saldo.shift(lag), dd[alvo])
                print(f"saldo(T-{lag}) x {nome}(T){'':14}{p:>+9.2f}{s:>+10.2f}{n:>5}")
        p, s, n = corr(ds.acum5.shift(2), dd["win_%"])
        print(f"{'acum 5d(T-2) x WIN(T)':32}{p:>+9.2f}{s:>+10.2f}{n:>5}")

    bloco("todos os dias", d)
    bloco("sem 05/10 (pos-1o turno)", d.drop(index="2026-10-05", errors="ignore"))
    print("\nLimiar de significancia grosseiro (n=21): |rho| > 0,44; (n=20): 0,45. Amostra pequena: use como descricao.")


if __name__ == "__main__":
    main()
