"""Fator de reversao da estrutura preco-volume na forma ORIGINAL (cross-sectional), acoes da B3.

  F = -1 * DELTA(CORR(HIGH, VOLUME, 5), 5) * RANK(STD(CLOSE, 20))   (RANK entre acoes, no dia)

Universo: acoes liquidas da B3 (lista abaixo), filtradas a cada dia por volume financeiro medio de
20 dias >= LIQ_MIN (so o que dava para operar naquele dia). Barras D1 do MT5 (~5 anos).

Medidas:
  - Rank IC diario (Spearman entre acoes) p/ 1, 5 e 10 dias; media, ICIR, t (amostras sem
    sobreposicao), % de dias positivos e por ano
  - IC do fator depois de tirar reversao simples (-ret 5d) e o rank de vol (regressao por dia)
  - quintis rebalanceados a cada 5 dias (entra na abertura seguinte, sai no fechamento do 5o dia),
    peso igual; long-short Q5-Q1 e Q5 contra a media do universo, bruto e com custos

Cuidados: barras do MT5 nao ajustadas por proventos (dias com |ret| > 35% sao descartados como
provavel desdobramento); lista de acoes de hoje (vies de sobrevivencia).

Uso: python estudo_fator_b3.py [--baixar] [--custo 0.0010] [--liq-min 30e6]
     --baixar busca as barras no MT5 e salva em dados/b3_d1.parquet|csv (rodar fora do pregao)
"""
import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

ARQ = Path(__file__).parent / "dados" / "b3_d1.csv"
ACOES = """ABEV3 ALOS3 ALPA4 ASAI3 AURE3 AZUL4 AZZA3 B3SA3 BBAS3 BBDC3 BBDC4 BBSE3 BEEF3 BHIA3 BPAC11 BRAP4
BRAV3 BRFS3 BRKM5 CASH3 CBAV3 CMIG4 CMIN3 COGN3 CPFE3 CPLE3 CPLE6 CRFB3 CSAN3 CSMG3 CSNA3 CVCB3 CXSE3
CYRE3 DIRR3 DXCO3 ECOR3 EGIE3 ELET3 ELET6 EMBR3 ENEV3 ENGI11 EQTL3 EZTC3 FLRY3 GGBR4 GMAT3 GOAU4 HAPV3
HYPE3 IGTI11 INTB3 IRBR3 ISAE4 ITSA4 ITUB4 JHSF3 KLBN11 LREN3 LWSA3 MGLU3 MOTV3 MOVI3 MRFG3 MRVE3 MULT3
NATU3 NEOE3 ODPV3 ONCO3 PCAR3 PETR3 PETR4 PETZ3 POMO4 PRIO3 PSSA3 QUAL3 RADL3 RAIL3 RAIZ4 RANI3 RAPT4
RDOR3 RECV3 RENT3 SANB11 SAPR11 SBSP3 SIMH3 SLCE3 SMFT3 SMTO3 STBP3 SUZB3 TAEE11 TEND3 TIMS3 TOTS3 TRPL4
UGPA3 USIM5 VALE3 VAMO3 VBBR3 VIVA3 VIVT3 VULC3 WEGE3 YDUQ3""".split()
TIMEOUT_POR_ATIVO = 20.0


def baixar():
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f"MT5: {mt5.last_error()}")
    partes = []
    for s in ACOES:
        if not mt5.symbol_select(s, True):
            print(f"  {s}: indisponivel", flush=True)
            continue
        t0 = time.time()
        r = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_D1, 1, 1400)
        dt = time.time() - t0
        n = 0 if r is None else len(r)
        print(f"  {s}: {n} barras em {dt:.1f}s", flush=True)
        if n:
            d = pd.DataFrame(r)
            d["symbol"] = s
            partes.append(d)
        if dt > TIMEOUT_POR_ATIVO:  # terminal lento como com os ticks de acoes: para por aqui
            print("  pedido lento demais; interrompendo o download", flush=True)
            break
    mt5.shutdown()
    df = pd.concat(partes)
    df["data"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"real_volume": "volume"})[["data", "symbol", "open", "high", "low", "close", "volume"]]
    ARQ.parent.mkdir(exist_ok=True)
    df.to_csv(ARQ, index=False)
    print(f"salvo {ARQ}: {df.symbol.nunique()} acoes, {len(df)} barras", flush=True)


def painel(campo, df):
    return df.pivot(index="data", columns="symbol", values=campo).sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baixar", action="store_true")
    ap.add_argument("--custo", type=float, default=0.0010, help="custo por lado (corretagem+emolumentos+slippage)")
    ap.add_argument("--liq-min", type=float, default=30e6, help="volume financeiro medio 20d minimo (R$)")
    ap.add_argument("--arquivo", default=str(ARQ), help="barras D1 (csv); padrao dados/b3_d1.csv")
    args = ap.parse_args()
    if args.baixar or not Path(args.arquivo).exists():
        baixar()

    df = pd.read_csv(args.arquivo, parse_dates=["data"])
    O, H, C, V = (painel(c, df) for c in ("open", "high", "close", "volume"))
    ret1 = np.log(C).diff()
    salto = ret1.abs() > np.log(1.35)  # provavel desdobramento/grupamento (barras nao ajustadas)
    # --- fator (janelas por acao) e universo
    corr = pd.DataFrame({s: H[s].rolling(5).corr(V[s]) for s in C.columns})
    std20 = C.rolling(20).std()
    liq = (C * V).rolling(20).mean()
    universo = (liq >= args.liq_min) & C.notna() & C.shift(60).notna()
    sujo = salto.rolling(25, min_periods=1).max().astype(bool)  # janela do fator contaminada por salto
    ok = universo & ~sujo
    rank_vol = std20.where(ok).rank(axis=1, pct=True)
    F = (-(corr - corr.shift(5)) * rank_vol).where(ok)
    rev = (-np.log(C).diff(5)).where(ok)
    # --- retornos futuros: entra na abertura de t+1, sai no fechamento de t+h
    fut = {}
    for h in (1, 5, 10):
        # algum salto entre t+1 e t+h? (janela para frente: rolling no indice invertido)
        salto_fut = salto.iloc[::-1].rolling(h, min_periods=1).max().iloc[::-1].shift(-1).fillna(1).astype(bool)
        fut[h] = (np.log(C.shift(-h)) - np.log(O.shift(-1))).where(ok & ~salto_fut)

    n_dia = ok.sum(axis=1)
    print(f"\nB3: {C.shape[1]} acoes baixadas, {C.index.min():%d/%m/%Y} a {C.index.max():%d/%m/%Y}; "
          f"universo liquido medio {n_dia[n_dia > 0].mean():.0f} acoes/dia (liq >= R$ {args.liq_min / 1e6:.0f} mi)")

    def ic_diario(x, y):
        m = x.notna() & y.notna()
        rx, ry = x.where(m).rank(axis=1), y.where(m).rank(axis=1)
        c = rx.sub(rx.mean(axis=1), axis=0).mul(ry.sub(ry.mean(axis=1), axis=0)).sum(axis=1)
        d = np.sqrt((rx.sub(rx.mean(axis=1), axis=0) ** 2).sum(axis=1) * (ry.sub(ry.mean(axis=1), axis=0) ** 2).sum(axis=1))
        ic = c / d
        return ic[m.sum(axis=1) >= 20]

    # fator sem reversao e sem vol: residuo da regressao por dia (ranks)
    def residuo(f, *xs):
        out = pd.DataFrame(index=f.index, columns=f.columns, dtype=float)
        for dt in f.index:
            y = f.loc[dt]
            X = pd.concat([x.loc[dt] for x in xs], axis=1)
            m = y.notna() & X.notna().all(axis=1)
            if m.sum() < 20:
                continue
            A = np.column_stack([np.ones(m.sum()), X[m].rank().to_numpy()])
            b = np.linalg.lstsq(A, y[m].rank().to_numpy(), rcond=None)[0]
            out.loc[dt, m[m].index] = y[m].rank().to_numpy() - A @ b
        return out

    F_res = residuo(F, rev, rank_vol)
    print(f"\n{'':22}{'IC medio':>9}{'ICIR':>7}{'t (s/ sobrep.)':>15}{'% dias > 0':>11}   IC por ano")
    for h in (1, 5, 10):
        for nome, x in (("fator", F), ("reversao 5d", rev), ("fator s/ rev e vol", F_res)):
            ic = ic_diario(x, fut[h])
            nao_sobrep = ic.iloc[::h]
            t = nao_sobrep.mean() / nao_sobrep.std() * np.sqrt(len(nao_sobrep))
            anos = " ".join(f"{a}:{v:+.3f}" for a, v in ic.groupby(ic.index.year).mean().items())
            print(f"h={h:<2} {nome:18}{ic.mean():>+9.4f}{ic.mean() / ic.std():>+7.2f}{t:>+15.2f}"
                  f"{(ic > 0).mean():>11.0%}   {anos}")
        print()

    # --- quintis, rebalanceamento a cada 5 dias
    datas = F.index[::5]
    linhas, pesos_ant = [], {}
    for dt in datas:
        f, r = F.loc[dt], fut[5].loc[dt]
        m = f.notna() & r.notna()
        if m.sum() < 25:
            continue
        q = pd.qcut(f[m].rank(method="first"), 5, labels=False)
        rets = r[m].groupby(q).apply(lambda x: np.expm1(x).mean())
        # giro das pontas (fracao da carteira trocada)
        topo, fundo = set(q[q == 4].index), set(q[q == 0].index)
        giro_t = 1 - len(topo & pesos_ant.get("t", set())) / max(len(topo), 1)
        giro_b = 1 - len(fundo & pesos_ant.get("b", set())) / max(len(fundo), 1)
        pesos_ant = {"t": topo, "b": fundo}
        linhas.append(dict(data=dt, **{f"Q{i + 1}": rets.get(i, np.nan) for i in range(5)},
                           univ=np.expm1(r[m]).mean(), giro_t=giro_t, giro_b=giro_b))
    q = pd.DataFrame(linhas).set_index("data")
    per_ano = 252 / 5
    q["LS"] = q["Q5"] - q["Q1"]
    q["LS_liq"] = q["LS"] - 2 * args.custo * (q["giro_t"] + q["giro_b"])  # compra+venda em cada ponta
    q["Q5_exc"] = q["Q5"] - q["univ"]
    q["Q5_exc_liq"] = q["Q5_exc"] - 2 * args.custo * q["giro_t"]
    print(f"Quintis (rebalanceio a cada 5 dias, {len(q)} periodos; retorno medio por periodo e anualizado)")
    for c in ("Q1", "Q2", "Q3", "Q4", "Q5", "univ"):
        print(f"  {c:5} {q[c].mean():+.3%} por periodo  ~{(1 + q[c].mean()) ** per_ano - 1:+.1%} a.a.")
    for c, nome in (("LS", "Q5-Q1 bruto"), ("LS_liq", f"Q5-Q1 c/ custo {args.custo:.2%}/lado"),
                    ("Q5_exc", "Q5 - universo bruto"), ("Q5_exc_liq", "Q5 - universo c/ custo")):
        s = q[c]
        print(f"  {nome:28} {s.mean():+.3%}/periodo  ~{s.mean() * per_ano:+.1%} a.a.  Sharpe "
              f"{s.mean() / s.std() * np.sqrt(per_ano):+.2f}  periodos positivos {(s > 0).mean():.0%}")
    print(f"  giro medio por rebalanceio: topo {q.giro_t.mean():.0%}, fundo {q.giro_b.mean():.0%}")
    print("  Q5-Q1 por ano (bruto): " + " ".join(f"{a}:{v:+.1%}" for a, v in
                                                q["LS"].groupby(q.index.year).sum().items()))
    q.to_csv(Path(args.arquivo).parent / "fator_b3_quintis.csv")


if __name__ == "__main__":
    main()
