"""Wyckoff objetivo: ranges, eventos, fases A-E e perfil de volume a partir de barras_1m.

Tudo e causal: a linha da barra t so usa barras com fechamento <= t. Nada de datas escolhidas a mao, range
fixo ou pivos fixos; todas as regras e limiares ficam em ConfigWyckoff.

Pipeline
  1. barras_1m (book.db, somente leitura) -> filtro do pregao (WIN$/WDO$ 09:00-18:25; acoes/ETF 10:00-17:00)
     -> reamostragem no timeframe pedido (15min, 60min, D...). Os baldes intradiarios sao ancorados na
     abertura do pregao e nunca cruzam dias; a ultima barra so entra se estiver completa.
  2. Variaveis por barra (todas com janela que termina na propria barra): ATR, volume relativo (volume /
     mediana dos N volumes ANTERIORES), spread relativo (amplitude / ATR da barra anterior), posicao do
     fechamento na barra (0 = minima, 1 = maxima), OBV.
  3. Pivos (swing high/low) de ordem L: a barra i e pivo se for o extremo de [i-L, i+L]. So fica conhecido
     no fechamento de i+L -> ATRASO DE CONFIRMACAO = L barras. Eventos baseados em pivo (AR, LPS, LPSY,
     range por pivos) saem com ts = barra de confirmacao e ts_ref = barra do extremo.
  4. Maquina de estados (uma passada, barra a barra):
       sem range --(SC/BC por climax de volume+spread no extremo de uma tendencia)--> Fase A
       sem range --(2 topos e 2 fundos confirmados alinhados, largura em ATR dentro dos limites)--> Fase B
       A --(AR confirmado por pivo; define o range)--> A --(ST: reteste do climax com volume menor)--> B
       B --(Spring: perfura o fundo e fecha de volta dentro / UTAD: espelho)--> C
       B/C --(SOS: fecha acima do topo com volume e fechamento alto / SOW: espelho)--> D
       D --(LPS/LPSY: pivo de recuo que segura acima do meio do range)--> D
       D --(fecha alem do extremo da barra de SOS/SOW depois do LPS/LPSY)--> E (range encerrado)
     Acumulacao x distribuicao: hipotese inicial pelo climax (SC -> acumulacao, BC -> distribuicao) ou pela
     tendencia anterior (range por pivos); Spring/SOS confirmam acumulacao e UTAD/SOW distribuicao (a
     coluna tipo muda no fechamento da barra do evento, nunca para tras).
  5. Por segmento de fase: perfil de volume REAL a partir das barras de 1 min (POC, VAH/VAL 70%), razao de
     volume alta/baixa, divergencia de OBV nos topos/fundos confirmados e esforco x resultado.

Uso:
  python -m wyckoff --ativo WIN --tf 60min --inicio 2026-06-01 [--fim AAAA-MM-DD] [--db dados/book.db]
                    [--saida dados/wyckoff] [--pivo 3] [--sessao 09:00-18:25]
Saidas (CSV): <ativo>_<tf>_barras.csv, _eventos.csv, _fases.csv, _perfil.csv; resumo no terminal.
"""
import argparse
import sqlite3
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

AQUI = Path(__file__).parent
APELIDOS = {"WIN": "WIN$", "WDO": "WDO$", "IND": "IND$", "DOL": "DOL$"}  # continuos da corretora (ver backfill_barras)


@dataclass
class ConfigWyckoff:
    # variaveis por barra
    n_atr: int = 14                 # janela do ATR (media simples do true range)
    n_vol: int = 20                 # volume relativo = volume / mediana dos n_vol volumes anteriores
    pivo: int = 3                   # ordem L dos pivos; atraso de confirmacao = L barras
    # tendencia anterior ao climax
    n_tendencia: int = 20           # barras olhadas para tras
    tendencia_atr: float = 3.0      # queda/alta minima (em ATR) nesse intervalo para haver tendencia
    # climax (SC/BC)
    climax_vol: float = 2.0         # volume relativo minimo
    climax_spread: float = 1.5      # amplitude / ATR minima
    n_extremo: int = 20             # a minima (SC) / maxima (BC) tem de ser o extremo de n_extremo barras
    # automatic rally/reaction (AR) e largura do range
    ar_min_atr: float = 1.5         # rebote minimo desde o climax, em ATR
    ar_max_barras: int = 30         # se o AR nao confirmar em tantas barras, o climax e descartado
    largura_min_atr: float = 2.0    # largura do range (topo - fundo) / ATR
    largura_max_atr: float = 15.0
    # secondary test (ST)
    st_tol_atr: float = 0.75        # distancia maxima do extremo do climax, em ATR
    st_vol_max: float = 1.0         # volume do ST <= st_vol_max x volume do climax
    # range por pivos (sem climax)
    pivos_tol_atr: float = 1.0      # topos (e fundos) consecutivos dentro dessa distancia
    pivos_janela: int = 40          # os 4 pivos tem de caber nessa janela de barras
    # spring / UTAD
    spring_min_atr: float = 0.1     # perfuracao minima alem do range
    spring_max_atr: float = 1.5     # perfuracao maxima (alem disso e rompimento, nao teste)
    spring_barras: int = 2          # barras para fechar de volta dentro do range (0 = mesma barra)
    # sign of strength / weakness (SOS/SOW)
    sos_atr: float = 0.25           # fechamento alem do range, em ATR
    sos_vol: float = 1.3            # volume relativo minimo
    sos_pos: float = 0.6            # posicao do fechamento (SOS >= sos_pos; SOW <= 1 - sos_pos)
    # last point of support / supply (LPS/LPSY)
    lps_frac: float = 0.5           # o recuo tem de segurar alem de fundo + lps_frac x largura (espelho no LPSY)
    lps_vol_max: float = 1.0        # volume relativo do pivo do LPS <= volume relativo do SOS x lps_vol_max
    # encerramento
    max_barras_range: int = 400     # range sem desfecho e abandonado
    ruptura_atr: float = 2.0        # fechamento alem do range sem SOS/SOW (volume baixo) encerra como "rompido"
    max_barras_e: int = 50          # fase E dura no maximo isso (ou ate fechar de volta dentro do range)
    # perfil de volume
    perfil_bins: int = 40           # bins de preco por perfil (se passo_preco nao for dado)
    passo_preco: float = 0.0        # passo fixo (ex.: 5 pontos no WIN); 0 = automatico
    area_valor: float = 0.70


# ---------------------------------------------------------------- leitura e reamostragem

def simbolo(ativo):
    return APELIDOS.get(ativo.upper(), ativo.upper())


def sessao_padrao(sym):
    """Pregao regular. Futuros (WIN$, WDO$...) 09:00-18:25; acoes/ETF 10:00-17:00 (inclui o call de fechamento)."""
    if sym.endswith("$") or sym.startswith("DI1"):
        return "09:00", "18:25"
    return "10:00", "17:00"


def carregar_1m(db_path, sym, inicio=None, fim=None):
    """barras_1m de um simbolo, somente leitura. Indice = hora de ABERTURA da barra (Brasilia, sem fuso)."""
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        q, p = "SELECT ts_ms, open, high, low, close, volume FROM barras_1m WHERE symbol=?", [sym]
        if inicio:
            q += " AND ts_ms >= ?"
            p.append(int(pd.Timestamp(inicio).value // 10**6))
        if fim:
            q += " AND ts_ms < ?"
            p.append(int((pd.Timestamp(fim) + pd.Timedelta(days=1)).value // 10**6))
        df = pd.read_sql(q + " ORDER BY ts_ms", con, params=p)
    finally:
        con.close()
    df.index = pd.to_datetime(df.pop("ts_ms"), unit="ms")  # ts_ms = Brasilia codificada como epoch UTC
    df.index.name = "hora"
    return df


def filtrar_pregao(m1, sessao):
    ini, fim = sessao
    m1 = m1[m1.index.dayofweek < 5]
    m1 = m1.between_time(ini, fim)
    return m1[(m1[["open", "high", "low", "close"]] > 0).all(axis=1)].dropna()


def reamostrar(m1, tf, sessao):
    """Barras de tf ('15min', '60min', 'D') ancoradas na abertura do pregao, sem cruzar dias.

    Colunas extras: ts_fim (hora em que a barra fecha = quando o sinal fica disponivel), n_1m e completa.
    So a ULTIMA barra pode estar incompleta (dados terminam no meio dela); ela e marcada e o chamador a descarta.
    """
    if m1.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "ts_fim", "n_1m", "completa"])
    ini_h, ini_m = map(int, sessao[0].split(":"))
    fim_h, fim_m = map(int, sessao[1].split(":"))
    dia = m1.index.normalize()
    abre = dia + pd.Timedelta(hours=ini_h, minutes=ini_m)
    fecha = dia + pd.Timedelta(hours=fim_h, minutes=fim_m + 1)  # fim do ultimo minuto do pregao
    if str(tf).upper() in ("D", "1D", "DIA"):
        rot = abre
        ts_fim_esperado = fecha
    else:
        passo = pd.Timedelta(tf)
        k = ((m1.index - abre) // passo).astype(int)
        rot = abre + k * passo
        ts_fim_esperado = np.minimum(rot + passo, fecha)
    g = m1.assign(_rot=rot, _fim=ts_fim_esperado).groupby("_rot")
    b = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
              volume=("volume", "sum"), ts_fim=("_fim", "first"), n_1m=("close", "size"))
    b.index.name = "hora"
    b["completa"] = True
    ultimo_min = m1.index[-1] + pd.Timedelta(minutes=1)
    if ultimo_min < b.ts_fim.iloc[-1]:
        b.iloc[-1, b.columns.get_loc("completa")] = False
    return b


# ---------------------------------------------------------------- variaveis por barra (causais)

def variaveis(b, cfg):
    d = b.copy()
    pc = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(cfg.n_atr, min_periods=max(3, cfg.n_atr // 2)).mean()
    atr_ant = d.atr.shift(1)
    d["vol_rel"] = d.volume / d.volume.shift(1).rolling(cfg.n_vol, min_periods=max(5, cfg.n_vol // 2)).median()
    d["spread_rel"] = (d.high - d.low) / atr_ant
    amp = (d.high - d.low).replace(0, np.nan)
    d["pos_fech"] = ((d.close - d.low) / amp).fillna(0.5)
    sinal = np.sign(d.close.diff()).fillna(0)
    d["obv"] = (sinal * d.volume).cumsum()
    d["sobe"] = sinal > 0
    return d


# ---------------------------------------------------------------- deteccao

class _Range:
    def __init__(self, tipo, origem, inicio, lo=np.nan, hi=np.nan):
        self.tipo, self.origem, self.inicio = tipo, origem, inicio
        self.lo, self.hi = lo, hi
        self.fase = "A" if origem == "climax" else "B"
        self.climax_i = None     # barra do SC/BC
        self.climax_vol = np.nan
        self.ar_ok = origem != "climax"
        self.teste = None        # (i_perfuracao, lado) aguardando fechar de volta dentro
        self.sos_i = None
        self.lps_ok = False
        self.fim_i = None
        self.desfecho = ""
        self.e_desde = None


def detectar(b, cfg=None):
    """Recebe barras reamostradas (completas) e devolve (barras com fase/tipo/evento, eventos, segmentos).

    Uma unica passada para frente; a decisao da barra t le apenas os arrays ate t (pivos com L barras de atraso).
    """
    cfg = cfg or ConfigWyckoff()
    d = variaveis(b, cfg)
    n, L = len(d), cfg.pivo
    H, Lo, C = d.high.to_numpy(), d.low.to_numpy(), d.close.to_numpy()
    ATR, VR, SR, POS = d.atr.to_numpy(), d.vol_rel.to_numpy(), d.spread_rel.to_numpy(), d.pos_fech.to_numpy()
    idx = d.index
    fase = np.array(["-"] * n, dtype=object)
    tipo = np.array([""] * n, dtype=object)
    rid = np.full(n, -1)
    rlo, rhi = np.full(n, np.nan), np.full(n, np.nan)
    eventos, ranges = [], []
    piv_h, piv_l = [], []   # (i, preco) confirmados
    r = None

    def ev(t, nome, ref=None, preco=np.nan, obs=""):
        ref = t if ref is None else ref
        eventos.append(dict(hora=idx[t], hora_ref=idx[ref], ts_disponivel=d.ts_fim.iloc[t], evento=nome,
                            range_id=len(ranges), tipo=r.tipo if r else "", preco=preco,
                            vol_rel=VR[ref], spread_rel=SR[ref], pos_fech=POS[ref], atraso_barras=t - ref, obs=obs))

    def fechar(t, desfecho):
        nonlocal r
        r.fim_i, r.desfecho = t, desfecho
        ranges.append(r)
        r = None

    e_ativo = None  # (range_fechado, direcao, desde, lo, hi) para rotular a fase E depois do range
    for t in range(n):
        atr = ATR[t]
        # 1) pivo confirmado nesta barra (extremo em t-L)
        novo_h = novo_l = None
        if t >= 2 * L:
            i = t - L
            if H[i] == H[i - L:t + 1].max() and H[i] > H[i - L:i].max(initial=-np.inf):
                piv_h.append((i, H[i]))
                novo_h = i
            if Lo[i] == Lo[i - L:t + 1].min() and Lo[i] < Lo[i - L:i].min(initial=np.inf):
                piv_l.append((i, Lo[i]))
                novo_l = i
        if not np.isfinite(atr) or not np.isfinite(VR[t]):
            continue

        # 2) sem range: procura climax ou range por pivos
        if r is None:
            j = t - cfg.n_tendencia
            caiu = j >= 0 and C[j] - C[t] >= cfg.tendencia_atr * atr
            subiu = j >= 0 and C[t] - C[j] >= cfg.tendencia_atr * atr
            jan = slice(max(0, t - cfg.n_extremo + 1), t + 1)
            climax = VR[t] >= cfg.climax_vol and SR[t] >= cfg.climax_spread
            if climax and caiu and Lo[t] <= Lo[jan].min():
                e_ativo = None
                r = _Range("acumulacao", "climax", t, lo=Lo[t])
                r.climax_i, r.climax_vol = t, VR[t]
                ev(t, "SC", preco=Lo[t])
            elif climax and subiu and H[t] >= H[jan].max():
                e_ativo = None
                r = _Range("distribuicao", "climax", t, hi=H[t])
                r.climax_i, r.climax_vol = t, VR[t]
                ev(t, "BC", preco=H[t])
            elif (novo_h is not None or novo_l is not None) and len(piv_h) >= 2 and len(piv_l) >= 2:
                (ih1, h1), (ih2, h2) = piv_h[-2:]
                (il1, l1), (il2, l2) = piv_l[-2:]
                hi_, lo_ = max(h1, h2), min(l1, l2)
                larg = (hi_ - lo_) / atr
                ok = (min(ih1, il1) >= t - cfg.pivos_janela and abs(h1 - h2) <= cfg.pivos_tol_atr * atr
                      and abs(l1 - l2) <= cfg.pivos_tol_atr * atr and cfg.largura_min_atr <= larg <= cfg.largura_max_atr
                      and lo_ <= C[t] <= hi_ and (e_ativo is None or t - e_ativo[2] > L))
                if ok:
                    e_ativo = None
                    j0 = min(ih1, il1)
                    k = j0 - cfg.n_tendencia
                    tp = ""
                    if k >= 0:
                        tp = "acumulacao" if C[j0] < C[k] else "distribuicao"
                    r = _Range(tp or "indefinido", "pivos", t, lo=lo_, hi=hi_)
                    ev(t, "RANGE", ref=j0, preco=np.nan, obs=f"range por pivos {lo_:.2f}-{hi_:.2f}")
        else:
            larg = r.hi - r.lo if np.isfinite(r.hi) and np.isfinite(r.lo) else np.nan
            acum = r.tipo == "acumulacao"
            # 3) fase A: AR confirmado por pivo, depois ST
            if not r.ar_ok:
                if t - r.climax_i > cfg.ar_max_barras:
                    ev(t, "CANCELA", obs="AR nao confirmou")
                    fechar(t, "sem AR")
                elif acum and novo_l is not None and novo_l > r.climax_i and Lo[novo_l] < r.lo:
                    r.lo = Lo[novo_l]  # climax estendeu; o extremo do SC passa a ser o novo fundo
                elif not acum and novo_h is not None and novo_h > r.climax_i and H[novo_h] > r.hi:
                    r.hi = H[novo_h]
                elif acum and novo_h is not None and novo_h > r.climax_i and H[novo_h] - r.lo >= cfg.ar_min_atr * atr:
                    r.hi, r.ar_ok = H[novo_h], True
                    ev(t, "AR", ref=novo_h, preco=r.hi)
                elif not acum and novo_l is not None and novo_l > r.climax_i and r.hi - Lo[novo_l] >= cfg.ar_min_atr * atr:
                    r.lo, r.ar_ok = Lo[novo_l], True
                    ev(t, "AR", ref=novo_l, preco=r.lo)
                if r is not None and r.ar_ok:
                    w = (r.hi - r.lo) / atr
                    if not cfg.largura_min_atr <= w <= cfg.largura_max_atr:
                        ev(t, "CANCELA", obs=f"largura {w:.1f} ATR fora dos limites")
                        fechar(t, "largura")
            elif r.fase == "A":
                alvo = r.lo if acum else r.hi
                ext = Lo[t] if acum else H[t]
                if abs(ext - alvo) <= cfg.st_tol_atr * atr and VR[t] <= cfg.st_vol_max * r.climax_vol \
                        and r.lo - cfg.spring_min_atr * atr <= C[t] <= r.hi:
                    r.fase = "B"
                    ev(t, "ST", preco=ext)
            if r is not None and r.ar_ok and r.fase in ("A", "B", "C", "D"):
                larg = r.hi - r.lo
                # 4) teste de perfuracao (Spring / UTAD) em B
                if r.fase in ("A", "B") and r.teste is None:
                    if r.lo - cfg.spring_max_atr * atr <= Lo[t] <= r.lo - cfg.spring_min_atr * atr and r.tipo != "distribuicao":
                        r.teste = (t, "baixo")
                    elif r.hi + cfg.spring_min_atr * atr <= H[t] <= r.hi + cfg.spring_max_atr * atr and r.tipo != "acumulacao":
                        r.teste = (t, "alto")
                if r.teste is not None:
                    i0, lado = r.teste
                    if lado == "baixo" and C[t] > r.lo:
                        r.fase, r.tipo, r.teste = "C", "acumulacao", None
                        ev(t, "SPRING", ref=i0, preco=Lo[i0:t + 1].min())
                    elif lado == "alto" and C[t] < r.hi:
                        r.fase, r.tipo, r.teste = "C", "distribuicao", None
                        ev(t, "UTAD", ref=i0, preco=H[i0:t + 1].max())
                    elif t - i0 >= cfg.spring_barras:
                        r.teste = None
                # 5) SOS / SOW (rompimento com esforco)
                sos = C[t] >= r.hi + cfg.sos_atr * atr and VR[t] >= cfg.sos_vol and POS[t] >= cfg.sos_pos
                sow = C[t] <= r.lo - cfg.sos_atr * atr and VR[t] >= cfg.sos_vol and POS[t] <= 1 - cfg.sos_pos
                if r.fase in ("A", "B", "C") and (sos or sow):
                    r.fase, r.sos_i, r.lps_ok = "D", t, False
                    r.tipo = "acumulacao" if sos else "distribuicao"
                    r.teste = None
                    ev(t, "SOS" if sos else "SOW", preco=C[t])
                elif r.fase == "D":
                    acum = r.tipo == "acumulacao"
                    # 6) LPS / LPSY: pivo de recuo depois do SOS/SOW
                    if acum and novo_l is not None and novo_l > r.sos_i and Lo[novo_l] >= r.lo + cfg.lps_frac * larg \
                            and VR[novo_l] <= cfg.lps_vol_max * VR[r.sos_i]:
                        r.lps_ok = True
                        ev(t, "LPS", ref=novo_l, preco=Lo[novo_l])
                    elif not acum and novo_h is not None and novo_h > r.sos_i and H[novo_h] <= r.hi - cfg.lps_frac * larg \
                            and VR[novo_h] <= cfg.lps_vol_max * VR[r.sos_i]:
                        r.lps_ok = True
                        ev(t, "LPSY", ref=novo_h, preco=H[novo_h])
                    # recuo profundo invalida o rompimento
                    if acum and C[t] < r.lo + cfg.lps_frac * larg and C[t] < r.hi:
                        ev(t, "FALHA", obs="SOS devolvido")
                        r.fase, r.sos_i = "B", None
                    elif not acum and C[t] > r.hi - cfg.lps_frac * larg and C[t] > r.lo:
                        ev(t, "FALHA", obs="SOW devolvido")
                        r.fase, r.sos_i = "B", None
                    # 7) fase E: depois do LPS/LPSY, fecha alem do extremo da barra de SOS/SOW
                    elif r.lps_ok and ((acum and C[t] > H[r.sos_i]) or (not acum and C[t] < Lo[r.sos_i])):
                        ev(t, "E", preco=C[t], obs="tendencia fora do range")
                        rr = r
                        fechar(t, "E")
                        e_ativo = (rr, "alta" if acum else "baixa", t)
                # 8) rompimento sem esforco ou range velho
                if r is not None and r.fase in ("A", "B", "C") and r.ar_ok:
                    if C[t] > r.hi + cfg.ruptura_atr * atr or C[t] < r.lo - cfg.ruptura_atr * atr:
                        ev(t, "ROMPIDO", preco=C[t], obs="saiu do range sem SOS/SOW")
                        fechar(t, "rompido")
                if r is not None and t - r.inicio > cfg.max_barras_range:
                    ev(t, "CANCELA", obs="range sem desfecho")
                    fechar(t, "expirou")

        # rotulo da barra t (estado depois de processar t)
        if r is not None:
            fase[t], tipo[t], rid[t] = r.fase, r.tipo, len(ranges)
            rlo[t], rhi[t] = r.lo, r.hi
        elif e_ativo is not None:
            rr, dirc, desde = e_ativo
            dentro = rr.lo <= C[t] <= rr.hi
            if t == desde or (not dentro and t - desde <= cfg.max_barras_e):
                fase[t], tipo[t], rid[t] = "E", rr.tipo, ranges.index(rr)
                rlo[t], rhi[t] = rr.lo, rr.hi
            else:
                e_ativo = None
    if r is not None:
        r.desfecho = "aberto"
        ranges.append(r)

    d["fase"], d["tipo"], d["range_id"], d["range_lo"], d["range_hi"] = fase, tipo, rid, rlo, rhi
    ev_df = pd.DataFrame(eventos, columns=["hora", "hora_ref", "ts_disponivel", "evento", "range_id", "tipo",
                                           "preco", "vol_rel", "spread_rel", "pos_fech", "atraso_barras", "obs"])
    d["evento"] = ""
    for h, g in ev_df.groupby("hora"):
        d.loc[h, "evento"] = "+".join(g.evento)
    return d, ev_df


# ---------------------------------------------------------------- perfil de volume e metricas por fase

def perfil_volume(m1, cfg, lo=None, hi=None):
    """Volume por preco a partir das barras de 1 min: o volume de cada minuto e espalhado uniformemente entre a
    minima e a maxima DO MINUTO (erro de no maximo um minuto de amplitude, nao de uma barra de 60 min ou do dia).
    -> (DataFrame preco/volume, poc, vah, val)."""
    if m1.empty or m1.volume.sum() <= 0:
        return pd.DataFrame(columns=["preco", "volume"]), np.nan, np.nan, np.nan
    lo = m1.low.min() if lo is None else lo
    hi = m1.high.max() if hi is None else hi
    passo = cfg.passo_preco or max((hi - lo) / cfg.perfil_bins, 1e-9)
    base = np.floor(lo / passo) * passo
    nb = int(np.ceil((hi - base) / passo)) + 1
    bordas = base + passo * np.arange(nb + 1)
    vap = np.zeros(nb)
    l, h, v = m1.low.to_numpy(), m1.high.to_numpy(), m1.volume.to_numpy()
    for a, z, vol in zip(l, h, v):
        if vol <= 0:
            continue
        if z <= a:
            vap[min(int((a - base) // passo), nb - 1)] += vol
            continue
        ov = np.clip(np.minimum(bordas[1:], z) - np.maximum(bordas[:-1], a), 0, None)
        s = ov.sum()
        if s > 0:
            vap += vol * ov / s
    centro = bordas[:-1] + passo / 2
    k = int(vap.argmax())
    e = dd = k
    acc, tot = vap[k], vap.sum()
    while acc < cfg.area_valor * tot and (e > 0 or dd < nb - 1):
        ve = vap[e - 1] if e > 0 else -1
        vd = vap[dd + 1] if dd < nb - 1 else -1
        if ve >= vd:
            e -= 1
            acc += vap[e]
        else:
            dd += 1
            acc += vap[dd]
    return pd.DataFrame({"preco": centro, "volume": vap}), centro[k], bordas[dd + 1], bordas[e]


def _div_obv(s, piv, col, maior):
    """Compara os dois ultimos pivos confirmados do segmento: preco faz extremo novo e o OBV nao confirma."""
    ps = [i for i in piv if i in s.index]
    if len(ps) < 2:
        return ""
    a, z = s.loc[ps[-2]], s.loc[ps[-1]]
    if maior and z[col] > a[col] and z.obv < a.obv:
        return "baixista"
    if not maior and z[col] < a[col] and z.obv > a.obv:
        return "altista"
    return "nao"


def segmentos(d, ev, m1, cfg):
    """Um segmento por sequencia continua de (range_id, fase). Metricas usam so barras do proprio segmento."""
    rot = d[d.range_id >= 0]
    if rot.empty:
        return pd.DataFrame(), pd.DataFrame()
    chave = (rot.range_id.astype(str) + rot.fase)
    grupo = (chave != chave.shift()).cumsum()
    L = cfg.pivo
    # pivos (por indice de barra), para a divergencia de OBV; confirmados dentro do segmento
    hi_piv, lo_piv = [], []
    H, Lo = d.high.to_numpy(), d.low.to_numpy()
    for i in range(L, len(d) - L):
        if H[i] == H[i - L:i + L + 1].max():
            hi_piv.append((d.index[i], d.index[i + L]))
        if Lo[i] == Lo[i - L:i + L + 1].min():
            lo_piv.append((d.index[i], d.index[i + L]))
    obv_antes = d.obv.shift(1).fillna(0)
    linhas, perfis = [], []
    for _, s in rot.groupby(grupo):
        ini, fim = s.index[0], s.index[-1]
        fim_disp = s.ts_fim.iloc[-1]
        mm = m1[(m1.index >= ini) & (m1.index < fim_disp)]
        prof, poc, vah, val = perfil_volume(mm, cfg)
        vu, vd = s.volume[s.sobe].sum(), s.volume[~s.sobe].sum()
        desloc = abs(s.close.iloc[-1] - s.open.iloc[0]) / s.atr.mean() if s.atr.mean() > 0 else np.nan
        hp = [p for p, conf in hi_piv if ini <= p and conf <= fim]
        lp = [p for p, conf in lo_piv if ini <= p and conf <= fim]
        larg = (s.range_hi.iloc[-1] - s.range_lo.iloc[-1])
        linhas.append(dict(
            range_id=int(s.range_id.iloc[0]), fase=s.fase.iloc[0], tipo=s.tipo.iloc[-1], inicio=ini, fim=fim,
            barras=len(s), range_lo=s.range_lo.iloc[-1], range_hi=s.range_hi.iloc[-1],
            largura_atr=larg / s.atr.iloc[-1] if s.atr.iloc[-1] > 0 else np.nan,
            var_pct=100 * (s.close.iloc[-1] / s.open.iloc[0] - 1), volume=s.volume.sum(),
            vol_rel_medio=s.vol_rel.mean(), ud_vol=vu / vd if vd > 0 else np.nan,
            obv_delta=s.obv.iloc[-1] - obv_antes.loc[ini],
            div_obv_topos=_div_obv(s, hp, "high", True), div_obv_fundos=_div_obv(s, lp, "low", False),
            # esforco x resultado: volume relativo acumulado por ATR de deslocamento liquido (alto = absorcao)
            esforco_resultado=s.vol_rel.sum() / desloc if desloc and desloc > 0 else np.nan,
            barras_absorcao=int(((s.vol_rel >= 1.5) & (s.spread_rel <= 0.8)).sum()),
            poc=poc, vah=vah, val=val,
            eventos="+".join(ev[(ev.hora >= ini) & (ev.hora <= fim)].evento)))
        if len(prof):
            perfis.append(prof.assign(range_id=int(s.range_id.iloc[0]), fase=s.fase.iloc[0], inicio=ini))
    return pd.DataFrame(linhas), (pd.concat(perfis, ignore_index=True) if perfis else pd.DataFrame())


# ---------------------------------------------------------------- orquestracao

def analisar(m1, tf="60min", cfg=None, sessao=("09:00", "18:25")):
    """m1 = barras de 1 min (indice = abertura da barra). -> dict com barras, eventos, fases, perfil."""
    cfg = cfg or ConfigWyckoff()
    m1 = filtrar_pregao(m1, sessao)
    b = reamostrar(m1, tf, sessao)
    if b.empty or not b.completa.any():
        vazio = pd.DataFrame()
        return dict(barras=vazio, eventos=vazio, fases=vazio, perfil=vazio)
    b = b[b.completa]  # barra em formacao nao entra (sem lookahead e sem semana/dia parcial)
    m1 = m1[m1.index < b.ts_fim.max()] if len(b) else m1.iloc[:0]
    d, ev = detectar(b, cfg)
    fases, perfil = segmentos(d, ev, m1, cfg)
    return dict(barras=d, eventos=ev, fases=fases, perfil=perfil)


def resumo_texto(res, ativo, tf):
    d, ev, f = res["barras"], res["eventos"], res["fases"]
    out = [f"{ativo} {tf}: {len(d)} barras de {d.index.min()} a {d.index.max()}" if len(d) else f"{ativo}: sem barras"]
    if len(d):
        u = d.iloc[-1]
        out.append(f"estado na ultima barra fechada: fase {u.fase} {u.tipo or ''} "
                   f"range {u.range_lo:.2f}-{u.range_hi:.2f}" if u.range_id >= 0 else "estado: sem range ativo")
    if len(ev):
        out.append(f"\neventos ({len(ev)}), ultimos 15:")
        out.append(ev.tail(15)[["hora", "hora_ref", "evento", "tipo", "preco", "vol_rel", "obs"]]
                   .to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    if len(f):
        out.append("\nfases (ultimas 12):")
        cols = ["range_id", "fase", "tipo", "inicio", "barras", "var_pct", "ud_vol", "div_obv_topos",
                "div_obv_fundos", "esforco_resultado", "poc", "val", "vah"]
        out.append(f.tail(12)[cols].to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    return "\n".join(out)


def painel(db_path, ativo="WIN", tf="60min", dias=120, cfg=None):
    """Resumo para o dashboard (GET /api/wyckoff): estado atual, ultimos eventos e fases. Somente leitura."""
    sym = simbolo(ativo)
    fim = pd.Timestamp.now().normalize()
    m1 = carregar_1m(db_path, sym, (fim - pd.Timedelta(days=dias)).date().isoformat())
    res = analisar(m1, tf, cfg, sessao_padrao(sym))
    d, ev, f = res["barras"], res["eventos"], res["fases"]
    if d.empty:
        return dict(ativo=sym, tf=tf, dias=dias, estado={}, eventos=[], fases=[])
    fmt = lambda t: None if pd.isna(t) else pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")
    est = {}
    if len(d):
        u = d.iloc[-1]
        est = dict(hora=fmt(d.index[-1]), close=u.close, fase=u.fase, tipo=u.tipo, range_lo=u.range_lo,
                   range_hi=u.range_hi, vol_rel=u.vol_rel)
    evs = [dict(hora=fmt(r.hora), hora_ref=fmt(r.hora_ref), evento=r.evento, tipo=r.tipo, preco=r.preco,
                vol_rel=r.vol_rel, obs=r.obs) for r in ev.tail(12).itertuples()]
    fs = [dict(range_id=r.range_id, fase=r.fase, tipo=r.tipo, inicio=fmt(r.inicio), barras=r.barras, var_pct=r.var_pct,
               ud_vol=r.ud_vol, div_topos=r.div_obv_topos, div_fundos=r.div_obv_fundos,
               esforco=r.esforco_resultado, poc=r.poc, val=r.val, vah=r.vah) for r in f.tail(8).itertuples()]
    return dict(ativo=sym, tf=tf, dias=dias, estado=est, eventos=evs[::-1], fases=fs[::-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wyckoff objetivo a partir de barras_1m")
    ap.add_argument("--ativo", default="WIN", help="WIN, WDO, BOVA11, ... (WIN -> WIN$, serie continua)")
    ap.add_argument("--tf", default="60min", help="15min, 60min, D ...")
    ap.add_argument("--inicio", help="AAAA-MM-DD (inclua aquecimento: ~n_vol barras antes do periodo de interesse)")
    ap.add_argument("--fim", help="AAAA-MM-DD (inclusive)")
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--saida", default=str(AQUI / "dados" / "wyckoff"))
    ap.add_argument("--sessao", help="HH:MM-HH:MM (padrao: 09:00-18:25 futuros, 10:00-17:00 acoes)")
    for f in fields(ConfigWyckoff):  # todo parametro da config vira --nome
        ap.add_argument("--" + f.name.replace("_", "-"), type=type(f.default), default=f.default)
    a = ap.parse_args(argv)
    cfg = ConfigWyckoff(**{f.name: getattr(a, f.name) for f in fields(ConfigWyckoff)})
    sym = simbolo(a.ativo)
    sessao = tuple(a.sessao.split("-")) if a.sessao else sessao_padrao(sym)
    m1 = carregar_1m(a.db, sym, a.inicio, a.fim)
    if m1.empty:
        raise SystemExit(f"sem barras_1m para {sym} no periodo")
    res = analisar(m1, a.tf, cfg, sessao)
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    base = f"{sym.replace('$', '')}_{a.tf}"
    for k in ("barras", "eventos", "fases", "perfil"):
        res[k].to_csv(saida / f"{base}_{k}.csv", index=(k == "barras"))
    print(resumo_texto(res, sym, a.tf))
    print(f"\nconfig: {asdict(cfg)}")
    print(f"CSVs em {saida / (base + '_*.csv')}")


if __name__ == "__main__":
    main()
