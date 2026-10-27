"""Modelo de regime do WIN (resultado do estudo_regime.py), treinado com todo o historico.

  hora : log(vol da proxima hora) ~ horario + vol do WIN na ultima hora (e na anterior) +
         vol diaria do WIN (vespera, 5 e 20 dias) + EXTRAS (medias moveis exponenciais da vol em blocos de 5 min,
         vol da ultima meia hora e vol da mesma janela nos dias anteriores). R2 fora da amostra ~0,76; os EXTRAS
         somam ~+0,03 (estudo_timestral.py, 182 dias, positivo em 7 de 7 meses). O VIX saiu do modelo em 19/10/2026:
         com os EXTRAS ele piorava a previsao fora da amostra (R2 0,758 sem VIX, 0,750 com VIX intradiario).
  dia  : log(vol do dia) ~ vol diaria do WIN (vespera, 5 e 20 dias). R2 ~0,47.
  normais: vol tipica de 60 min em cada horario (WIN e WDO), para a vol relativa.
  DI   : distribuicao da variacao diaria do DI1F29, para dizer se o dia de hoje e atipico.

O macro entra so como contexto (alertas e painel); direcao e tendencia nao sao previsiveis (ver estudo) e por
isso nao ha modelo para elas.

Uso: python modelo_regime.py [--db dados/book.db] [--sem-atualizar]   -> grava dados/modelo_regime.json
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import regimes as rg

ARQ = Path(__file__).parent / "dados" / "modelo_regime.json"
SLOTS = [f"{h:02d}:{m:02d}" for h in range(10, 18) for m in (5, 20, 35, 50)
         if "10:05" <= f"{h:02d}:{m:02d}" <= "17:15"]
VAR_LONGAS = ["log_d1", "log_d5", "log_d20"]
BLOCOS = 110  # blocos de 5 min entre 09:05 e 18:15
MEIAS_VIDAS = (("1h", 12), ("meiodia", 55), ("2d", 220), ("5d", 550))  # em blocos de 5 min
EXTRAS = ["log_ew_1h", "log_ew_meiodia", "log_ew_2d", "log_ew_5d", "log_l30", "log_mesma_d1", "log_mesma_m5"]


def blocos_de(pw):
    """Vol realizada (%) por bloco de 5 min na grade 09:05-18:15. pw: precos de 1 min na grade (pode ser parcial:
    o resto vira NaN). Bloco com menos de 3 retornos validos = NaN."""
    r2 = (np.log(pw).diff().iloc[1:] ** 2).to_numpy()
    r2 = np.concatenate([r2, np.full(BLOCOS * 5 - len(r2), np.nan)]).reshape(BLOCOS, 5)
    b = np.sqrt(np.nansum(r2, axis=1)) * 100
    b[(~np.isnan(r2)).sum(axis=1) < 3] = np.nan
    return b


def _var_ajustada(b, forma):
    """Variancia do bloco dividida pela forma tipica do dia (o horario ja nao entra nas medias moveis)."""
    v = np.exp(2 * (np.log(np.clip(b, 1e-3, None)) - forma))
    v[np.isnan(b)] = np.nan
    return v


def _ewma(v0, serie, hl):
    """Media movel exponencial (meia vida hl, em blocos) continuando de v0; NaN nao atualiza. -> trajetoria."""
    a, v, out = 1 - 0.5 ** (1 / hl), v0, np.empty(len(serie))
    for i, x in enumerate(serie):
        if not np.isnan(x):
            v = x if np.isnan(v) else (1 - a) * v + a * x
        out[i] = v
    return out


def extras_treino(df, blocos):
    """Acrescenta EXTRAS a df (linhas dia x horario). blocos: {dia: vol por bloco de 5 min}. So usa blocos
    anteriores ao instante da linha. A forma tipica do dia vem dos 60 primeiros pregoes. -> (df, forma)."""
    dias = sorted(blocos)
    pos = {d: i for i, d in enumerate(dias)}
    b = np.concatenate([blocos[d] for d in dias])
    j = np.tile(np.arange(BLOCOS), len(dias))
    lb = np.log(np.clip(b[:60 * BLOCOS], 1e-3, None))
    forma = np.array([np.nanmedian(lb[j[:60 * BLOCOS] == k]) for k in range(BLOCOS)])
    var_aj = _var_ajustada(b, forma[j])
    p0 = np.array([pos[d] * BLOCOS + int((pd.Timestamp(sl + ":00") - pd.Timestamp("09:05:00")) / pd.Timedelta("5min"))
                   for d, sl in zip(df.dia, df.slot)])
    for nome, hl in MEIAS_VIDAS:
        ew = _ewma(np.nan, var_aj, hl)
        df[f"log_ew_{nome}"] = np.where(p0 > 0, 0.5 * np.log(ew[np.maximum(p0 - 1, 0)]), np.nan)
    cs = np.concatenate([[0.0], np.cumsum(np.where(np.isnan(b), 0.0, b ** 2))])
    df["log_l30"] = 0.5 * np.log(np.maximum(cs[p0] - cs[np.maximum(p0 - 6, 0)], 1e-6))
    piv = df.pivot(index="dia", columns="slot", values="rv_fut").sort_index()
    for nome, m in (("d1", piv.shift(1)), ("m5", piv.shift(1).rolling(5, min_periods=3).mean())):
        longo = np.log(m.where(m > 0)).stack().rename(f"log_mesma_{nome}").reset_index()
        df = df.drop(columns=[f"log_mesma_{nome}"], errors="ignore").merge(longo, on=["dia", "slot"], how="left")
    return df, forma


def historico_extras(con, modelo, hoje=None):
    """Estado dos EXTRAS ao fim do ultimo pregao fechado (dias anteriores a hoje): medias moveis da vol ajustada
    e as vols das janelas de 60 min por horario nos ultimos 5 pregoes. Calculado uma vez na partida do coletor."""
    forma = np.array(modelo["forma"])
    hoje = pd.Timestamp(hoje).normalize() if hoje is not None else pd.Timestamp.now().normalize()
    win = rg.carregar(con, "WIN$")
    ew = {nome: np.nan for nome, _ in MEIAS_VIDAS}
    janelas = {sl: [] for sl in SLOTS}
    for d in (x for x in sorted(set(win.index.normalize())) if x < hoje):
        pw = rg.na_grade(win, rg.grade(d, rg.INICIO, rg.FIM))
        if pw.notna().sum() < 300:
            continue
        var_aj = _var_ajustada(blocos_de(pw), forma)
        for nome, hl in MEIAS_VIDAS:
            ew[nome] = float(_ewma(ew[nome], var_aj, hl)[-1])
        for sl in SLOTS:
            t = d + pd.Timedelta(sl + ":00")
            janelas[sl].append(rg.rv(pw[t:t + pd.Timedelta("59min")]))
    return dict(ew=ew, janelas={sl: v[-5:] for sl, v in janelas.items()})


def extras_ao_vivo(modelo, hist, pw, t):
    """EXTRAS no instante t com os blocos de 5 min de hoje ja completos. pw: precos de hoje na grade ate t (sem t).
    -> dict ou None se faltar dado."""
    j = int(((t - pd.Timedelta("1min")) - (t.normalize() + pd.Timedelta("09:05:00"))) / pd.Timedelta("5min"))
    sl = slot_de(t)
    if hist is None or sl is None or j < 12:
        return None
    forma = np.array(modelo["forma"])
    b = blocos_de(pw)[:j]
    var_aj = _var_ajustada(b, forma[:j])
    x = {}
    for nome, hl in MEIAS_VIDAS:
        ew = _ewma(hist["ew"][nome], var_aj, hl)[-1]
        x[f"log_ew_{nome}"] = 0.5 * np.log(ew)
    x["log_l30"] = 0.5 * np.log(max(np.where(np.isnan(b[j - 6:j]), 0.0, b[j - 6:j] ** 2).sum(), 1e-6))
    ja = [v for v in hist["janelas"][sl] if v and v > 0]
    ult = hist["janelas"][sl][-1] if hist["janelas"][sl] else np.nan
    if not (ult and ult > 0) or len(ja) < 3:
        return None
    x["log_mesma_d1"] = float(np.log(ult))
    x["log_mesma_m5"] = float(np.log(np.mean(ja)))
    return x if all(np.isfinite(v) for v in x.values()) else None


def atualizar_barras(db_path, dias=10):
    """Traz as barras de 1 min recentes do MT5 (e corrige a escala se houve rolagem do WIN/WDO)."""
    import banco
    import backfill_barras as bb
    db = banco.abrir(db_path, bb.SCHEMA)
    fim = datetime.utcnow().replace(second=0, microsecond=0)
    ini = fim - timedelta(days=dias)
    try:
        bb.conferir_rolagem(db)  # vencimento do WIN/WDO: o MT5 reescreve o historico na escala do contrato novo
        bb.backfill_mt5(db, ini, fim)
    except BaseException as e:  # MT5 fora: treina com o que ja existe
        print(f"aviso: barras do MT5 nao atualizadas ({e})", flush=True)
    db.close()


def ajustar(df, alvo, cols):
    d = df[[alvo] + cols].replace([np.inf, -np.inf], np.nan).dropna()  # log(0) de janela sem negocio (leilao/halt de 26/10)
    A = np.column_stack([np.ones(len(d)), d[cols].to_numpy()])
    b = np.linalg.lstsq(A, d[alvo].to_numpy(), rcond=None)[0]
    res = d[alvo].to_numpy() - A @ b
    r2 = 1 - res.var() / d[alvo].var()
    return dict(intercepto=float(b[0]), coef=dict(zip(cols, map(float, b[1:]))), r2=float(r2),
                desvio_res=float(res.std()), n=int(len(d)))


def treinar(con):
    win, wdo, di = (rg.carregar(con, s) for s in ("WIN$", "WDO$", "DI1F29"))
    dias = sorted(set(win.index.normalize()))
    rv_dia, linhas, blocos = {}, [], {}
    for d in dias:
        g = rg.grade(d, rg.INICIO, rg.FIM)
        pw = rg.na_grade(win, g)
        if pw.notna().sum() < 300:
            continue
        rv_dia[d] = rg.rv(pw)
        blocos[d] = blocos_de(pw)
        pwdo = rg.na_grade(wdo, g)
        for sl in SLOTS:
            t = d + pd.Timedelta(sl + ":00")
            ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
            linhas.append(dict(
                dia=d, slot=sl,
                rv_fut=rg.rv(pw[t:t + pd.Timedelta("59min")]),
                rv_pas=rg.rv(pw[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")]),
                rv_ant=rg.rv(ant) if ant.notna().sum() >= 30 else np.nan,
                wdo_rv_pas=rg.rv(pwdo[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")])))
    df = pd.DataFrame(linhas)
    diaria = pd.Series(rv_dia).sort_index()
    ant = diaria.shift(1)
    hist = pd.DataFrame({"log_d1": np.log(ant), "log_d5": np.log(ant.rolling(5, min_periods=3).mean()),
                         "log_d20": np.log(ant.rolling(20, min_periods=10).mean()), "log_rv_dia": np.log(diaria)})
    df = df.merge(hist.rename_axis("dia").reset_index(), on="dia", how="left")
    for c in ("rv_fut", "rv_pas", "rv_ant"):
        df[f"log_{c}"] = np.log(df[c])
    df, forma = extras_treino(df, blocos)
    for sl in SLOTS[1:]:
        df[f"h_{sl}"] = (df["slot"] == sl).astype(float)
    hs = [f"h_{sl}" for sl in SLOTS[1:]]

    # dois modelos de hora: com a hora anterior (a partir de 11:05) e sem (10:05-11:04)
    base = hs + ["log_rv_pas"] + VAR_LONGAS
    modelo_hora_base = ajustar(df, "log_rv_fut", base + ["log_rv_ant"])  # sem EXTRAS (fallback ao vivo)
    modelo_hora = ajustar(df, "log_rv_fut", base + ["log_rv_ant"] + EXTRAS)
    modelo_hora_curto = ajustar(df, "log_rv_fut", base)
    modelo_dia = ajustar(hist.reset_index(), "log_rv_dia", VAR_LONGAS)

    di_dia = di.groupby(di.index.normalize()).last().diff().dropna() * 100  # pontos-base
    return dict(
        treinado_em=datetime.now().isoformat(timespec="seconds"),
        periodo=[str(dias[0].date()), str(dias[-1].date())], pregoes=len(rv_dia),
        slots=SLOTS, modelo_hora=modelo_hora, modelo_hora_base=modelo_hora_base, modelo_hora_curto=modelo_hora_curto,
        modelo_dia=modelo_dia, forma=[float(v) for v in forma],
        normal_win={sl: float(df.loc[df.slot == sl, "rv_pas"].median()) for sl in SLOTS},
        normal_wdo={sl: float(df.loc[df.slot == sl, "wdo_rv_pas"].median()) for sl in SLOTS},
        di29_abs_pct={str(p): float(di_dia.abs().quantile(p / 100)) for p in (50, 80, 95)},
        vol_diaria={str(k.date()): float(v) for k, v in diaria.tail(25).items()},
    )


def carregar_modelo():
    return json.loads(ARQ.read_text(encoding="utf-8")) if ARQ.exists() else None


def _prever(m, x):
    return m["intercepto"] + sum(c * x[k] for k, c in m["coef"].items())


def vol_longa(modelo, hoje):
    """log da vol diaria do WIN na vespera e medias de 5 e 20 dias (so dias antes de hoje)."""
    v = pd.Series({pd.Timestamp(k): x for k, x in modelo["vol_diaria"].items()}).sort_index()
    v = v[v.index < pd.Timestamp(hoje).normalize()]
    if len(v) < 10:
        return None
    return {"log_d1": np.log(v.iloc[-1]), "log_d5": np.log(v.tail(5).mean()), "log_d20": np.log(v.tail(20).mean())}


def slot_de(t):
    """Horario do modelo mais proximo (para baixo) de t, limitado a 10:05-17:15."""
    hhmm = t.strftime("%H:%M")
    validos = [s for s in SLOTS if s <= hhmm]
    return validos[-1] if validos else None


def _prever_hora(modelo, t, rv_pas, rv_ant, vix=None, extras=None):
    """Vol prevista (%) para os proximos 60 min. None antes de 10:05 ou sem dados. Com `extras` (extras_ao_vivo)
    usa o modelo com as medias moveis; sem eles, o modelo anterior."""
    sl, longa = slot_de(t), vol_longa(modelo, t)
    if sl is None or longa is None or not (rv_pas and rv_pas > 0):
        return None
    x = {f"h_{s}": float(s == sl) for s in SLOTS[1:]} | longa | {"log_rv_pas": np.log(rv_pas)}
    if vix and vix > 0:  # so modelos treinados antes de 19/10/2026 usam o VIX; sem ele (e sem a chave) a previsao e None
        x["log_vix"] = np.log(vix)
    if rv_ant and rv_ant > 0:
        x["log_rv_ant"] = np.log(rv_ant)
        if "log_ew_1h" in modelo["modelo_hora"]["coef"]:
            if extras:
                return float(np.exp(_prever(modelo["modelo_hora"], x | extras)))
            return float(np.exp(_prever(modelo["modelo_hora_base"], x)))
        return float(np.exp(_prever(modelo["modelo_hora"], x)))
    return float(np.exp(_prever(modelo["modelo_hora_curto"], x)))


def prever_hora(modelo, t, rv_pas, rv_ant, vix=None, extras=None):
    """Ver _prever_hora. Modelo salvo com o VIX (antes de 19/10/2026) e VIX ausente -> None."""
    try:
        return _prever_hora(modelo, t, rv_pas, rv_ant, vix, extras)
    except KeyError:
        return None


def prever_dia(modelo, hoje):
    longa = vol_longa(modelo, hoje)
    return float(np.exp(_prever(modelo["modelo_dia"], longa))) if longa else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--sem-atualizar", action="store_true")
    args = ap.parse_args()
    if not args.sem_atualizar:
        atualizar_barras(args.db)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    m = treinar(con)
    ARQ.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"modelo salvo em {ARQ} | {m['pregoes']} pregoes {m['periodo']} | R2 (na amostra) hora "
          f"{m['modelo_hora']['r2']:.2f}, dia {m['modelo_dia']['r2']:.2f}", flush=True)


if __name__ == "__main__":
    main()
