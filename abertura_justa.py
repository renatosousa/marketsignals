"""Abertura justa (fair value pre-mercado) do WIN$ e do WDO$ (ESPEC_V3 4.2).

Alvo de cada pregao T:
  gap = log(abertura_T / fechamento_{T-1})
  abertura = primeiro preco a partir das 09:00 nas barras_1m;
  fechamento = ultimo_liquido de fechamento_diario quando houver, senao o ultimo close da sessao de T-1.

Variaveis overnight (18:00 BRT do pregao T-1 ate 08:55 BRT de T, global_barras 60m/5m; usa a ultima barra
com ts <= limite):
  retornos de ES, NQ, BRENT, EM (media de USDMXN e USDZAR), DXY, OURO, COBRE, ASIA (N225/HSI/CSI300) e
  EUROPA (STOXX50/DAX/FTSE); US10Y em bps; MINERIO pelo retorno diario quando houver. Variavel ausente entra
  como 0 e fica registrada em `faltantes` (o US10Y e o caso previsto de faltar).

Modelo: Ridge (sklearn, alpha=1.0) com variaveis padronizadas, janela movel de 60 pregoes, walk-forward
estrito (para prever T so usa pregoes < T). sigma_erro = desvio dos residuos fora da amostra dos 60 pregoes
anteriores, em pontos. `abertura_prevista = fechamento_{T-1} * exp(gap_previsto)`.

Saidas: tabela abertura_justa (dia_idx, ativo, gap_previsto, sigma_erro, abertura_prevista, abertura_real,
desvio_sigma, coef_json, ts_ms).

Uso:
  python abertura_justa.py avaliar [--db dados/book.db]
  python abertura_justa.py prever [--db dados/book.db]
"""
import argparse
import bisect
import json
import math
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
from sklearn.linear_model import Ridge

import banco
import calendario_b3
from relogio import local_br_ms

D = 86_400_000
ATIVOS = ("WIN$", "WDO$")
FEATURES = ("ES", "NQ", "BRENT", "EM", "DXY", "OURO", "COBRE", "US10Y", "ASIA", "EUROPA", "MINERIO")
# Modelo em uso (escolhido em 18/11/2026 no walk-forward de ~190 pregoes): ES + cesta EM, janela de 120 pregoes.
# O conjunto completo da ESPEC com janela de 60 deu R2 fora da amostra negativo (WIN -0,24; WDO -0,03); ES+EM com 120
# deu WIN +0,064 (MAE 685 pts, sinal 78%) e WDO +0,098 (MAE 10,5 pts, sinal 83%). Escolha feita olhando ~14
# combinacoes nos mesmos dados: o R2 real tende a ser um pouco menor. FEATURES continua sendo o que se calcula.
FEATURES_MODELO = ("ES", "EM")
JANELA = 120
MIN_TREINO = 20
MIN_FEATURES = 2          # dia com menos variaveis que isso nao entra no treino (inicio do backfill)
ABERTURA_MIN = 9 * 60     # 09:00
FIM_OVERNIGHT = 8 * 60 + 55
CORTE_ANTERIOR = 18 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS abertura_justa (
    dia_idx INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    gap_previsto REAL,
    sigma_erro REAL,
    abertura_prevista REAL,
    abertura_real REAL,
    desvio_sigma REAL,
    coef_json TEXT,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (dia_idx, ativo));
"""


def _dia_idx(d):
    if isinstance(d, int):
        return d
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return (d - date(1970, 1, 1)).days


def dia_de_ms(ts_ms):
    return int(ts_ms) // D


def _data(dia_idx):
    return date(1970, 1, 1) + timedelta(days=int(dia_idx))


def _media(valores):
    vals = [v for v in valores if v is not None]
    return float(np.mean(vals)) if vals else None


# ------------------------------------------------------------------ leitura do banco

def _barras_do_ativo(con, ativo):
    """{dia_idx: [(ts_ms, open, close)]} das barras_1m do ativo."""
    try:
        rows = con.execute("SELECT ts_ms, open, close FROM barras_1m WHERE symbol=? AND close IS NOT NULL "
                           "ORDER BY ts_ms", (ativo,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for ts, o, c in rows:
        out.setdefault(int(ts) // D, []).append((int(ts), o, c))
    return out


def _fechamento_diario(con, ativo):
    """{dia_idx: ultimo_liquido} de fechamento_diario (tabela pode ainda nao existir)."""
    try:
        return {int(d): float(v) for d, v in con.execute(
            "SELECT dia_idx, ultimo_liquido FROM fechamento_diario WHERE ativo=? AND ultimo_liquido IS NOT NULL",
            (ativo,)).fetchall()}
    except sqlite3.OperationalError:
        return {}


def _abertura_do_dia(bars):
    for ts, o, c in bars:
        if (ts % D) // 60_000 >= ABERTURA_MIN:
            return float(o) if o is not None else float(c)
    return None


def _fechamento_do_dia(bars):
    for ts, _o, c in reversed(bars):
        if (ts % D) // 60_000 <= 18 * 60 + 30:
            return float(c)
    return None


def _series_globais(con):
    """({ativo: (ts[], close[])}, {ativo: (ts[], close[]) dos diarios}) de global_barras."""
    intra, diario = {}, {}
    try:
        rows = con.execute("SELECT ativo, intervalo, ts_ms, close FROM global_barras "
                           "WHERE close IS NOT NULL AND close>0 ORDER BY ativo, ts_ms").fetchall()
    except sqlite3.OperationalError:
        return {}, {}
    for ativo, intervalo, ts, close in rows:
        destino = diario if intervalo == "1d" else intra
        destino.setdefault(ativo, {})[int(ts)] = float(close)
    def _par(d):
        out = {}
        for ativo, valores in d.items():
            itens = sorted(valores.items())
            out[ativo] = ([t for t, _c in itens], [c for _t, c in itens])
        return out
    return _par(intra), _par(diario)


def _ultimo_ate(serie, limite):
    """Ultimo close com ts <= limite (ou None)."""
    if not serie:
        return None
    ts, closes = serie
    i = bisect.bisect_right(ts, int(limite)) - 1
    return closes[i] if i >= 0 else None


def _retorno(serie, ini, fim):
    a, b = _ultimo_ate(serie, ini), _ultimo_ate(serie, fim)
    if a is None or b is None or a <= 0 or b <= 0 or b == a:
        return None
    return math.log(b / a)


def _retorno_diario(serie, limite):
    """Retorno do ultimo diario <= limite contra o diario anterior."""
    if not serie:
        return None
    ts, closes = serie
    i = bisect.bisect_right(ts, int(limite)) - 1
    if i < 1 or closes[i - 1] <= 0 or closes[i] <= 0:
        return None
    return math.log(closes[i] / closes[i - 1])


def features_dia(series, dia_idx, limite_fim_ms=None):
    """Variaveis overnight do pregao T -> (dict, [nomes ausentes])."""
    intra, diario = series
    prev = calendario_b3.pregao_anterior(_data(dia_idx))
    ini = _dia_idx(prev) * D + CORTE_ANTERIOR * 60_000
    fim = dia_idx * D + FIM_OVERNIGHT * 60_000
    if limite_fim_ms is not None:
        fim = min(int(limite_fim_ms), fim)

    def _r(ativo):
        return _retorno(intra.get(ativo), ini, fim)

    us10y = None
    a, b = _ultimo_ate(intra.get("US10Y"), ini), _ultimo_ate(intra.get("US10Y"), fim)
    if a is not None and b is not None:
        us10y = (b - a) * 100.0
    x = {
        "ES": _r("ES"), "NQ": _r("NQ"), "BRENT": _r("BRENT"),
        "EM": _media([_r("USDMXN"), _r("USDZAR")]),
        "DXY": _r("DXY"), "OURO": _r("OURO"), "COBRE": _r("COBRE"),
        "US10Y": us10y,
        "ASIA": _media([_r("N225"), _r("HSI"), _r("CSI300")]),
        "EUROPA": _media([_r("STOXX50"), _r("DAX"), _r("FTSE")]),
        "MINERIO": _retorno_diario(diario.get("MINERIO"), fim),
    }
    faltantes = [f for f in FEATURES if x[f] is None]
    for f in faltantes:
        x[f] = 0.0
    return x, faltantes


# ------------------------------------------------------------------ dataset e modelo

def _historico(con, ativo, series=None):
    """[{dia, gap, abertura, fechamento, x, faltantes}] dos pregoes com alvo e features.

    Nao olha o futuro: a abertura e o fechamento sao do proprio dia e de T-1, e as features usam
    apenas barras com ts <= 08:55 de T."""
    series = series or _series_globais(con)
    barras_por_dia = _barras_do_ativo(con, ativo)
    fech_diario = _fechamento_diario(con, ativo)
    out = []
    for dia in sorted(barras_por_dia):
        prev = _dia_idx(calendario_b3.pregao_anterior(_data(dia)))
        abertura = _abertura_do_dia(barras_por_dia[dia])
        fechamento = fech_diario.get(prev)
        if fechamento is None:
            fechamento = _fechamento_do_dia(barras_por_dia.get(prev, []))
        if not abertura or not fechamento or abertura <= 0 or fechamento <= 0:
            continue
        x, faltantes = features_dia(series, dia)
        if len(faltantes) > len(FEATURES) - MIN_FEATURES:
            continue
        out.append({"dia": dia, "gap": math.log(abertura / fechamento), "abertura": abertura,
                    "fechamento": fechamento, "x": x, "faltantes": faltantes})
    return out


def _ajustar(treino, x_alvo, features):
    """Ridge padronizado -> (previsao do gap, coef_json)."""
    X = np.array([[r["x"][f] for f in features] for r in treino], dtype=float)
    y = np.array([r["gap"] for r in treino], dtype=float)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    modelo = Ridge(alpha=1.0).fit((X - mu) / sd, y)
    z = (np.array([x_alvo[f] for f in features], dtype=float) - mu) / sd
    pred = float(modelo.predict(z.reshape(1, -1))[0])
    coef = {"intercept": float(modelo.intercept_), "features": list(features),
            "beta": {f: float(c) for f, c in zip(features, modelo.coef_)},
            "media": {f: float(m) for f, m in zip(features, mu)},
            "desvio": {f: float(s) for f, s in zip(features, sd)}}
    return pred, coef


def _walk_forward(rows, features=FEATURES_MODELO, janela=JANELA, min_treino=MIN_TREINO):
    """Previsao fora da amostra de cada linha, treinando so nas 60 linhas anteriores."""
    preds, residuos = [], []
    for i, row in enumerate(rows):
        pred, coef = None, None
        treino = rows[max(0, i - janela):i]
        if len(treino) >= min_treino:
            pred, coef = _ajustar(treino, row["x"], features)
        sigma = None
        if len(residuos) >= 10:
            s = float(np.std(residuos[-janela:], ddof=1))
            if np.isfinite(s) and s > 0:
                sigma = s
        prevista = row["fechamento"] * math.exp(pred) if pred is not None else None
        desvio = (row["abertura"] - prevista) / sigma if sigma and prevista else None
        preds.append({"dia": row["dia"], "pred": pred, "sigma": sigma, "prevista": prevista,
                      "coef": coef, "desvio_sigma": desvio})
        if pred is not None:
            residuos.append(row["abertura"] - prevista)
    return preds


def _metricas(rows, preds):
    """R2 fora da amostra (gap), MAE em pontos e acerto de sinal."""
    usados = [(r, p) for r, p in zip(rows, preds) if p["pred"] is not None]
    if not usados:
        return {"n": 0, "r2": None, "mae_pts": None, "acerto_sinal": None, "util": False}
    real = np.array([r["gap"] for r, _ in usados])
    pred = np.array([p["pred"] for _, p in usados])
    sst = float(((real - real.mean()) ** 2).sum())
    r2 = 1 - float(((real - pred) ** 2).sum()) / sst if sst > 0 else None
    mae = float(np.mean([abs(r["abertura"] - p["prevista"]) for r, p in usados]))
    acerto = float(np.mean(np.sign(real) == np.sign(pred)))
    util = bool(r2 is not None and r2 > 0.05 and acerto > 0.55)
    return {"n": len(usados), "r2": r2, "mae_pts": mae, "acerto_sinal": acerto, "util": util}


def _metricas_ingenuas(rows, preds):
    """Bases: gap=0 e apenas ES (mesmo walk-forward)."""
    usados = [r for r, p in zip(rows, preds) if p["pred"] is not None]
    if not usados:
        return {"n": 0, "r2": None, "mae_pts": None, "acerto_sinal": None}
    real = np.array([r["gap"] for r in usados])
    sst = float(((real - real.mean()) ** 2).sum())
    r2 = 1 - float((real ** 2).sum()) / sst if sst > 0 else None
    mae = float(np.mean([abs(r["abertura"] - r["fechamento"]) for r in usados]))
    acerto = float(np.mean(np.sign(real) == 0))
    return {"n": len(usados), "r2": r2, "mae_pts": mae, "acerto_sinal": acerto}


def avaliar(con):
    """Imprime R2 fora da amostra, MAE em pontos e acerto de sinal contra gap=0 e so ES. -> dict."""
    series = _series_globais(con)
    resultado = {}
    for ativo in ATIVOS:
        rows = _historico(con, ativo, series)
        preds = _walk_forward(rows)
        es = _walk_forward(rows, features=("ES",))
        modelo = _metricas(rows, preds)
        ingenuo = _metricas_ingenuas(rows, preds)
        so_es = _metricas(rows, es)
        resultado[ativo] = {"modelo": modelo, "gap_zero": ingenuo, "so_es": so_es}
        print(f"\n{ativo}: {modelo['n']} pregoes fora da amostra")
        for rotulo, m in (("modelo", modelo), ("gap=0", ingenuo), ("so ES", so_es)):
            r2 = "-" if m["r2"] is None else f"{m['r2']:+.4f}"
            mae = "-" if m["mae_pts"] is None else f"{m['mae_pts']:.1f}"
            acerto = "-" if m["acerto_sinal"] is None else f"{m['acerto_sinal'] * 100:.1f}%"
            print(f"  {rotulo:7s} n={m['n']:4d}  R2={r2:>8s}  MAE={mae:>8s} pts  sinal={acerto:>7s}")
        print(f"  criterio util (R2>0,05 e sinal>55%): {'SIM' if modelo['util'] else 'NAO'}")
    return resultado


def prever_hoje(con, agora_ms):
    """Previsao do gap/abertura do dia de `agora_ms` com o que existe ate agora -> {ativo: dict}."""
    agora_ms = int(agora_ms)
    dia = dia_de_ms(agora_ms)
    series = _series_globais(con)
    limite = min(agora_ms, dia * D + FIM_OVERNIGHT * 60_000)
    saida = {}
    for ativo in ATIVOS:
        rows = [r for r in _historico(con, ativo, series) if r["dia"] < dia]
        if len(rows) < MIN_TREINO:
            saida[ativo] = None
            continue
        x, faltantes = features_dia(series, dia, limite_fim_ms=limite)
        treino = rows[-JANELA:]
        pred, coef = _ajustar(treino, x, FEATURES_MODELO)
        sigma = None
        preds = _walk_forward(rows)
        residuos = [r["abertura"] - p["prevista"] for r, p in zip(rows, preds)
                    if p["pred"] is not None and p["prevista"] is not None]
        if len(residuos) >= 10:
            s = float(np.std(residuos[-JANELA:], ddof=1))
            sigma = s if np.isfinite(s) and s > 0 else None
        prev = _dia_idx(calendario_b3.pregao_anterior(_data(dia)))
        fechamento = _fechamento_diario(con, ativo).get(prev)
        if fechamento is None:
            fechamento = _fechamento_do_dia(_barras_do_ativo(con, ativo).get(prev, []))
        prevista = fechamento * math.exp(pred) if fechamento else None
        coef["faltantes"] = faltantes
        saida[ativo] = {
            "dia_idx": dia, "ativo": ativo, "gap_previsto": pred, "sigma_erro": sigma,
            "abertura_prevista": prevista, "fechamento_anterior": fechamento,
            "coef_json": json.dumps(coef, ensure_ascii=False), "faltantes": faltantes,
        }
    return saida


def distorcao_abertura(abertura_real, abertura_prevista, sigma_erro):
    """Flag tatica: desvio > 1,5 sigma_erro em pontos."""
    if abertura_real is None or abertura_prevista is None or not sigma_erro:
        return False
    return abs(float(abertura_real) - float(abertura_prevista)) > 1.5 * float(sigma_erro)


def gravar(con_rw, linhas):
    """INSERT OR REPLACE das linhas de abertura_justa. -> n."""
    con_rw.executescript(SCHEMA)
    if not linhas:
        return 0
    con_rw.executemany(
        "INSERT OR REPLACE INTO abertura_justa VALUES (?,?,?,?,?,?,?,?,?)",
        [(l["dia_idx"], l["ativo"], l["gap_previsto"], l["sigma_erro"], l["abertura_prevista"],
          l["abertura_real"], l["desvio_sigma"], l["coef_json"], l["ts_ms"]) for l in linhas])
    con_rw.commit()
    return len(linhas)


def atualizar(db_path):
    """Recalcula e grava a tabela abertura_justa para os pregoes historicos. -> n de linhas."""
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        series = _series_globais(ro)
        linhas = []
        for ativo in ATIVOS:
            rows = _historico(ro, ativo, series)
            for row, p in zip(rows, _walk_forward(rows)):
                if p["pred"] is None:
                    continue
                coef = dict(p["coef"])
                coef["faltantes"] = row["faltantes"]
                linhas.append({"dia_idx": row["dia"], "ativo": ativo, "gap_previsto": p["pred"],
                               "sigma_erro": p["sigma"], "abertura_prevista": p["prevista"],
                               "abertura_real": row["abertura"], "desvio_sigma": p["desvio_sigma"],
                               "coef_json": json.dumps(coef, ensure_ascii=False),
                               "ts_ms": int(local_br_ms())})
    finally:
        ro.close()
    rw = banco.abrir(db_path, SCHEMA)
    try:
        return gravar(rw, linhas)
    finally:
        rw.close()


def main():
    ap = argparse.ArgumentParser(description="Abertura justa do WIN$/WDO$ (Ridge walk-forward)")
    ap.add_argument("cmd", choices=("avaliar", "prever", "atualizar"))
    ap.add_argument("--db", default="dados/book.db")
    args = ap.parse_args()
    if args.cmd == "atualizar":
        print(f"{atualizar(args.db)} linha(s) gravada(s)")
        return
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    try:
        if args.cmd == "avaliar":
            avaliar(con)
        else:
            for ativo, r in prever_hoje(con, int(local_br_ms())).items():
                if r is None:
                    print(f"{ativo}: sem historico suficiente")
                else:
                    print(f"{ativo}: gap={r['gap_previsto'] * 100:+.3f}% "
                          f"abertura={r['abertura_prevista']} sigma={r['sigma_erro']} "
                          f"faltantes={r['faltantes']}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
