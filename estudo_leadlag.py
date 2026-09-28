"""Estudo de antecipacao (lead-lag) entre o WIN e ativos macro/internacionais, com barras de 1 min.

Dados: tabela barras_1m (backfill_barras.py). Amostra: pregao do WIN, 09:05-18:15 (hora de
Brasilia), so retornos dentro do mesmo dia (rolagens e gaps noturnos ficam de fora).

Para cada ativo X:
  - correlacao cruzada corr(r_WIN(t), r_X(t-k)), k = -3..+5 min (k > 0: X ANTECIPA o WIN)
  - poder preditivo: corr(r_X(t), soma r_WIN(t+1..t+h)) para h = 1, 5, 15, e estabilidade por mes
  - tamanho economico: movimento medio do WIN (pontos) nos h minutos seguintes quando r_X(t)
    esta no decil mais alto/baixo, contra o custo de ~1 tick (5 pts) por lado

Minuto sem negocio em X: preco repetido (retorno 0) por ate 5 min; depois, sem dado.

Uso: python estudo_leadlag.py [--db dados/book.db] [--ativos IB:ES IB:NQ ...] [--csv saida.csv]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

ALVO = "WIN$"
PADRAO = ["IB:ES", "IB:NQ", "IB:VIX", "IB:DX", "IB:ZN", "IB:CL", "IB:GC", "IB:EWZ", "WDO$"]
INICIO, FIM = "09:05", "18:15"
LAGS = range(-3, 6)
HORIZ = (1, 5, 15)
FAIXAS = [("09:05", "10:30", "antes de NY"), ("10:30", "11:30", "abertura NY"),
          ("11:30", "16:00", "meio do dia"), ("16:00", "18:15", "fim/fechamento NY")]


def carregar(con, symbol):
    df = pd.read_sql("SELECT ts_ms, close FROM barras_1m WHERE symbol=? ORDER BY ts_ms", con, params=(symbol,))
    s = pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["ts_ms"], unit="ms"), name=symbol)
    return s[~s.index.duplicated()]


def grade_pregao(alvo):
    """Minutos do pregao nos dias em que o WIN negociou."""
    dias = pd.Index(alvo.index.normalize().unique())
    mins = pd.timedelta_range(INICIO + ":00", FIM + ":00", freq="1min")
    return pd.DatetimeIndex([d + m for d in dias for m in mins])


def retornos(preco, grade, limite=5):
    """log-retorno de 1 min na grade; ffill de ate `limite` minutos; nada atravessa o dia."""
    ini = grade.min().normalize()
    p = preco[preco.index >= ini - pd.Timedelta(days=1)]
    completo = p.reindex(p.index.union(grade)).sort_index()
    completo = completo.groupby(completo.index.normalize()).ffill(limit=limite)
    lp = np.log(completo.reindex(grade))
    r = lp.groupby(lp.index.normalize()).diff()
    return r


def corr(a, b):
    m = a.notna() & b.notna()
    if m.sum() < 200 or a[m].std() == 0 or b[m].std() == 0:
        return np.nan, int(m.sum())
    return float(np.corrcoef(a[m], b[m])[0, 1]), int(m.sum())


def futuro(r, h):
    """soma dos retornos t+1..t+h dentro do mesmo dia."""
    dia = r.index.normalize()
    return sum(r.groupby(dia).shift(-i) for i in range(1, h + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--ativos", nargs="+", default=PADRAO)
    ap.add_argument("--csv")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    win = carregar(con, ALVO)
    grade = grade_pregao(win)
    rw = retornos(win, grade, limite=1)
    pts_fut = {h: (win.reindex(grade).groupby(grade.normalize()).shift(-h) - win.reindex(grade)) for h in HORIZ}
    rw_fut = {h: futuro(rw, h) for h in HORIZ}
    meses = rw.index.to_period("M")
    dias = grade.normalize().nunique()
    print(f"WIN: {dias} pregoes, {rw.notna().sum()} retornos de 1 min "
          f"({grade.min():%d/%m/%Y} a {grade.max():%d/%m/%Y})\n")

    linhas = []
    for x in args.ativos:
        px = carregar(con, x)
        if px.empty:
            print(f"{x}: sem dados em barras_1m\n")
            continue
        rx = retornos(px, grade)
        cob = rx.notna().mean()
        # r_X(t-k) dentro do mesmo dia (k < 0 olha o futuro de X: o WIN antecipando X)
        cc = {k: corr(rw, rx.groupby(rx.index.normalize()).shift(k))[0] for k in LAGS}
        n = int((rw.notna() & rx.notna()).sum())
        lim = 2 / np.sqrt(n) if n else np.nan
        print(f"=== {x} | cobertura {cob:.0%} dos minutos | n={n} | limiar ~{lim:.3f}")
        print("  corr(WIN_t, X_t-k):  " + "  ".join(f"k={k:+d}:{cc[k]:+.3f}" for k in LAGS))

        # poder preditivo e estabilidade por mes
        for h in HORIZ:
            c, nn = corr(rx, rw_fut[h])
            por_mes = [corr(rx[meses == m], rw_fut[h][meses == m])[0] for m in sorted(set(meses))]
            por_mes = [v for v in por_mes if not np.isnan(v)]
            mesmo_sinal = np.mean(np.sign(por_mes) == np.sign(c)) if por_mes and not np.isnan(c) else np.nan
            # decis: movimento medio do WIN em pontos
            m = rx.notna() & pts_fut[h].notna()
            q = rx[m].quantile([0.1, 0.9])
            baixo = pts_fut[h][m & (rx <= q.iloc[0])].mean()
            alto = pts_fut[h][m & (rx >= q.iloc[1])].mean()
            # comprar WIN depois de X no decil alto e vender depois do baixo (sinal oposto se corr < 0):
            # ganho bruto medio por operacao, ja livre da tendencia do periodo
            ganho = (alto - baixo) / 2 * (1 if (c or 0) >= 0 else -1)
            print(f"  prever WIN t+1..t+{h:<2}: corr {c:+.3f} | mesmo sinal em {mesmo_sinal:.0%} dos {len(por_mes)} meses"
                  f" | WIN apos decil baixo {baixo:+.1f} / alto {alto:+.1f} pts -> ganho bruto {ganho:+.1f} pts/op")
            linhas.append(dict(ativo=x, horizonte=h, corr=c, n=nn, meses_mesmo_sinal=mesmo_sinal,
                               win_pts_decil_baixo=baixo, win_pts_decil_alto=alto, ganho_bruto_pts=ganho,
                               corr_contemporanea=cc[0], cobertura=cob))
        # por faixa de horario (h=1)
        hh = rx.index.strftime("%H:%M")
        partes = []
        for a, b, nome in FAIXAS:
            sel = (hh >= a) & (hh < b)
            partes.append(f"{nome} {corr(rx[sel], rw_fut[1][sel])[0]:+.3f}")
        print("  prever t+1 por horario: " + " | ".join(partes) + "\n")

    if args.csv:
        pd.DataFrame(linhas).to_csv(args.csv, index=False)
        print(f"resumo salvo em {args.csv}")
    print("Leitura: k>0 com corr alta = X antecipa o WIN. 'prever' usa so informacao disponivel em t.\n"
          "Limiar 2/sqrt(n) ignora autocorrelacao; confie mais na estabilidade entre meses e no tamanho\n"
          "em pontos: o ganho bruto por operacao precisa superar o custo de ida e volta (~5 pts de spread\n"
          "+ emolumentos/corretagem) com folga.")


if __name__ == "__main__":
    main()
