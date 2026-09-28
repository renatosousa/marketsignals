"""Macro como CONTEXTO de regime do WIN: o que se sabe antes da abertura (EOD anterior + noite) e
o que aconteceu na ultima hora ajudam a prever o regime do dia / da proxima hora?

Regime = volatilidade realizada (rv), eficiencia/tendencia (er) e retorno (ver regimes.py).
Criterio: o macro so conta se MELHORAR, fora da amostra, a previsao feita so com o proprio WIN
(volatilidade e muito persistente; o WIN sozinho ja preve boa parte dela).

  EOD     : 1 linha por pregao. Alvo = regime de 09:05-18:15. Base = regime do WIN na vespera.
            Validacao walk-forward (treina em todos os dias anteriores, preve o seguinte).
  intraday: a cada 15 min de 10:05 a 17:15. Alvo = proximos 60 min. Base = horario do dia +
            regime do WIN nos 60 min anteriores. Validacao: 2/3 iniciais dos dias treinam,
            1/3 final testa (dias inteiros, sem vazamento entre janelas sobrepostas).

Uso: python estudo_regime.py [--parte eod|intraday|ambos] [--db dados/book.db] [--csv-dir dados]
"""
import argparse
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import regimes as rg

warnings.filterwarnings("ignore", category=RuntimeWarning)

MACRO = {"ES": "IB:ES", "NQ": "IB:NQ", "VIX": "IB:VIX", "DX": "IB:DX", "ZN": "IB:ZN", "CL": "IB:CL",
         "GC": "IB:GC", "EWZ": "IB:EWZ", "WDO": "WDO$", "DI27": "DI1F27", "DI29": "DI1F29", "DI33": "DI1F33"}
DI = ("DI27", "DI29", "DI33")  # close e taxa: variacao em pontos-base, nao retorno


# ------------------------------------------------------------------ utilitarios estatisticos
def spearman(x, y):
    m = x.notna() & y.notna()
    return (x[m].rank().corr(y[m].rank()), int(m.sum())) if m.sum() >= 20 else (np.nan, int(m.sum()))


def ols_prev(Xtr, ytr, Xte):
    A = np.column_stack([np.ones(len(Xtr)), Xtr])
    b = np.linalg.lstsq(A, ytr, rcond=None)[0]
    return np.column_stack([np.ones(len(Xte)), Xte]) @ b


def r2_oos(y, prev, media_treino):
    return 1 - np.sum((y - prev) ** 2) / np.sum((y - media_treino) ** 2)


def walk_forward(df, alvo, base, extra, minimo=60):
    """R2 fora da amostra (walk-forward) da base e da base+extra, nas mesmas linhas."""
    cols = base + extra
    d = df[[alvo] + cols].dropna()
    if len(d) < minimo + 20:
        return np.nan, np.nan, len(d)
    y = d[alvo].to_numpy()
    pb, pe, med = [], [], []
    for i in range(minimo, len(d)):
        tr = slice(0, i)
        pb.append(ols_prev(d[base].to_numpy()[tr], y[tr], d[base].to_numpy()[i:i + 1])[0] if base
                  else y[tr].mean())
        pe.append(ols_prev(d[cols].to_numpy()[tr], y[tr], d[cols].to_numpy()[i:i + 1])[0])
        med.append(y[tr].mean())
    yt, med = y[minimo:], np.array(med)
    sst = np.sum((yt - med) ** 2)
    return 1 - np.sum((yt - pb) ** 2) / sst, 1 - np.sum((yt - pe) ** 2) / sst, len(yt)


def split_dias(df, alvo, base, extra, frac=2 / 3):
    cols = base + extra
    d = df[["dia", alvo] + cols].dropna()
    dias = np.sort(d["dia"].unique())
    corte = dias[int(len(dias) * frac)]
    tr, te = d[d["dia"] < corte], d[d["dia"] >= corte]
    if len(tr) < 100 or len(te) < 50:
        return np.nan, np.nan, len(te)
    y, med = te[alvo].to_numpy(), tr[alvo].mean()
    pb = ols_prev(tr[base].to_numpy(), tr[alvo].to_numpy(), te[base].to_numpy())
    pe = ols_prev(tr[cols].to_numpy(), tr[alvo].to_numpy(), te[cols].to_numpy())
    return r2_oos(y, pb, med), r2_oos(y, pe, med), len(te)


def tabela_incremento(nome, resultados):
    print(f"\n  {nome}")
    print(f"    {'variavel macro':24}{'R2 base':>9}{'R2 +macro':>11}{'ganho':>9}   n")
    for var, (rb, re, n) in sorted(resultados.items(), key=lambda kv: -(kv[1][1] - kv[1][0])
                                   if not np.isnan(kv[1][1]) else 0):
        marca = "  <==" if re - rb > 0.01 else ""
        print(f"    {var:24}{rb:>9.3f}{re:>11.3f}{re - rb:>+9.3f}  {n}{marca}")


# ------------------------------------------------------------------ montagem dos dados
def dados_base(con):
    win = rg.carregar(con, "WIN$")
    series = {k: rg.carregar(con, v) for k, v in MACRO.items()}
    dias = sorted(set(win.index.normalize()))
    return win, series, dias


def variacao(k, a, b):
    """Retorno em % (precos) ou variacao em pontos-base (DI, que e taxa)."""
    if k in DI:
        return (b - a) * 100 if pd.notna(a) and pd.notna(b) else np.nan
    return rg.lret(a, b)


def montar_eod(win, series, dias):
    linhas = []
    for i in range(1, len(dias)):
        d, dp = dias[i], dias[i - 1]
        pw = rg.na_grade(win, rg.grade(d, rg.INICIO, rg.FIM))
        pwp = rg.na_grade(win, rg.grade(dp, rg.INICIO, rg.FIM))
        if pw.notna().sum() < 300 or pwp.notna().sum() < 300:
            continue
        fech_ant, abre = dp + pd.Timedelta(rg.FECHAMENTO + ":00"), d + pd.Timedelta(rg.ABERTURA + ":00")
        l = dict(dia=d, dow=d.dayofweek,
                 rv_dia=rg.rv(pw), er_dia=rg.er(pw), ret_dia=rg.ret(pw),
                 win_rv_ant=rg.rv(pwp), win_er_ant=rg.er(pwp), win_ret_ant=rg.ret(pwp),
                 gap=rg.lret(rg.preco_em(win, fech_ant, "2h"), rg.preco_em(win, d + pd.Timedelta("09:04:00"), "10min")))
        for k, s in series.items():
            if k in ("WDO",) + DI:  # B3: variacao no pregao anterior (nao negocia a noite)
                p = rg.na_grade(s, rg.grade(dp, rg.INICIO, rg.FIM)).dropna()
                l[f"{k}_ant"] = variacao(k, p.iloc[0], p.iloc[-1]) if len(p) > 30 else np.nan
                if k == "WDO":
                    l["WDO_rv_ant"] = rg.rv(rg.na_grade(s, rg.grade(dp, rg.INICIO, rg.FIM)))
            else:  # exterior: o que andou com a B3 fechada (fechamento anterior -> 09:00)
                a, b = rg.preco_em(s, fech_ant, "3h"), rg.preco_em(s, abre, "3h")
                l[f"{k}_noite"] = variacao(k, a, b)
                if k in ("ES", "VIX"):
                    noite = s[(s.index > fech_ant) & (s.index <= abre)]
                    l[f"{k}_rv_noite"] = rg.rv(noite) if len(noite) > 30 else np.nan
                if k == "VIX":
                    l["VIX_nivel"] = b
        di27 = rg.preco_em(series["DI27"], dp + pd.Timedelta("18:00:00"), "3h")
        di33 = rg.preco_em(series["DI33"], dp + pd.Timedelta("18:00:00"), "3h")
        l["DI_incl"] = (di33 - di27) * 100 if pd.notna(di27) and pd.notna(di33) else np.nan
        linhas.append(l)
    df = pd.DataFrame(linhas)
    # transformacoes para prever intensidade (vol/amplitude): tamanho do movimento, log da vol
    df["log_rv_dia"], df["log_win_rv_ant"] = np.log(df["rv_dia"]), np.log(df["win_rv_ant"])
    df["abs_ret_dia"] = df["ret_dia"].abs()
    return df


def montar_intraday(win, series, dias):
    linhas = []
    slots = pd.timedelta_range("10:05:00", "17:15:00", freq="15min")
    dias_ok = set(dias)
    for d in dias:
        g = rg.grade(d, rg.INICIO, rg.FIM)
        pw = rg.na_grade(win, g)
        if pw.notna().sum() < 300:
            continue
        px = {k: rg.na_grade(s, g) for k, s in series.items()}
        for sl in slots:
            t = d + sl
            pas, fut, ant = (pw[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")],
                             pw[t:t + pd.Timedelta("59min")], pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")])
            l = dict(dia=d, slot=str(sl)[-8:-3],
                     win_rv_fut=rg.rv(fut), win_er_fut=rg.er(fut), win_ret_fut=rg.ret(fut),
                     win_rv_pas=rg.rv(pas), win_er_pas=rg.er(pas), win_ret_pas=rg.ret(pas),
                     win_rv_ant=rg.rv(ant) if ant.notna().sum() >= 30 else np.nan)
            for k, p in px.items():
                ps = p[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")].dropna()
                l[f"{k}_var"] = variacao(k, ps.iloc[0], ps.iloc[-1]) if len(ps) > 20 else np.nan
                if k in ("ES", "WDO", "VIX"):
                    l[f"{k}_rv"] = rg.rv(p[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")])
                    pa = p[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
                    l[f"{k}_rv_ant"] = rg.rv(pa) if pa.notna().sum() >= 30 else np.nan
            l["VIX_nivel"] = px["VIX"][:t].dropna().iloc[-1] if px["VIX"][:t].notna().any() else np.nan
            l["corr_WIN_ES"] = rg.corr_ret(pas, px["ES"][t - pd.Timedelta("60min"):t - pd.Timedelta("1min")])
            l["corr_WIN_WDO"] = rg.corr_ret(pas, px["WDO"][t - pd.Timedelta("60min"):t - pd.Timedelta("1min")])
            linhas.append(l)
    df = pd.DataFrame(linhas)
    for c in ("win_rv_fut", "win_rv_pas", "win_rv_ant", "ES_rv", "ES_rv_ant", "WDO_rv", "WDO_rv_ant",
              "VIX_rv", "VIX_rv_ant", "VIX_nivel"):
        df[f"log_{c}"] = np.log(df[c])
    df["mud_rv_win"] = df["log_win_rv_fut"] - df["log_win_rv_pas"]        # mudanca de regime de vol
    df["acel_ES"] = df["log_ES_rv"] - df["log_ES_rv_ant"]                  # macro acelerando/acalmando
    df["acel_WDO"] = df["log_WDO_rv"] - df["log_WDO_rv_ant"]
    df["acel_WIN"] = df["log_win_rv_pas"] - df["log_win_rv_ant"]
    for k in MACRO:
        df[f"abs_{k}_var"] = df[f"{k}_var"].abs()
    return df


def historico_vol(eod):
    """Vol diaria do WIN em dias ANTERIORES (vespera, media de 5 e 20 dias), em log. Sem ela, qualquer
    medida lenta de risco (ex.: nivel do VIX) parece informacao nova so por resumir semanas de vol."""
    d = eod[["dia", "rv_dia"]].sort_values("dia").set_index("dia")["rv_dia"].shift(1)
    h = pd.DataFrame({"win_rv_d1": d, "win_rv_d5": d.rolling(5, min_periods=3).mean(),
                      "win_rv_d20": d.rolling(20, min_periods=10).mean()})
    return np.log(h).add_prefix("log_").reset_index()


BASE_LONGA = ["log_win_rv_d1", "log_win_rv_d5", "log_win_rv_d20"]


# ------------------------------------------------------------------ relatorios
def relatorio_eod(df):
    n = len(df)
    print(f"\n#################### EOD ANTERIOR + NOITE -> REGIME DO DIA ({n} pregoes, "
          f"{df.dia.min():%d/%m} a {df.dia.max():%d/%m}) ####################")
    print(f"limiar de significancia ~ |rho| > {2 / np.sqrt(n):.2f}")
    feats = ["win_rv_ant", "win_er_ant", "win_ret_ant", "gap", "ES_noite", "NQ_noite", "VIX_noite", "VIX_nivel",
             "ES_rv_noite", "DX_noite", "ZN_noite", "CL_noite", "GC_noite", "EWZ_noite", "WDO_ant", "WDO_rv_ant",
             "DI27_ant", "DI29_ant", "DI33_ant", "DI_incl"]
    print(f"\n  correlacao de Spearman com o regime do dia\n    {'variavel':16}{'vol dia':>9}{'|ret| dia':>11}"
          f"{'efic. dia':>11}{'ret dia':>9}")
    for f in feats:
        if f not in df:
            continue
        vals = [spearman(df[f].abs() if f.endswith(("noite", "_ant", "gap")) and f not in
                         ("win_rv_ant", "win_er_ant", "WDO_rv_ant", "ES_rv_noite") else df[f], df[a])[0]
                for a in ("rv_dia", "abs_ret_dia", "er_dia")]
        vals.append(spearman(df[f], df["ret_dia"])[0])
        print(f"    {f:16}" + "".join(f"{v:>+10.2f}" for v in vals))
    print("    (vol/|ret|/eficiencia usam o TAMANHO do movimento macro; 'ret dia' usa o sinal)")

    # incremento fora da amostra sobre a persistencia do proprio WIN
    for k in [c for c in df.columns if c.endswith(("noite", "_ant")) and c != "win_rv_ant"] + ["gap"]:
        df[f"abs_{k}"] = df[k].abs()
    df["log_VIX_nivel"], df["log_ES_rv_noite"] = np.log(df["VIX_nivel"]), np.log(df["ES_rv_noite"])
    cands = ["log_VIX_nivel", "log_ES_rv_noite", "abs_ES_noite", "abs_VIX_noite", "abs_DX_noite", "abs_ZN_noite",
             "abs_CL_noite", "abs_GC_noite", "abs_gap", "abs_WDO_ant", "abs_DI29_ant", "DI_incl"]
    df = df.merge(historico_vol(df), on="dia", how="left")
    res = {c: walk_forward(df, "log_rv_dia", BASE_LONGA, [c]) for c in cands}
    res["TODOS (VIX, ES noite, gap, DI)"] = walk_forward(
        df, "log_rv_dia", BASE_LONGA, ["log_VIX_nivel", "log_ES_rv_noite", "abs_gap", "abs_DI29_ant"])
    tabela_incremento("VOL DO DIA (log) - base: vol do WIN na vespera e medias de 5 e 20 dias", res)
    cands_er = ["log_VIX_nivel", "abs_ES_noite", "abs_gap", "abs_DX_noite", "abs_DI29_ant", "DI_incl", "win_ret_ant"]
    res = {c: walk_forward(df, "er_dia", ["win_er_ant"], [c]) for c in cands_er}
    tabela_incremento("EFICIENCIA/TENDENCIA DO DIA - base: eficiencia do WIN na vespera", res)
    cands_ret = ["ES_noite", "NQ_noite", "VIX_noite", "DX_noite", "ZN_noite", "CL_noite", "EWZ_noite", "gap",
                 "win_ret_ant", "WDO_ant", "DI29_ant"]
    res = {c: walk_forward(df, "ret_dia", [], [c]) for c in cands_ret}
    tabela_incremento("DIRECAO DO DIA (ret %) - base: media historica", res)


def relatorio_intraday(df, hist):
    df = df.merge(hist, on="dia", how="left")
    dias = df.dia.nunique()
    print(f"\n#################### ULTIMA HORA -> REGIME DA PROXIMA HORA ({len(df)} janelas, {dias} pregoes) "
          f"####################")
    slot = pd.get_dummies(df["slot"], prefix="h", drop_first=True, dtype=float)
    df = pd.concat([df, slot], axis=1)
    hs = list(slot.columns)
    base_vol = hs + ["log_win_rv_pas", "log_win_rv_ant"] + BASE_LONGA
    cands = ["log_ES_rv", "acel_ES", "log_VIX_nivel", "abs_VIX_var", "log_WDO_rv", "acel_WDO", "abs_ES_var",
             "abs_DX_var", "abs_ZN_var", "abs_CL_var", "abs_DI29_var", "corr_WIN_ES", "corr_WIN_WDO"]
    res = {c: split_dias(df, "log_win_rv_fut", base_vol, [c]) for c in cands}
    res["TODOS"] = split_dias(df, "log_win_rv_fut", base_vol, cands)
    tabela_incremento("VOL DA PROXIMA HORA (log) - base: horario + vol do WIN nas 2 h anteriores e em 1/5/20 dias",
                      res)

    base_mud = hs + ["acel_WIN", "log_win_rv_pas"] + BASE_LONGA
    res = {c: split_dias(df, "mud_rv_win", base_mud, [c]) for c in ["acel_ES", "acel_WDO", "abs_VIX_var",
                                                                   "log_VIX_nivel", "corr_WIN_ES"]}
    tabela_incremento("MUDANCA DE REGIME DE VOL (prox. hora vs. ultima) - base: horario + aceleracao e vol do WIN",
                      res)

    base_er = hs + ["win_er_pas"]
    cands_er = ["corr_WIN_ES", "corr_WIN_WDO", "abs_ES_var", "abs_DX_var", "abs_DI29_var", "log_VIX_nivel", "acel_ES"]
    res = {c: split_dias(df, "win_er_fut", base_er, [c]) for c in cands_er}
    res["TODOS"] = split_dias(df, "win_er_fut", base_er, cands_er)
    tabela_incremento("EFICIENCIA/TENDENCIA DA PROXIMA HORA - base: horario + eficiencia da ultima hora", res)

    cands_ret = ["ES_var", "NQ_var", "VIX_var", "DX_var", "ZN_var", "CL_var", "GC_var", "WDO_var", "DI29_var",
                 "win_ret_pas"]
    res = {c: split_dias(df, "win_ret_fut", [], [c]) for c in cands_ret}
    tabela_incremento("DIRECAO DA PROXIMA HORA (ret %) - base: media", res)
    print("\n  Spearman (proxima hora):")
    for c in cands_ret:
        print(f"    {c:14} ret {spearman(df[c], df['win_ret_fut'])[0]:+.3f}   "
              f"|ret| x vol {spearman(df[c].abs(), df['win_rv_fut'])[0]:+.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parte", choices=["eod", "intraday", "ambos"], default="ambos")
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--csv-dir", default="dados")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    win, series, dias = dados_base(con)
    eod = montar_eod(win, series, dias)  # a parte intraday tambem usa a vol diaria (historico_vol)
    eod.to_csv(Path(args.csv_dir) / "regime_eod.csv", index=False)
    if args.parte in ("eod", "ambos"):
        relatorio_eod(eod)
    if args.parte in ("intraday", "ambos"):
        df = montar_intraday(win, series, dias)
        df.to_csv(Path(args.csv_dir) / "regime_intraday.csv", index=False)
        relatorio_intraday(df, historico_vol(eod))
    print("\nLeitura: 'ganho' = aumento do R2 fora da amostra ao juntar a variavel macro a base so-WIN.\n"
          "Acima de ~0,01 (marcado <==) e informacao nova util; perto de zero ou negativo = o macro\n"
          "nao acrescenta nada ao que o WIN ja mostra.")


if __name__ == "__main__":
    main()
