"""As zonas de confluencia do WIN$ tem reacao melhor que niveis aleatorios? (ESPEC_V3.md 9.2).

O motor_regioes.py e uma heuristica: pesos de 20 a 30 por fonte, piso de forca 35, cluster de 0,12 ATR.
Este estudo mede, fora da amostra, se a zona acrescenta alguma coisa ao simples fato de estar a uma
certa distancia do preco:

1. Reconstrucao CAUSAL das zonas: a cada 30 min do pregao (10:00 a 17:00) chama motor_regioes.regioes
   com agora_ms historico. O que nao existia no passado fica de fora naturalmente: posicao em aberto de
   opcoes so a partir de 28/10 (D-1 da B3), saldo do Tryd so ate 13/11, e o modelo_regime_v3.json e
   desligado (foi treinado no historico inteiro; usa-lo em janeiro seria olhar o futuro). O vies do
   motor_cenario tambem nao existe no passado (a tabela cenario_scores nasce com ele), entao o estudo
   usa um proxy causal de tendencia: z do retorno de 5 e de 20 pregoes dividido pela vol diaria,
   mapeado por 100 x tanh(z/1,5), a mesma forma da ESPEC 5.2.
2. Primeiro toque de cada zona depois da criacao (preco entra na faixa): entrada limite na borda por
   onde o preco entra (preco_min nas zonas acima, preco_max nas abaixo), MFE e MAE em 15, 30 e 60 min
   na direcao da operacao da zona (compra em suporte, venda em resistencia), com 5 pontos de custo por
   lado, e o retorno no fechamento do horizonte. A zona que persiste de um slot para o outro herda o
   estado (nao conta o mesmo toque duas vezes).
3. Benchmark Monte Carlo: para cada toque, 200 niveis aleatorios do MESMO lado e a mesma distancia do
   preco no instante da criacao ate a entrada da zona (a borda por onde o preco entra; jitter de +-25%
   na distancia, para haver sorteio), com o mesmo calculo de MFE/MAE ao serem tocados (entrada limite no
   nivel, no primeiro toque da sessao).
4. Resultado por faixa de forca (35-59, 60-79, 80+), por alinhamento (a favor x contra) e com/sem o
   filtro de gatilho (gatilhos_execucao.estado_zona: o toque so conta se virar GATILHO_CONFIRMADO em
   ate 5 min; so ha fita do Tryd a partir de 30/10, e o n diz isso).
5. Diferenca testada por bootstrap de dias (1.000 reamostragens com reposicao), porque toques do mesmo
   dia nao sao independentes.
6. Validacao das bandas: cobertura walk-forward de [q15,q85] e [q05,q95] do modelo_regime_v3 contra a
   meta (70% e 90%), reusando modelo_regime_v3.avaliar (custa ~2 min; --sem-bandas pula).

Uso: python estudo_regioes.py [--dias N] [--slots 30] [--random 200] [--bootstrap 1000] [--sem-bandas]
Grava dados/estudo_regioes.csv (uma linha por faixa x alinhamento x filtro x horizonte). Somente leitura.
"""
import argparse
import contextlib
import io
import math
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import calendario_b3 as cal
import gatilhos_execucao as ge
import motor_regioes as mr

DIA_MS = 86_400_000
HORIZONTES = (15, 30, 60)
CUSTO_LADO = 5.0
N_RANDOM = 200
N_BOOTSTRAP = 1000
JITTER = 0.25
MAX_TOQUE_MIN = 120
FAIXAS = ((35.0, 60.0, "35-59"), (60.0, 80.0, "60-79"), (80.0, 1e9, "80+"))
ALINHAMENTOS = ("A_FAVOR_TENDENCIA", "CONTRA_TENDENCIA", "ALVO/NEUTRA")
ARQ_CSV = Path(__file__).resolve().parent / "dados" / "estudo_regioes.csv"
SESSAO = (540, 1110)          # 09:00 a 18:30 em minutos do dia
DIAS_MIN_BARRAS = 120         # dia com menos minutos que isso nao entra


# ------------------------------------------------------------------ dados

def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _dias_estudo(con, ativo="WIN$", limite=None):
    """Dias com snapshots do ativo, em ordem crescente (respeita `limite`).

    O motor_regioes.preco_atual le `snapshots`; sem snapshots do dia nao ha preco no instante e
    `regioes` devolve lista vazia. As barras_1m de janeiro a setembro servem para ATR/perfil composto,
    mas nao permitem reconstruir a zona (nao ha o preco de referencia do momento), entao o estudo
    comeca em 24/09/2026, quando o coletor de livro passou a gravar.
    """
    dias = set()
    try:
        for (d,) in con.execute("SELECT DISTINCT ts_ms / 86400000 FROM snapshots WHERE symbol=?", (ativo,)):
            if d is not None:
                dias.add(int(d))
    except sqlite3.OperationalError:
        return []
    out = sorted(dias)
    return out[-limite:] if limite else out


def _dias_com_preco(con, ativo="WIN$", limite=None):
    """Dias com barras OU snapshots (para os fechamentos diarios e o proxy de vies)."""
    dias = set()
    for tabela in ("barras_1m", "snapshots"):
        try:
            for (d,) in con.execute(f"SELECT DISTINCT ts_ms / 86400000 FROM {tabela} WHERE symbol=?", (ativo,)):
                if d is not None:
                    dias.add(int(d))
        except sqlite3.OperationalError:
            continue
    out = sorted(dias)
    return out[-limite:] if limite else out


def _ohlc_minuto(con, ativo, dia):
    """Arrays densos por minuto do pregao: high, low, close e a posicao do minuto.

    Prefere barras_1m (OHLC real); sem elas (o dia corrente antes do backfill) agrega os snapshots por
    minuto (max/min/ultimo do ultimo negocio). -> dict(ini, high, low, close) com NaN fora do pregao.
    """
    ini, fim = SESSAO
    n = fim - ini + 1
    high = np.full(n, np.nan)
    low = np.full(n, np.nan)
    close = np.full(n, np.nan)
    t0 = int(dia) * DIA_MS + ini * 60_000
    df = pd.DataFrame()
    try:
        df = pd.read_sql("SELECT ts_ms, high, low, close FROM barras_1m WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                         "AND close > 0 ORDER BY ts_ms", con,
                         params=(ativo, t0, int(dia) * DIA_MS + fim * 60_000))
    except sqlite3.OperationalError:
        df = pd.DataFrame()
    if df.empty:
        try:
            df = pd.read_sql("SELECT (ts_ms / 60000) * 60000 ts_ms, max(last) high, min(last) low, "
                             "max(ts_ms) ultimo FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                             "AND spread > 0 AND last > 0 GROUP BY ts_ms / 60000 ORDER BY ts_ms", con,
                             params=(ativo, t0, int(dia) * DIA_MS + fim * 60_000))
        except sqlite3.OperationalError:
            df = pd.DataFrame()
        if not df.empty:
            # o close do minuto e o ultimo negocio visto nele
            ult = pd.read_sql("SELECT ts_ms, last FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                              "AND spread > 0 AND last > 0 ORDER BY ts_ms", con,
                              params=(ativo, t0, int(dia) * DIA_MS + fim * 60_000))
            if not ult.empty:
                ult["m"] = (ult.ts_ms // 60_000) * 60_000
                df = df.merge(ult.groupby("m").last().rename(columns={"last": "close"}),
                              left_on="ts_ms", right_index=True, how="left")
                df["close"] = df.close.fillna(df.high)
    if df.empty:
        return None
    pos = ((df.ts_ms.to_numpy(np.int64) // 60_000) - (int(dia) * 1440 + ini)).astype(int)
    ok = (pos >= 0) & (pos < n)
    if ok.sum() < DIAS_MIN_BARRAS:
        return None
    high[pos[ok]] = df.high.to_numpy(float)[ok]
    low[pos[ok]] = df.low.to_numpy(float)[ok]
    close[pos[ok]] = df.close.to_numpy(float)[ok] if "close" in df else df.high.to_numpy(float)[ok]
    return dict(ini=ini, high=high, low=low, close=close)


def _fwd(serie, h):
    """Maximo (ou minimo) da serie em (i, i+h], por i; NaN quando nao ha nenhum valor na janela."""
    s = pd.Series(serie)
    return s.rolling(h, min_periods=1).max().shift(-h).to_numpy(float)


def _fwd_min(serie, h):
    s = pd.Series(serie)
    return s.rolling(h, min_periods=1).min().shift(-h).to_numpy(float)


def _fechamentos_diarios(con, ativo, n):
    """Fechamentos diarios do ativo (barras/snapshots) para o proxy de vies, do mais antigo ao novo."""
    out = []
    dias = _dias_com_preco(con, ativo, n)
    for d in dias:
        o = _ohlc_minuto(con, ativo, d)
        if o is None:
            continue
        c = o["close"][~np.isnan(o["close"])]
        if len(c):
            out.append((d, float(c[-1])))
    return out


def _vies_proxy(closes, dia):
    """Proxy causal do vies: z do retorno de 5 e 20 pregoes / vol diaria, 100 x tanh(z/1,5)."""
    d = [(k, v) for k, v in closes if k < dia]
    if len(d) < 25:
        return 0.0
    p = np.array([v for _k, v in d][-80:], dtype=float)
    r = np.diff(np.log(p))
    sd = float(np.std(r[-60:], ddof=1)) if len(r) >= 20 else None
    if not sd or sd <= 0:
        return 0.0
    z5 = math.log(p[-1] / p[-6]) / (sd * math.sqrt(5))
    z20 = math.log(p[-1] / p[-21]) / (sd * math.sqrt(20))
    return float(100.0 * math.tanh(((z5 + z20) / 2.0) / 1.5))


# ------------------------------------------------------------------ zonas e toques

def _slots(passo_min=30):
    """Minutos do dia dos slots de reconstrucao (10:00 a 17:00)."""
    ini, fim = 10 * 60, 17 * 60 + 1
    return list(range(ini, fim, int(passo_min)))


def _classe_alinhamento(alinhamento):
    return alinhamento if alinhamento in ("A_FAVOR_TENDENCIA", "CONTRA_TENDENCIA") else "ALVO/NEUTRA"


def _toque(ohlc, de_min, ate_min, lado, preco_min, preco_max):
    """Primeiro minuto em (de_min, ate_min] em que o preco entra na faixa da zona -> posicao ou None."""
    ini = ohlc["ini"]
    a = max(int(de_min) - ini + 1, 0)
    b = min(int(ate_min) - ini + 1, len(ohlc["high"]))
    if b <= a:
        return None
    h, l = ohlc["high"][a:b], ohlc["low"][a:b]
    if lado == "ACIMA":
        mask = h >= preco_min
    else:
        mask = l <= preco_max
    if not mask.any():
        return None
    return a + int(mask.argmax())


def _medir(ohlc, pos, direcao, fmax, fmin, horizonte, entry=None):
    """(mfe, mae, ret) liquidos do toque no indice `pos` do array do dia.

    `entry` e o preco limite da entrada (a borda da zona ou o nivel aleatorio); sem ele, o fechamento
    do minuto do toque. O MFE/MAE conta de (pos, pos+horizonte], sem o minuto do toque.
    """
    close = ohlc["close"]
    if entry is None:
        entry = close[pos]
    if not np.isfinite(entry):
        return None
    j = pos + horizonte
    if j >= len(close):
        return None
    saida = close[j]
    if not np.isfinite(saida):
        return None
    alto, baixo = fmax[horizonte][pos], fmin[horizonte][pos]
    if not (np.isfinite(alto) and np.isfinite(baixo)):
        return None
    if direcao == "COMPRA":
        mfe, mae = alto - entry, entry - baixo
        ret = saida - entry
    else:
        mfe, mae = entry - baixo, alto - entry
        ret = entry - saida
    return float(mfe - CUSTO_LADO), float(mae + CUSTO_LADO), float(ret - 2 * CUSTO_LADO)


def _benchmark(ohlc, pos_criacao, distancia, lado, direcao, fmax, fmin, horizonte, rng, n_random=N_RANDOM,
               janela_min=None):
    """Media de (edge, ret) de `n_random` niveis aleatorios do mesmo lado, mesma distancia, no horizonte.

    O nivel aleatorio e medido no primeiro toque dele na sessao, como a zona (cada um espera o seu
    toque); `janela_min` limita a busca, mas o padrao e o resto do dia.
    """
    if not np.isfinite(ohlc["close"][pos_criacao]) or distancia <= 0:
        return None
    base = ohlc["close"][pos_criacao]
    sinal = 1.0 if lado == "ACIMA" else -1.0
    dist = distancia * (1.0 + rng.uniform(-JITTER, JITTER, int(n_random)))
    niveis = base + sinal * dist
    a = pos_criacao + 1
    b = len(ohlc["high"]) if janela_min is None else min(a + max(int(janela_min), 1), len(ohlc["high"]))
    if b <= a:
        return None
    if lado == "ACIMA":
        mask = ohlc["high"][a:b][None, :] >= niveis[:, None]
    else:
        mask = ohlc["low"][a:b][None, :] <= niveis[:, None]
    tem = mask.any(axis=1)
    if not tem.any():
        return None
    idx = a + mask.argmax(axis=1)
    idx = idx[tem]
    entrada = niveis[tem]
    close = ohlc["close"]
    j = idx + horizonte
    ok = (j < len(close)) & np.isfinite(close[np.minimum(j, len(close) - 1)])
    if not ok.any():
        return None
    idx, entrada, j = idx[ok], entrada[ok], j[ok]
    saida = close[j]
    alto, baixo = fmax[horizonte][idx], fmin[horizonte][idx]
    ok = np.isfinite(alto) & np.isfinite(baixo)
    if not ok.any():
        return None
    alto, baixo, entrada, saida = alto[ok], baixo[ok], entrada[ok], saida[ok]
    if direcao == "COMPRA":
        mfe, mae, ret = alto - entrada, entrada - baixo, saida - entrada
    else:
        mfe, mae, ret = entrada - baixo, alto - entrada, entrada - saida
    edge = (mfe - CUSTO_LADO) - (mae + CUSTO_LADO)
    ret = ret - 2 * CUSTO_LADO
    return float(np.mean(edge)), float(np.mean(ret)), int(len(edge))


def _eventos_dia(con, dia, ohlc, vies, cache, com_gatilho, passo_slot=30):
    """Zonas criadas nos slots do dia e o primeiro toque de cada uma depois da criacao."""
    atr_d = mr.atr_diario(con, "WIN$", dia, cache=cache)
    if not atr_d or atr_d <= 0:
        return []
    delta = mr.DELTA_ATR * atr_d
    ini = ohlc["ini"]
    eventos, ativos = [], []
    anterior = None
    for tmin in _slots(passo_slot):
        pos_slot = tmin - ini
        if pos_slot < 0 or pos_slot >= len(ohlc["close"]):
            continue
        if not np.isfinite(ohlc["close"][pos_slot]):
            continue
        agora = int(dia) * DIA_MS + tmin * 60_000
        zonas = mr.regioes(con, "WIN$", agora, vies)
        for z in zonas:
            par = next((a for a in ativos if not a["tocada"] and a["zona"]["lado"] == z["lado"]
                        and abs(a["zona"]["preco_centro"] - z["preco_centro"]) <= delta), None)
            if par is not None:
                par["ultimo_slot"] = tmin
            else:
                ativos.append(dict(zona=z, criada_min=tmin, preco_criacao=float(ohlc["close"][pos_slot]),
                                   ultimo_slot=tmin, tocada=False))
        # checa os toques na janela (slot anterior, slot atual] das zonas que ja existiam antes
        if anterior is not None:
            for a in ativos:
                if a["tocada"] or a["criada_min"] > anterior:
                    continue
                z = a["zona"]
                pos = _toque(ohlc, anterior, tmin, z["lado"], z["preco_min"], z["preco_max"])
                if pos is None:
                    continue
                a["tocada"] = True
                entrada = float(z["preco_min"] if z["lado"] == "ACIMA" else z["preco_max"])
                ev = dict(dia=int(dia), criada_min=a["criada_min"], toque_min=ini + pos, pos=pos,
                          lado=z["lado"], direcao=z["direcao"], alinhamento=z["alinhamento"],
                          forca=float(z["forca"]), centro=float(z["preco_centro"]),
                          preco_min=float(z["preco_min"]), preco_max=float(z["preco_max"]),
                          entrada=entrada, preco_criacao=a["preco_criacao"],
                          distancia=abs(entrada - a["preco_criacao"]),
                          componentes=list(z["componentes"]), confirmado=None)
                ev["id"] = f"{dia}-{ev['criada_min']}-{z['lado']}-{z['preco_centro']:.0f}"
                # nao conta o mesmo toque de novo (zona recriada logo depois no mesmo preco)
                dup = any(e["dia"] == ev["dia"] and e["lado"] == ev["lado"]
                          and abs(e["centro"] - ev["centro"]) <= delta
                          and abs(e["toque_min"] - ev["toque_min"]) <= 30 for e in eventos)
                if not dup:
                    eventos.append(ev)
        anterior = tmin
        ativos = [a for a in ativos if not a["tocada"]]
    if com_gatilho:
        for ev in eventos:
            ev["confirmado"] = _gatilho(con, ev)
    return eventos


def _gatilho(con, ev):
    """True se a zona vira GATILHO_CONFIRMADO em ate 5 min do toque (gatilhos_execucao)."""
    zona = dict(preco_min=ev["preco_min"], preco_centro=ev["centro"], preco_max=ev["preco_max"])
    for k in range(6):
        ts = int(ev["dia"]) * DIA_MS + (ev["toque_min"] + k) * 60_000
        try:
            r = ge.estado_zona(con, "WIN$", zona, ts, ev["direcao"])
        except Exception:
            return None
        if r["status"] == "GATILHO_CONFIRMADO":
            return True
    return False


# ------------------------------------------------------------------ agregacao

def _grupos(eventos):
    """{(faixa, alinhamento, filtro): [eventos]}."""
    out = {}
    for ev in eventos:
        for lo, hi, nome in FAIXAS:
            if lo <= ev["forca"] < hi:
                faixa = nome
                break
        else:
            continue
        al = _classe_alinhamento(ev["alinhamento"])
        chaves = [(faixa, al, "sem_filtro")]
        if ev.get("confirmado") is not None:
            chaves.append((faixa, al, "com_filtro" if ev["confirmado"] else "sem_confirmacao"))
        for k in chaves:
            out.setdefault(k, []).append(ev)
    return out


def _bootstrap(por_dia, n=N_BOOTSTRAP, seed=7):
    """Media da diferenca e IC95% por bootstrap de DIAS (reamostra o dia inteiro, com seus toques).

    `por_dia` e {dia: [diferencas]}; a estimativa e a media por toque (pooled) e o IC vem da
    reamostragem dos dias com reposicao, que preserva a dependencia entre toques do mesmo pregao.
    """
    grupos = [np.array(v, dtype=float) for v in por_dia.values() if len(v)]
    grupos = [g[np.isfinite(g)] for g in grupos if np.isfinite(g).any()]
    if len(grupos) < 3:
        return None
    todos = np.concatenate(grupos)
    rng = np.random.default_rng(seed)
    medias = np.empty(n)
    for i in range(n):
        escolha = rng.integers(0, len(grupos), len(grupos))
        medias[i] = np.concatenate([grupos[j] for j in escolha]).mean()
    lo, hi = np.percentile(medias, [2.5, 97.5])
    p = 2.0 * min(float((medias <= 0).mean()), float((medias >= 0).mean()))
    return dict(media=float(todos.mean()), lo=float(lo), hi=float(hi), p=float(min(p, 1.0)), n_dias=len(grupos))


def _tabela(eventos, horizonte):
    """Linhas (faixa, alinhamento, filtro) com zona x aleatorio, IC e p para um horizonte."""
    linhas = []
    for chave, evs in sorted(_grupos(eventos).items()):
        faixa, al, filtro = chave
        z_edge, z_ret, r_edge, r_ret, por_dia_edge, por_dia_ret = [], [], [], [], {}, {}
        for ev in evs:
            m = ev["m"].get(horizonte)
            if not m:
                continue
            z_edge.append(m["zona_edge"])
            z_ret.append(m["zona_ret"])
            r_edge.append(m["rand_edge"])
            r_ret.append(m["rand_ret"])
            por_dia_edge.setdefault(ev["dia"], []).append(m["zona_edge"] - m["rand_edge"])
            por_dia_ret.setdefault(ev["dia"], []).append(m["zona_ret"] - m["rand_ret"])
        if len(z_edge) < 3:
            continue
        be, br = _bootstrap(por_dia_edge), _bootstrap(por_dia_ret)
        linhas.append(dict(faixa=faixa, alinhamento=al, filtro=filtro, horizonte=horizonte,
                           n_toques=len(z_edge), n_dias=len(por_dia_edge),
                           zona_edge=float(np.mean(z_edge)), rand_edge=float(np.mean(r_edge)),
                           zona_ret=float(np.mean(z_ret)), rand_ret=float(np.mean(r_ret)),
                           diff_edge=be["media"] if be else None, ic_edge=(be["lo"], be["hi"]) if be else None,
                           p_edge=be["p"] if be else None,
                           diff_ret=br["media"] if br else None, ic_ret=(br["lo"], br["hi"]) if br else None,
                           p_ret=br["p"] if br else None))
    return linhas


def _fmt(x, casas=1):
    return "  n/d" if x is None else f"{x:+.1f}" if casas == 1 else f"{x:+.2f}"


def _imprimir(linhas, horizonte):
    print(f"\n=== horizonte {horizonte} min (custo 5 pts por lado; MFE/MAE na direcao da zona)")
    print("    diff = media por toque da diferenca zona - aleatorio; IC95% por bootstrap de dias")
    print(f"{'faixa':6} {'alinhamento':20} {'filtro':16} {'n':>5} {'dias':>5} "
          f"{'zona_edge':>10} {'rand_edge':>10} {'diff':>7} {'IC95':>17} {'p':>6} "
          f"{'zona_ret':>9} {'rand_ret':>9} {'diff':>7} {'IC95':>17} {'p':>6}")
    for l in linhas:
        ic = f"[{l['ic_edge'][0]:+.1f},{l['ic_edge'][1]:+.1f}]" if l["ic_edge"] else "n/d"
        icr = f"[{l['ic_ret'][0]:+.1f},{l['ic_ret'][1]:+.1f}]" if l["ic_ret"] else "n/d"
        print(f"{l['faixa']:6} {l['alinhamento']:20} {l['filtro']:16} {l['n_toques']:5d} {l['n_dias']:5d} "
              f"{l['zona_edge']:10.1f} {l['rand_edge']:10.1f} {_fmt(l['diff_edge']):>7} {ic:>17} "
              f"{_fmt(l['p_edge'], 3):>6} {l['zona_ret']:9.1f} {l['rand_ret']:9.1f} "
              f"{_fmt(l['diff_ret']):>7} {icr:>17} {_fmt(l['p_ret'], 3):>6}")


# ------------------------------------------------------------------ bandas

def _bandas(con):
    """Cobertura walk-forward do modelo_regime_v3 (se o modulo e o json existirem) -> dict."""
    arq = Path(__file__).resolve().parent / mr.ARQ_MODELO
    if not arq.exists():
        return None, "sem dados/modelo_regime_v3.json"
    try:
        import modelo_regime_v3 as m3
    except Exception as e:
        return None, f"modelo_regime_v3 indisponivel: {e.__class__.__name__}"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            res = m3.avaliar(con)
    except Exception as e:
        return None, f"avaliar falhou: {e.__class__.__name__}: {e}"
    out = {}
    for chave, nome in (("z60", "60 min"), ("zfech", "fechamento")):
        m = res.get(chave) or {}
        if not m.get("geral"):
            continue
        out[nome] = dict(geral=m["geral"], vol_alta=m.get("vol_alta"), vol_baixa=m.get("vol_baixa"),
                         n_dias=m.get("n_dias"))
    return out, None


def _imprimir_bandas(bandas, motivo):
    print("\n=== cobertura das bandas do modelo_regime_v3 (walk-forward, metas 70+-3% e 90+-3%)")
    if not bandas:
        print(f"  nao validado: {motivo}")
        return
    for nome, m in bandas.items():
        g = m["geral"]
        print(f"  {nome:11} n={g.get('n', 0):5d} dias={m.get('n_dias')}  [q15,q85]: {g.get('c15', np.nan):6.1%}"
              f"   [q05,q95]: {g.get('c05', np.nan):6.1%}")
        a, b = m.get("vol_alta") or {}, m.get("vol_baixa") or {}
        if a and b:
            print(f"      vol alta: {a.get('c15', np.nan):6.1%}/{a.get('c05', np.nan):6.1%}   "
                  f"vol baixa: {b.get('c15', np.nan):6.1%}/{b.get('c05', np.nan):6.1%}")


# ------------------------------------------------------------------ estudo

@contextlib.contextmanager
def _cache_barras():
    """Cache de barras por dia e desliga o modelo_regime_v3 (treinado no historico inteiro)."""
    cache_global = {}
    orig_dados, orig_modelo = mr._dados_dia, mr._modelo_v3

    def dados(con, ativo, dia, fim_ms=None, cache=None):
        if fim_ms is None:
            chave = (mr.normalizar(ativo), int(dia))
            if chave in cache_global:
                df = cache_global[chave]
                if cache is not None:
                    cache[chave] = df
                return df
            df = orig_dados(con, ativo, dia, fim_ms, cache)
            cache_global[chave] = df
            return df
        return orig_dados(con, ativo, dia, fim_ms, cache)

    mr._dados_dia = dados
    mr._modelo_v3 = lambda: None
    try:
        yield cache_global
    finally:
        mr._dados_dia = orig_dados
        mr._modelo_v3 = orig_modelo


def estudo(con, limite=None, passo_slot=30, n_random=N_RANDOM, n_bootstrap=N_BOOTSTRAP,
           com_bandas=True, com_gatilho=True, verboso=True):
    """Roda o estudo completo e devolve as linhas (uma por faixa x alinhamento x filtro x horizonte)."""
    t0 = time.time()
    dias = _dias_estudo(con, "WIN$", limite)
    closes = _fechamentos_diarios(con, "WIN$", (limite or 260) + 30)
    rng = np.random.default_rng(11)
    eventos = []
    with _cache_barras() as cache:
        for k, dia in enumerate(dias):
            ohlc = _ohlc_minuto(con, "WIN$", dia)
            if ohlc is None:
                continue
            vies = _vies_proxy(closes, dia)
            evs = _eventos_dia(con, dia, ohlc, vies, cache, com_gatilho and _tem_fita(con, dia), passo_slot)
            fmax = {h: _fwd(ohlc["high"], h) for h in HORIZONTES}
            fmin = {h: _fwd_min(ohlc["low"], h) for h in HORIZONTES}
            for ev in evs:
                res = {}
                for h in HORIZONTES:
                    z = _medir(ohlc, ev["pos"], ev["direcao"], fmax, fmin, h, ev["entrada"])
                    b = _benchmark(ohlc, ev["pos"] - (ev["toque_min"] - ev["criada_min"]), ev["distancia"],
                                   ev["lado"], ev["direcao"], fmax, fmin, h, rng, n_random)
                    if z and b:
                        res[h] = dict(zona_edge=z[0] - z[1], zona_ret=z[2], rand_edge=b[0], rand_ret=b[1],
                                      rand_n=b[2])
                ev["m"] = res
                eventos.append(ev)
            if verboso and (k + 1) % 20 == 0:
                print(f"  {k + 1}/{len(dias)} dias | {len(eventos)} toques | {time.time() - t0:.0f}s",
                      flush=True)
    linhas = []
    for h in HORIZONTES:
        linhas += _tabela(eventos, h)
    bandas, motivo = (None, "desligado") if not com_bandas else _bandas(con)
    return dict(dias=len(dias), eventos=eventos, linhas=linhas, bandas=bandas, motivo_bandas=motivo,
                segundos=time.time() - t0)


def _tem_fita(con, dia):
    """True se o dia tem fita do Tryd (fonte do gatilho) publicada."""
    try:
        r = con.execute("SELECT 1 FROM tryd_fita WHERE dia=? LIMIT 1", (int(dia),)).fetchone()
        if r:
            return True
        return bool(con.execute("SELECT 1 FROM tryd_fita_1min WHERE dia=? LIMIT 1", (int(dia),)).fetchone())
    except sqlite3.OperationalError:
        return False


def _conclusao(linhas):
    """Frase honesta com o resultado das faixas de forca >= 60 no horizonte de 30 min."""
    foco = [l for l in linhas if l["horizonte"] == 30 and l["filtro"] == "sem_filtro"
            and l["faixa"] in ("60-79", "80+") and l["alinhamento"] in ("A_FAVOR_TENDENCIA", "CONTRA_TENDENCIA")]
    if not foco:
        return "Sem toques suficientes nas faixas de forca >= 60 para concluir qualquer coisa."
    uteis = [l for l in foco if l["p_edge"] is not None]
    if not uteis:
        return "Sem dias suficientes para o bootstrap nas faixas de forca >= 60: nada a concluir."
    n = sum(l["n_toques"] for l in uteis)
    dias = max(l["n_dias"] for l in uteis)
    sig_pos = [l for l in uteis if l["p_edge"] < 0.05 and l["diff_edge"] > 0]
    sig_neg = [l for l in uteis if l["p_edge"] < 0.05 and l["diff_edge"] < 0]
    if sig_pos and not sig_neg:
        melhor = max(sig_pos, key=lambda l: l["diff_edge"])
        return (f"Com {n} toques em ate {dias} dias por linha, {len(sig_pos)} das {len(uteis)} linhas de forca >= 60 "
                f"batem o nivel aleatorio de mesma distancia (melhor: {melhor['faixa']} {melhor['alinhamento']}, "
                f"{melhor['diff_edge']:+.1f} pts, p={melhor['p_edge']:.3f}).")
    if sig_neg and not sig_pos:
        pior = min(sig_neg, key=lambda l: l["diff_edge"])
        return (f"Com {n} toques em ate {dias} dias por linha, as {len(sig_neg)} linhas de forca >= 60 com "
                f"diferenca significativa ficaram PIOR que o nivel aleatorio de mesma distancia "
                f"(pior: {pior['faixa']} {pior['alinhamento']}, {pior['diff_edge']:+.1f} pts, p={pior['p_edge']:.3f}), "
                f"entao a confluencia nao mostrou reacao melhor que o acaso neste historico.")
    if sig_pos and sig_neg:
        return (f"Com {n} toques em ate {dias} dias por linha, {len(sig_pos)} linhas de forca >= 60 batem o "
                f"aleatorio e {len(sig_neg)} ficam pior (p<0,05): o sinal nao e consistente entre faixas e "
                f"alinhamentos, entao a confluencia nao provou valor proprio.")
    return (f"Com {n} toques em ate {dias} dias por linha, nenhuma das {len(uteis)} linhas de forca >= 60 difere do "
            f"nivel aleatorio de mesma distancia com p<0,05: as zonas nao mostraram reacao melhor que o acaso.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Estudo das zonas de confluencia do WIN$ (ESPEC 9.2)")
    ap.add_argument("--dias", type=int, default=0, help="usa so os ultimos N pregoes (0 = todos)")
    ap.add_argument("--slots", type=int, default=30, help="passo da reconstrucao em minutos")
    ap.add_argument("--random", type=int, default=N_RANDOM, help="niveis aleatorios por toque")
    ap.add_argument("--bootstrap", type=int, default=N_BOOTSTRAP, help="reamostragens do bootstrap")
    ap.add_argument("--sem-bandas", action="store_true", help="pula a validacao das bandas (2 min)")
    ap.add_argument("--sem-gatilho", action="store_true", help="pula o filtro de gatilho (caro)")
    ap.add_argument("--db", default="dados/book.db")
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        print(f"Estudo das regioes do WIN$ | slots de {args.slots} min | {args.random} aleatorios por toque | "
              f"bootstrap de {args.bootstrap} por dia")
        res = estudo(con, limite=args.dias or None, passo_slot=args.slots, n_random=args.random,
                     n_bootstrap=args.bootstrap, com_bandas=not args.sem_bandas,
                     com_gatilho=not args.sem_gatilho)
    finally:
        con.close()
    print(f"\n{res['dias']} pregoes com snapshots reconstruidos, {len(res['eventos'])} toques, "
          f"{res['segundos']:.0f}s")
    avaliados = [e for e in res["eventos"] if e.get("confirmado") is not None]
    if avaliados:
        conf = sum(1 for e in avaliados if e["confirmado"])
        print(f"filtro de gatilho: {conf} de {len(avaliados)} toques viraram GATILHO_CONFIRMADO em ate 5 min "
              f"({len({e['dia'] for e in avaliados})} pregoes com fita do Tryd)")
    else:
        print("filtro de gatilho: sem fita do Tryd no periodo, nenhum toque avaliado")
    for h in HORIZONTES:
        _imprimir([l for l in res["linhas"] if l["horizonte"] == h], h)
    _imprimir_bandas(res["bandas"], res["motivo_bandas"])
    print("\n=== conclusao")
    print(" ", _conclusao(res["linhas"]))
    ARQ_CSV.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(res["linhas"])
    if not df.empty:
        df["ic_edge_lo"] = [x[0] if x else None for x in df.ic_edge]
        df["ic_edge_hi"] = [x[1] if x else None for x in df.ic_edge]
        df["ic_ret_lo"] = [x[0] if x else None for x in df.ic_ret]
        df["ic_ret_hi"] = [x[1] if x else None for x in df.ic_ret]
        df = df.drop(columns=["ic_edge", "ic_ret"])
    df.to_csv(ARQ_CSV, index=False, encoding="utf-8")
    print(f"\nCSV: {ARQ_CSV}")
    return res


if __name__ == "__main__":
    main()
