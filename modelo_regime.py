"""Modelo de regime do WIN (resultado do estudo_regime.py), treinado com todo o historico.

  hora : log(vol da proxima hora) ~ horario + vol do WIN na ultima hora (e na anterior) +
         vol diaria do WIN (vespera, 5 e 20 dias) + log(VIX). R2 fora da amostra ~0,76 no estudo.
  dia  : log(vol do dia) ~ vol diaria do WIN (vespera, 5 e 20 dias). R2 ~0,47.
  normais: vol tipica de 60 min em cada horario (WIN e WDO), para a vol relativa.
  DI   : distribuicao da variacao diaria do DI1F29, para dizer se o dia de hoje e atipico.

O macro entra como contexto (so o VIX mostrou ganho, pequeno); direcao e tendencia nao sao
previsiveis (ver estudo) e por isso nao ha modelo para elas.

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


def atualizar_barras(db_path, dias=10):
    """Traz as barras de 1 min recentes (MT5 e, se o Gateway estiver aberto, VIX da IBKR)."""
    import banco
    import backfill_barras as bb
    db = banco.abrir(db_path, bb.SCHEMA)
    fim = datetime.utcnow().replace(second=0, microsecond=0)
    ini = fim - timedelta(days=dias)
    try:
        bb.backfill_mt5(db, ini, fim)
    except BaseException as e:  # MT5 fora: treina com o que ja existe
        print(f"aviso: barras do MT5 nao atualizadas ({e})", flush=True)
    try:
        from ib_async import IB, Index
        from datetime import timezone
        ib = IB()
        ib.connect("127.0.0.1", 4001, clientId=16, readonly=True, timeout=15)
        c = Index("VIX", "CBOE")
        ib.qualifyContracts(c)
        barras = bb.barras_ib(ib, c, ini.replace(tzinfo=timezone.utc), fim.replace(tzinfo=timezone.utc))
        bb.gravar(db, bb.linhas_ib("IB:VIX", "VIX", barras))
        ib.disconnect()
        print(f"VIX: {len(barras)} barras recentes", flush=True)
    except Exception as e:
        print(f"aviso: VIX da IBKR nao atualizado ({e.__class__.__name__})", flush=True)
    db.close()


def ajustar(df, alvo, cols):
    d = df[[alvo] + cols].dropna()
    A = np.column_stack([np.ones(len(d)), d[cols].to_numpy()])
    b = np.linalg.lstsq(A, d[alvo].to_numpy(), rcond=None)[0]
    res = d[alvo].to_numpy() - A @ b
    r2 = 1 - res.var() / d[alvo].var()
    return dict(intercepto=float(b[0]), coef=dict(zip(cols, map(float, b[1:]))), r2=float(r2),
                desvio_res=float(res.std()), n=int(len(d)))


def treinar(con):
    win, vix, wdo, di = (rg.carregar(con, s) for s in ("WIN$", "IB:VIX", "WDO$", "DI1F29"))
    dias = sorted(set(win.index.normalize()))
    rv_dia, linhas = {}, []
    for d in dias:
        g = rg.grade(d, rg.INICIO, rg.FIM)
        pw = rg.na_grade(win, g)
        if pw.notna().sum() < 300:
            continue
        rv_dia[d] = rg.rv(pw)
        pwdo = rg.na_grade(wdo, g)
        for sl in SLOTS:
            t = d + pd.Timedelta(sl + ":00")
            ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
            linhas.append(dict(
                dia=d, slot=sl,
                rv_fut=rg.rv(pw[t:t + pd.Timedelta("59min")]),
                rv_pas=rg.rv(pw[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")]),
                rv_ant=rg.rv(ant) if ant.notna().sum() >= 30 else np.nan,
                wdo_rv_pas=rg.rv(pwdo[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")]),
                vix=rg.preco_em(vix, t, "3D")))
    df = pd.DataFrame(linhas)
    diaria = pd.Series(rv_dia).sort_index()
    ant = diaria.shift(1)
    hist = pd.DataFrame({"log_d1": np.log(ant), "log_d5": np.log(ant.rolling(5, min_periods=3).mean()),
                         "log_d20": np.log(ant.rolling(20, min_periods=10).mean()), "log_rv_dia": np.log(diaria)})
    df = df.merge(hist.rename_axis("dia").reset_index(), on="dia", how="left")
    for c in ("rv_fut", "rv_pas", "rv_ant", "vix"):
        df[f"log_{c}"] = np.log(df[c])
    for sl in SLOTS[1:]:
        df[f"h_{sl}"] = (df["slot"] == sl).astype(float)
    hs = [f"h_{sl}" for sl in SLOTS[1:]]

    # dois modelos de hora: com a hora anterior (a partir de 11:05) e sem (10:05-11:04)
    base = hs + ["log_rv_pas"] + VAR_LONGAS + ["log_vix"]
    modelo_hora = ajustar(df, "log_rv_fut", base + ["log_rv_ant"])
    modelo_hora_curto = ajustar(df, "log_rv_fut", base)
    modelo_dia = ajustar(hist.reset_index(), "log_rv_dia", VAR_LONGAS)

    di_dia = di.groupby(di.index.normalize()).last().diff().dropna() * 100  # pontos-base
    return dict(
        treinado_em=datetime.now().isoformat(timespec="seconds"),
        periodo=[str(dias[0].date()), str(dias[-1].date())], pregoes=len(rv_dia),
        slots=SLOTS, modelo_hora=modelo_hora, modelo_hora_curto=modelo_hora_curto, modelo_dia=modelo_dia,
        normal_win={sl: float(df.loc[df.slot == sl, "rv_pas"].median()) for sl in SLOTS},
        normal_wdo={sl: float(df.loc[df.slot == sl, "wdo_rv_pas"].median()) for sl in SLOTS},
        di29_abs_pct={p: float(di_dia.abs().quantile(p / 100)) for p in (50, 80, 95)},
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


def prever_hora(modelo, t, rv_pas, rv_ant, vix):
    """Vol prevista (%) para os proximos 60 min. None antes de 10:05 ou sem dados."""
    sl, longa = slot_de(t), vol_longa(modelo, t)
    if sl is None or longa is None or not (rv_pas and rv_pas > 0 and vix and vix > 0):
        return None
    x = {f"h_{s}": float(s == sl) for s in SLOTS[1:]} | longa | {"log_rv_pas": np.log(rv_pas), "log_vix": np.log(vix)}
    if rv_ant and rv_ant > 0:
        x["log_rv_ant"] = np.log(rv_ant)
        return float(np.exp(_prever(modelo["modelo_hora"], x)))
    return float(np.exp(_prever(modelo["modelo_hora_curto"], x)))


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
          f"{m['modelo_hora']['r2']:.2f}, dia {m['modelo_dia']['r2']:.2f} | coef VIX "
          f"{m['modelo_hora']['coef']['log_vix']:+.3f}", flush=True)


if __name__ == "__main__":
    main()
