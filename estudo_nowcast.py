"""Cruza as variaveis intradiarias (features_fluxo_dia) com o saldo OFICIAL do estrangeiro (fluxo_diario).

Cada dia adiciona uma observacao quando o rotulo da B3 chega (D+2). Regra de maturidade: com menos de
~60 observacoes completas nada aqui deve virar modelo; serve para acompanhar se a relacao existe.

Criterio para uma variavel entrar no modelo (fora da amostra, walk-forward): acertar o sinal do saldo em
mais de 60% dos dias e ter Spearman acima de ~0,4 de forma estavel. Abaixo disso, ela fica de fora.

Uso: python estudo_nowcast.py [--db dados/book.db] [--min-cobertura 0.7]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

import fluxo_estrangeiro as fe

FEATURES = ["cesta_fin_liq_mi", "cesta_liq_pct", "cesta_liq_abertura_mi", "cesta_liq_meio_mi", "cesta_liq_fim_mi",
            "n_acoes_comprando", "lote_medio_fin", "wdo_liq_pct", "wdo_ret_pct", "win_ret_pct", "di29_bps",
            "win_amp_pct", "win_vol_rel"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--min-cobertura", type=float, default=0.7)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    f = pd.read_sql("SELECT * FROM features_fluxo_dia ORDER BY data", con).set_index("data")
    rot = fe.serie(a.db)
    rot = (rot.saldo_mil / 1e6).rename("saldo_bi")
    d = f.join(rot, how="left")
    print(f"dias com variaveis: {len(f)} | com rotulo oficial: {int(d.saldo_bi.notna().sum())}")
    print(d[["cobertura_acoes", "cesta_fin_liq_mi", "saldo_bi", "win_ret_pct"]].round(2).to_string())
    ok = d[(d.cobertura_acoes >= a.min_cobertura) & d.saldo_bi.notna()]
    print(f"\nobservacoes completas (cobertura >= {a.min_cobertura:.0%} e rotulo): {len(ok)}")
    if len(ok) < 8:
        print("poucas observacoes para qualquer correlacao; continue acumulando (meta: >= 60).")
        return
    print(f"\n{'variavel':26}{'Spearman':>10}{'acerto de sinal':>17}")
    for c in FEATURES:
        x, y = ok[c], ok.saldo_bi
        m = x.notna()
        if m.sum() < 8:
            continue
        rho = x[m].rank().corr(y[m].rank())
        acerto = (np.sign(x[m]) == np.sign(y[m])).mean()
        print(f"{c:26}{rho:>+10.2f}{acerto:>16.0%}")
    print("\nCom n pequeno, |rho| < 2/sqrt(n) e ruido; ver criterio no cabecalho do arquivo.")


if __name__ == "__main__":
    main()
