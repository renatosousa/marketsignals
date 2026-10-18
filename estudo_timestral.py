"""Timestral (Mistral, series temporais zero-shot) como previsor da vol da proxima hora do WIN. Estudo OFFLINE,
somente leitura: nao altera o banco nem os coletores.

Mesma regua do projeto (modelo_regime.py / estudo_regime.py): a cada 15 min de 10:05 a 17:15, alvo
log(vol realizada dos proximos ~60 min), base = HAR do modelo de producao (horario + vol da ultima hora e da
anterior + vol diaria de vespera/5d/20d). O Timestral entra como variavel extra do OLS (stacking):
  serie    : log da vol realizada por bloco de 5 min (09:05-18:15 = 110 blocos/dia), dias concatenados
  contexto : ultimos --ctx blocos ANTES do instante t (so passado)
  horizonte: 12 blocos (60 min); covariavel futura conhecida = indice do bloco no pregao
  previsao : vol da hora = sqrt(soma(exp(2*q)))  (q50, e q10/q90 para a banda)
Validacao: walk-forward por dia inteiro (treina em todos os dias anteriores, preve o dia seguinte), com as
mesmas linhas para base e base+Timestral. O VIX fica de fora da base (o ganho dele e pequeno e o dado recente vem atrasado).

Saidas: R2 fora da amostra e ganho (base+TS - base) por grupo de horario e por mes; teste de Diebold-Mariano
agrupado por dia; calibracao da faixa de +-1/2 desvios (retorno de 60 min / vol prevista).

Uso: python estudo_timestral.py [--db dados/book.db] [--modelos 5M,28M] [--ctx 2048] [--lote 16]
                                 [--threads 6] [--refazer] [--sem-modelo] [--csv-dir dados]
  --sem-modelo : so as bases (nao precisa do pacote timestral)
"""
import argparse
import sqlite3
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import modelo_regime as mr
import regimes as rg
from estudo_regime import ols_prev

warnings.filterwarnings("ignore", category=RuntimeWarning)

BLOCOS = 110          # blocos de 5 min entre 09:05 e 18:15
H = 12                # horizonte: 12 blocos = 60 min
MIN_TREINO = 60       # dias de treino antes do 1o dia de teste
MIN_CTX_DIAS = 20     # dias de historico para o 1o contexto
NIVEIS = (0.1, 0.5, 0.9)
GRUPOS = {            # horarios com e sem a vol da hora anterior (como no modelo de producao)
    "10:35-17:15 (modelo_hora)": [s for s in mr.SLOTS if s >= "10:35"],
    "10:05-10:20 (modelo_hora_curto)": [s for s in mr.SLOTS if s < "10:35"],
}


# ------------------------------------------------------------------ dados
def montar(con):
    """Linhas (dia, slot) com alvo, variaveis do modelo atual, retorno de 60 min e os blocos de 5 min por dia."""
    win = rg.carregar(con, "WIN$")
    vix = rg.carregar(con, "IB:VIX")
    dias = sorted(set(win.index.normalize()))
    blocos, rv_dia, linhas = {}, {}, []
    for d in dias:
        g = rg.grade(d, rg.INICIO, rg.FIM)
        pw = rg.na_grade(win, g)
        if pw.notna().sum() < 300:
            continue
        rv_dia[d] = rg.rv(pw)
        r2 = (np.log(pw).diff().iloc[1:] ** 2).to_numpy().reshape(BLOCOS, 5)
        b = np.sqrt(np.nansum(r2, axis=1)) * 100
        b[(~np.isnan(r2)).sum(axis=1) < 3] = np.nan
        blocos[d] = b
        for sl in mr.SLOTS:
            t = d + pd.Timedelta(sl + ":00")
            ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
            linhas.append(dict(
                dia=d, slot=sl, j0=int((t - (d + pd.Timedelta("09:05:00"))) / pd.Timedelta("5min")),
                rv_fut=rg.rv(pw[t:t + pd.Timedelta("59min")]),
                rv_pas=rg.rv(pw[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")]),
                rv_ant=rg.rv(ant) if ant.notna().sum() >= 30 else np.nan,
                ret60=rg.lret(pw.get(t), pw.get(t + pd.Timedelta("60min"))),
                vix=rg.preco_em(vix, t, "3D")))
    df = pd.DataFrame(linhas)
    diaria = pd.Series(rv_dia).sort_index()
    ant = diaria.shift(1)
    hist = pd.DataFrame({"log_d1": np.log(ant), "log_d5": np.log(ant.rolling(5, min_periods=3).mean()),
                         "log_d20": np.log(ant.rolling(20, min_periods=10).mean())})
    df = df.merge(hist.rename_axis("dia").reset_index(), on="dia", how="left")
    for c in ("rv_fut", "rv_pas", "rv_ant"):
        df[f"log_{c}"] = np.log(df[c].where(df[c] > 0))
    df["mes"] = df.dia.dt.strftime("%Y-%m")
    return df, blocos


def enriquecer(df, blocos):
    """Base classica mais forte (as mesmas variaveis do modelo de producao, modelo_regime.EXTRAS), para nao
    atribuir ao Timestral o que medias moveis simples tambem captam."""
    return mr.extras_treino(df, blocos)[0]


EXTRAS = mr.EXTRAS


# ------------------------------------------------------------------ Timestral
def _np(x):
    return x.detach().cpu().numpy() if hasattr(x, "detach") else np.asarray(x)


def prever_timestral(df, blocos, nome, ctx, lote, threads, cache, refazer):
    """-> DataFrame (dia, slot, ts_q10, ts_q50, ts_q90): vol prevista (%) para a proxima hora."""
    import torch
    from timestral import TimestralForecaster
    torch.set_num_threads(threads)
    dias = sorted(blocos)
    pos = {d: i for i, d in enumerate(dias)}
    serie = np.log(np.clip(np.concatenate([blocos[d] for d in dias]), 1e-3, None))
    serie[np.isnan(np.concatenate([blocos[d] for d in dias]))] = np.nan
    idx_bloco = np.tile(np.arange(BLOCOS), len(dias)).astype(np.float32)

    prontos = pd.read_csv(cache, parse_dates=["dia"]) if cache.exists() and not refazer else pd.DataFrame(
        columns=["dia", "slot", "ts_q10", "ts_q50", "ts_q90"])
    feitos = set(zip(prontos.dia, prontos.slot))
    alvo = [(r.dia, r.slot, pos[r.dia] * BLOCOS + r.j0) for r in df.itertuples()
            if (r.dia, r.slot) not in feitos and pos[r.dia] * BLOCOS + r.j0 >= max(ctx, MIN_CTX_DIAS * BLOCOS)]
    if not alvo:
        return prontos
    print(f"  carregando Timestral-{nome} ...", flush=True)
    t0 = time.time()
    fc = TimestralForecaster.from_pretrained(f"mistralai/Timestral-{nome}")
    print(f"  carregado em {time.time() - t0:.0f}s; {len(alvo)} previsoes, contexto {ctx}, lote {lote}", flush=True)
    novos, t0 = [], time.time()
    for i in range(0, len(alvo), lote):
        lt = alvo[i:i + lote]
        X = np.stack([serie[p - ctx:p] for _, _, p in lt])[:, None, :].astype(np.float32)
        K = np.stack([idx_bloco[p - ctx:p + H] for _, _, p in lt])[:, None, :]
        f = fc.forecast(X, prediction_length=H, known_covariates=K)
        q = _np(f.quantiles)                                   # (B, 1, H, 21)
        niv = [float(v) for v in f.quantile_levels]
        cols = [int(np.argmin([abs(v - n) for v in niv])) for n in NIVEIS]
        vol = {n: np.sqrt(np.exp(2 * q[:, 0, :, c]).sum(axis=1)) for n, c in zip(NIVEIS, cols)}
        for k, (d, sl, _) in enumerate(lt):
            novos.append(dict(dia=d, slot=sl, ts_q10=vol[0.1][k], ts_q50=vol[0.5][k], ts_q90=vol[0.9][k]))
        if (i // lote) % 20 == 0:
            print(f"    {i + len(lt)}/{len(alvo)}  ({time.time() - t0:.0f}s)", flush=True)
    out = pd.concat([prontos, pd.DataFrame(novos)], ignore_index=True)
    out.to_csv(cache, index=False)
    print(f"  {len(novos)} previsoes em {time.time() - t0:.0f}s ({(time.time() - t0) / len(novos) * 1000:.0f} ms cada)",
          flush=True)
    return out


# ------------------------------------------------------------------ validacao
def walk_forward(d, y, cols):
    """Previsao e media de treino de cada linha, treinando em todos os dias anteriores ao dia da linha."""
    dias = np.sort(d.dia.unique())
    X, Y = d[cols].to_numpy(float), d[y].to_numpy(float)
    pred, med = np.full(len(d), np.nan), np.full(len(d), np.nan)
    for k in range(MIN_TREINO, len(dias)):
        tr, te = (d.dia < dias[k]).to_numpy(), (d.dia == dias[k]).to_numpy()
        if tr.sum() < 100:
            continue
        pred[te] = ols_prev(X[tr], Y[tr], X[te])
        med[te] = Y[tr].mean()
    return pred, med


def r2(y, pred, med):
    m = ~np.isnan(pred)
    return 1 - np.sum((y[m] - pred[m]) ** 2) / np.sum((y[m] - med[m]) ** 2), int(m.sum())


def dm_por_dia(dia, perda_a, perda_b):
    """Diebold-Mariano agrupado por dia: media da diferenca de perdas por dia e estatistica t entre dias.
    Positivo = b melhor que a."""
    d = pd.DataFrame({"dia": dia, "x": perda_a - perda_b}).dropna().groupby("dia").x.mean()
    return float(d.mean()), float(d.mean() / (d.std() / np.sqrt(len(d)))) if len(d) > 5 else np.nan, len(d)


def avaliar(df, rotulo):
    print(f"\n=== {rotulo} ===")
    resumo = []
    for gnome, slots in GRUPOS.items():
        g = df[df.slot.isin(slots)].copy()
        hs = [f"h_{s}" for s in slots[1:]]
        for s in slots[1:]:
            g[f"h_{s}"] = (g.slot == s).astype(float)
        har = hs + ["log_rv_pas"] + mr.VAR_LONGAS + (["log_rv_ant"] if "10:35" in slots else [])
        especs = {"so horario": hs, "horario + vol da ultima hora": hs + ["log_rv_pas"], "HAR (base de producao)": har,
                  "HAR enriquecido (medias moveis etc.)": har + EXTRAS}
        if "log_ts" in g:
            especs["horario + Timestral"] = hs + ["log_ts"]
            especs["HAR + Timestral"] = har + ["log_ts"]
            especs["HAR enriquecido + Timestral"] = har + EXTRAS + ["log_ts"]
        cols_todas = sorted(set(sum(especs.values(), [])) | {"log_rv_fut", "ret60"})
        d = g.dropna(subset=cols_todas).sort_values(["dia", "slot"]).reset_index(drop=True)
        if d.dia.nunique() < MIN_TREINO + 10:
            print(f"\n  {gnome}: amostra insuficiente ({d.dia.nunique()} dias)")
            continue
        y = d.log_rv_fut.to_numpy()
        res = {k: walk_forward(d, "log_rv_fut", c) for k, c in especs.items()}
        print(f"\n  {gnome}  |  {d.dia.nunique()} dias, {int((~np.isnan(res['HAR (base de producao)'][0])).sum())} "
              f"previsoes fora da amostra (a partir de {pd.Timestamp(np.sort(d.dia.unique())[MIN_TREINO]).date()})")
        print(f"    {'especificacao':34}{'R2 OOS':>8}")
        for k, (p, m) in res.items():
            print(f"    {k:34}{r2(y, p, m)[0]:>8.3f}")
        if "HAR + Timestral" in res:
            for rot_b, rot_e in (("HAR (base de producao)", "HAR + Timestral"),
                                 ("HAR enriquecido (medias moveis etc.)", "HAR enriquecido + Timestral")):
                pb, mb = res[rot_b]
                pe, _ = res[rot_e]
                ganho = r2(y, pe, mb)[0] - r2(y, pb, mb)[0]
                md, t, nd = dm_por_dia(d.dia, (y - pb) ** 2, (y - pe) ** 2)
                print(f"    ganho do Timestral sobre {rot_b}: {ganho:+.4f} | dif. de EQM {md:+.5f}, t entre dias {t:+.2f} "
                      f"({nd} dias)")
                ok = ~np.isnan(pb)
                por_mes = []
                for mes, gm in d[ok].groupby("mes"):
                    i = gm.index.to_numpy()
                    sst = np.sum((y[i] - mb[i]) ** 2)
                    por_mes.append((mes, len(i), (np.sum((y[i] - pb[i]) ** 2) - np.sum((y[i] - pe[i]) ** 2)) / sst))
                print("      por mes: " + ", ".join(f"{m} {gm:+.3f}" for m, n, gm in por_mes))
                sinais = sum(1 for *_, gm in por_mes if gm > 0)
                resumo.append((f"{gnome} | vs {rot_b[:12]}", ganho, t, sinais, len(por_mes)))
            calibrar(d, ~np.isnan(res["HAR (base de producao)"][0]), res["HAR (base de producao)"][0],
                     res["HAR enriquecido + Timestral"][0])
    return resumo


def calibrar(d, ok, pb, pe):
    """Cobertura da faixa +-k * escala * vol prevista para o retorno de 60 min. A escala e a dispersao de
    z = retorno / vol prevista, estimada so com linhas anteriores (como um ajuste conformal simples)."""
    print("    calibracao da faixa (retorno de 60 min / vol prevista; escala estimada so no passado):")
    for nome, p in (("HAR", pb), ("HAR + Timestral", pe)):
        z = d.ret60.to_numpy() / np.exp(p)
        dia = d.dia.to_numpy()
        m = ok & ~np.isnan(z)
        zs, ds = z[m], dia[m]
        dias_u = np.unique(ds)
        corte = dias_u[len(dias_u) // 3]
        antes, depois = ds < corte, ds >= corte
        if antes.sum() < 100 or depois.sum() < 100:
            continue
        # escala expansiva por dia (so dias anteriores ao da linha)
        esc = np.full(len(zs), np.nan)
        for u in dias_u[dias_u >= corte]:
            esc[ds == u] = np.std(zs[ds < u])
        zz = zs[depois] / esc[depois]
        print(f"      {nome:18} dispersao de z {np.std(zs):.2f} | cobertura de +-1: {np.mean(np.abs(zz) <= 1) * 100:.1f}% "
              f"(normal 68,3) | +-2: {np.mean(np.abs(zz) <= 2) * 100:.1f}% (normal 95,4) | n={len(zz)}")


# ------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--modelos", default="5M,28M")
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--lote", type=int, default=16)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--refazer", action="store_true")
    ap.add_argument("--sem-modelo", action="store_true")
    ap.add_argument("--csv-dir", default="dados")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    t0 = time.time()
    df, blocos = montar(con)
    con.close()
    df = enriquecer(df, blocos)
    print(f"{df.dia.nunique()} pregoes ({df.dia.min().date()} a {df.dia.max().date()}), {len(df)} linhas dia x horario "
          f"({time.time() - t0:.0f}s)")

    resumos = {}
    if args.sem_modelo:
        avaliar(df, "somente bases (sem Timestral)")
    else:
        for nome in args.modelos.split(","):
            cache = Path(args.csv_dir) / f"timestral_{nome}_ctx{args.ctx}.csv"
            ts = prever_timestral(df, blocos, nome, args.ctx, args.lote, args.threads, cache, args.refazer)
            ts["dia"] = pd.to_datetime(ts["dia"]).astype(df.dia.dtype)
            d = df.merge(ts, on=["dia", "slot"], how="inner")
            d["log_ts"] = np.log(d.ts_q50.where(d.ts_q50 > 0))
            print(f"\nTimestral-{nome}: correlacao de log(ts_q50) com log(vol da proxima hora): "
                  f"{d[['log_ts', 'log_rv_fut']].corr().iloc[0, 1]:.3f} | com log(vol da ultima hora): "
                  f"{d[['log_ts', 'log_rv_pas']].corr().iloc[0, 1]:.3f}")
            resumos[nome] = avaliar(d, f"Timestral-{nome}, contexto {args.ctx}")

    print("\n" + "-" * 72)
    print("Criterio do projeto: ganho de R2 >= +0,01 e do mesmo sinal na maioria dos meses.")
    for nome, r in resumos.items():
        for g, ganho, t, pos, n in r:
            print(f"  Timestral-{nome:5} {g:34} ganho {ganho:+.4f}  t={t:+.2f}  meses+ {pos}/{n}"
                  f"{'  <== passa' if ganho >= 0.01 and pos >= 2 / 3 * n else ''}")
    # aviso sobre o modelo de producao: sem VIX a linha e descartada
    vix_ok = df.assign(tem_vix=df.vix.notna()).groupby("mes").tem_vix.mean()
    print("\nAviso (modelo de producao): fracao das linhas com VIX por mes (sem VIX, ajustar() descarta a linha):")
    print("  " + ", ".join(f"{m} {v * 100:.0f}%" for m, v in vix_ok.items()))


if __name__ == "__main__":
    main()
