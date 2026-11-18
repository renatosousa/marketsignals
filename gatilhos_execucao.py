"""Gatilho de microestrutura quando o preco testa uma zona operacional (ESPEC_V3.md 6.3 A).

O motor_regioes.py diz ONDE olhar; aqui se decide se o teste da zona tem defesa suficiente para virar
entrada. O semaforo da zona tem tres estados:

  DISTANTE   preco a mais de 1 ATR de 15 min da faixa da zona;
  PROXIMA    dentro de 1 ATR de 15 min, mas fora da faixa;
  EM_TESTE   preco dentro da faixa [preco_min, preco_max] — e onde a microestrutura e lida.

Em teste, nos ultimos 5 min:
  absorcao   volume agredido CONTRA a operacao (compra -> agressao vendedora) acima do percentil 80 dos
             blocos de 5 min do dia E deslocamento do preco menor que 0,3 x ATR de 15 min. A fonte e a
             tryd_fita do WIN/WDO (agressor) e, quando a fita tem o "Agente Agressor", o mesmo agente
             repetindo o mesmo lote >= 3 vezes no mesmo preco conta como iceberg (defesa reforcada). Sem
             fita, usa snapshots (vol_compra_agr/vol_venda_agr) do WIN/BOVA.
  OFI        order flow imbalance do topo do livro (grafico_quant.ofi_eventos sobre snapshots + niveis do
             WIN/BOVA) virando a favor da operacao nos ultimos minutos: o fluxo recente tem o sinal da
             operacao e supera (ou inverte) o do trecho anterior.
  Wyckoff    evento SPRING/UTAD/LPS/LPSY nas ultimas 4 barras de 15 min (wyckoff.analisar) com preco
             dentro da faixa da zona, com tolerancia de 0,25 x ATR de 15 min (um Spring perfura a faixa
             por construcao).

GATILHO_CONFIRMADO somente se absorcao E (OFI a favor OU evento Wyckoff). Caso contrario a zona fica em
"EM_TESTE (sem defesa ainda)": agressao rasgando o nivel, nao executar contra o fluxo imediato.

Os limiares estao nas constantes deste modulo e todas as metricas medidas voltam em `metricas` para
auditoria. Leitura apenas; nada e gravado. Heuristica ainda nao validada (a validacao de MFE/MAE e
expectativa com custo e o estudo_regioes.py, outra tarefa).
"""
import sqlite3

import numpy as np
import pandas as pd

import grafico_quant as gq
import wyckoff
from motor_regioes import ATIVOS, DIA_MS, atr_15m, normalizar, preco_atual

JANELA_ABSORCAO_MIN = 5          # janela do volume agredido contra a operacao
BLOCO_ABSORCAO_MIN = 5           # tamanho do bloco do percentil do dia
PERCENTIL_ABSORCAO = 80.0
DESLOCAMENTO_ATR15 = 0.3         # deslocamento maximo, em ATR de 15 min, para haver absorcao
ATR_PROXIMA = 1.0                # distancia maxima para PROXIMA, em ATR de 15 min
OFI_MIN = 5                      # minutos de OFI recente (e dos OFI_MIN anteriores, para a virada)
ICEBERG_REPETICOES = 3           # mesmo agente, mesmo preco, mesmo lote
ICEBERG_LOTE_REL = 3.0           # lote >= 3 x a mediana dos lotes repetidos do trecho
WYCKOFF_BARRAS = 4               # ultimas barras de 15 min
WYCKOFF_TOL_ATR15 = 0.25         # tolerancia da faixa da zona para o evento Wyckoff
WYCKOFF_EVENTOS = ("SPRING", "UTAD", "LPS", "LPSY")


def _minuto(ts_ms):
    return (int(ts_ms) % DIA_MS) // 60_000


def _finito(x):
    return isinstance(x, (int, float, np.floating)) and np.isfinite(float(x))


def _contrato_fita(con, dia, ativo):
    """Contrato da fita do dia (WINZ26/WDOZ26...) para o ativo; None sem fita.

    A fita bruta e a fonte ao vivo; o agregado tryd_fita_1min cobre o dia ja fechado pelo manutencao_banco.
    """
    pref = {"WIN$": "WIN", "WDO$": "WDO"}.get(normalizar(ativo))
    if not pref:
        return None
    for tabela in ("tryd_fita", "tryd_fita_1min"):
        try:
            r = con.execute(f"SELECT ativo FROM {tabela} WHERE dia=? AND ativo LIKE ? LIMIT 1",
                            (int(dia), pref + "%")).fetchone()
        except sqlite3.OperationalError:
            continue
        if r:
            return r[0]
    return None


def _fita_blocos(con, ativo, contrato, dia, agora_ms, agressor):
    """(volumes por bloco de 5 min do dia, volume dos ultimos 5 min) do agressor pedido.

    Prefere tryd_fita_1min (agregado diario do manutencao_banco.py, uma linha por minuto/grupo): a soma de
    vol_agr_* dos grupos e o volume agredido do minuto, e o filtro por minuto mantem a leitura causal.
    Sem o agregado do dia (o caso do pregao corrente), cai na fita bruta.
    """
    ativo = normalizar(ativo)
    dia, agora_ms = int(dia), int(agora_ms)
    min_agora = (agora_ms % DIA_MS) // 60_000
    col = "vol_agr_venda" if agressor == "Vendedor" else "vol_agr_compra"
    try:
        linhas = con.execute(f"SELECT (minuto / ?) * ? b, sum(coalesce({col}, 0)) v FROM tryd_fita_1min "
                             "WHERE dia=? AND ativo=? AND minuto <= ? GROUP BY b ORDER BY b",
                             (BLOCO_ABSORCAO_MIN, BLOCO_ABSORCAO_MIN, dia, contrato, min_agora)).fetchall()
        ultimo = con.execute(f"SELECT coalesce(sum(coalesce({col}, 0)), 0) FROM tryd_fita_1min "
                             "WHERE dia=? AND ativo=? AND minuto BETWEEN ? AND ?",
                             (dia, contrato, min_agora - JANELA_ABSORCAO_MIN + 1, min_agora)).fetchone()
    except sqlite3.OperationalError:
        linhas = []
        ultimo = None
    if linhas:
        return [float(v or 0) for _b, v in linhas], float(ultimo[0] or 0) if ultimo else 0.0
    ini = dia * DIA_MS + ATIVOS[ativo]["sessao"][0] * 60_000
    sql = ("SELECT (ts_ms / ?) * ? b, sum(qtd) v FROM tryd_fita WHERE dia=? AND ativo=? "
           "AND ts_ms BETWEEN ? AND ? AND agressor=? GROUP BY b ORDER BY b")
    passo = BLOCO_ABSORCAO_MIN * 60_000
    try:
        blocos = con.execute(sql, (passo, passo, dia, contrato, ini, agora_ms, agressor)).fetchall()
        ultimo = con.execute("SELECT coalesce(sum(qtd), 0) FROM tryd_fita WHERE dia=? AND ativo=? "
                            "AND ts_ms BETWEEN ? AND ? AND agressor=?",
                            (dia, contrato, agora_ms - JANELA_ABSORCAO_MIN * 60_000, agora_ms,
                             agressor)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not blocos:
        return None
    return [float(v or 0) for _b, v in blocos], float(ultimo[0] or 0) if ultimo else 0.0


def _snap_blocos(con, ativo, dia, agora_ms, contra):
    """Mesma conta pela agressao dos snapshots (sem fita): blocos de 5 min do dia e ultimos 5 min."""
    cfg = ATIVOS[normalizar(ativo)]
    if not cfg["livro"]:
        return None
    col = "vol_venda_agr" if contra == "venda" else "vol_compra_agr"
    ini = int(dia) * DIA_MS + cfg["sessao"][0] * 60_000
    passo = BLOCO_ABSORCAO_MIN * 60_000
    try:
        blocos = con.execute(f"SELECT (ts_ms / ?) * ? b, sum(coalesce({col}, 0)) v FROM snapshots "
                             "WHERE symbol=? AND ts_ms BETWEEN ? AND ? GROUP BY b ORDER BY b",
                             (passo, passo, cfg["symbol"], ini, int(agora_ms))).fetchall()
        ultimo = con.execute(f"SELECT coalesce(sum(coalesce({col}, 0)), 0) FROM snapshots WHERE symbol=? "
                             "AND ts_ms BETWEEN ? AND ?",
                             (cfg["symbol"], int(agora_ms) - JANELA_ABSORCAO_MIN * 60_000, int(agora_ms))).fetchone()
    except sqlite3.OperationalError:
        return None
    return [float(v or 0) for _b, v in blocos], float(ultimo[0] or 0) if ultimo else 0.0


def _iceberg(con, contrato, dia, agora_ms, agressor):
    """Mesmo agente repetindo o mesmo lote no mesmo preco >= ICEBERG_REPETICOES vezes nos ultimos 5 min.

    O lote tem de ser grande para o padrao do proprio trecho (>= ICEBERG_LOTE_REL x mediana dos lotes
    repetidos): sem isso, 1 contrato repetido centenas de vezes por uma corretora de varejo passaria como
    iceberg. A mediana e do trecho, nao do dia, e a regra continua heuristica.
    """
    try:
        linhas = con.execute("SELECT agente_agr, preco, qtd, count(*) n FROM tryd_fita WHERE dia=? AND ativo=? "
                             "AND ts_ms BETWEEN ? AND ? AND agressor=? AND agente_agr IS NOT NULL "
                             "AND agente_agr <> '' GROUP BY agente_agr, preco, qtd HAVING n >= ?",
                             (int(dia), contrato, int(agora_ms) - JANELA_ABSORCAO_MIN * 60_000, int(agora_ms),
                              agressor, ICEBERG_REPETICOES)).fetchall()
        todos = [float(q or 0) for (q,) in con.execute(
            "SELECT qtd FROM tryd_fita WHERE dia=? AND ativo=? AND ts_ms BETWEEN ? AND ?",
            (int(dia), contrato, int(agora_ms) - JANELA_ABSORCAO_MIN * 60_000, int(agora_ms)))]
    except sqlite3.OperationalError:
        return None
    if not linhas or not todos:
        return None
    mediana = float(np.median(todos))
    piso = max(2.0, ICEBERG_LOTE_REL * mediana)
    out = [dict(agente=a, preco=float(p), lote=float(q), repeticoes=int(n))
           for a, p, q, n in linhas if float(q or 0) >= piso]
    out.sort(key=lambda r: -r["lote"] * r["repeticoes"])
    return out[:3] or None


def _ofi(con, ativo, agora_ms):
    """OFI do topo do livro (Cont, Kukanov e Stoikov) nos ultimos 2 x OFI_MIN minutos; None sem livro."""
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    if not cfg["livro"]:
        return None
    symbol = cfg["symbol"]
    ini = int(agora_ms) - 2 * OFI_MIN * 60_000
    try:
        s = pd.read_sql("SELECT ts_ms, sessao, bid, ask FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                        "AND bid > 0 AND ask > bid ORDER BY ts_ms", con,
                        params=(symbol, max(ini, int(agora_ms) - 2 * OFI_MIN * 60_000), int(agora_ms)))
        if s.empty:
            return None
        ses = list(s.sessao.unique())
        n = pd.read_sql(f"SELECT ts_ms, lado, volume FROM niveis WHERE sessao IN ({','.join('?' * len(ses))}) "
                        "AND symbol=? AND ts_ms BETWEEN ? AND ? AND dist=1", con,
                        params=(*ses, symbol, int(s.ts_ms.iloc[0]), int(s.ts_ms.iloc[-1])))
    except sqlite3.OperationalError:
        return None
    if n.empty or "COMPRA" not in set(n.lado) or "VENDA" not in set(n.lado):
        return None
    q = n.pivot_table(index="ts_ms", columns="lado", values="volume", aggfunc="first")
    s = s.join(q.rename(columns={"COMPRA": "qb", "VENDA": "qa"})[["qb", "qa"]], on="ts_ms")
    if s[["qb", "qa"]].isna().any().any():
        return None
    e = gq.ofi_eventos(*(s[c].to_numpy(float) for c in ("bid", "ask", "qb", "qa")))
    e = np.where(np.isfinite(e), e, 0.0)
    corte = int(agora_ms) - OFI_MIN * 60_000
    atual = float(e[s.ts_ms.to_numpy() > corte].sum())
    anterior = float(e[s.ts_ms.to_numpy() <= corte].sum())
    return dict(ofi_atual=atual, ofi_anterior=anterior, ofi_janela_min=OFI_MIN)


def _m1(con, ativo, dia, agora_ms):
    """Barras de 1 min do dia ate agora (OHLC + volume agredido) para o wyckoff.analisar."""
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    ini = int(dia) * DIA_MS + cfg["sessao"][0] * 60_000
    try:
        if cfg["livro"]:
            s = pd.read_sql("SELECT ts_ms, last, coalesce(vol_compra_agr, 0) vc, coalesce(vol_venda_agr, 0) vv "
                            "FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND spread > 0 AND last > 0 "
                            "ORDER BY ts_ms", con, params=(cfg["symbol"], ini, int(agora_ms)))
        else:
            s = pd.read_sql("SELECT ts_ms + 59999 ts_ms, preco last, coalesce(vol_compra, 0) vc, "
                            "coalesce(vol_venda, 0) vv FROM agressao_1min WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                            "AND preco > 0 ORDER BY ts_ms", con, params=(cfg["symbol"], ini, int(agora_ms)))
    except sqlite3.OperationalError:
        return pd.DataFrame()
    if s.empty:
        return pd.DataFrame()
    s["e"], s["prof"] = 0.0, np.nan
    b = gq.barras(s, 1)
    b.index = pd.to_datetime(b.pop("t"), unit="ms")
    b.index.name = "hora"
    b = b.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return b[["open", "high", "low", "close", "volume"]]


def _wyckoff_zona(con, ativo, zona, agora_ms, atr15):
    """Eventos Wyckoff de 15 min nas ultimas WYCKOFF_BARRAS barras com preco na faixa da zona."""
    ativo = normalizar(ativo)
    m1 = _m1(con, ativo, agora_ms // DIA_MS, agora_ms)
    if len(m1) < 20:
        return [], None
    cfg, _preset = wyckoff.config_com_preset("WDO" if ativo == "WDO$" else "WIN")
    sessao = wyckoff.sessao_padrao(ATIVOS[ativo]["symbol"])
    try:
        res = wyckoff.analisar(m1, "15min", cfg, sessao)
    except Exception:
        return [], None
    ev = res.get("eventos")
    barras = res.get("barras")
    if ev is None or len(ev) == 0 or barras is None or len(barras) == 0:
        return [], cfg
    desde = barras.index[-1] - (WYCKOFF_BARRAS - 1) * pd.Timedelta(minutes=15)
    tol = WYCKOFF_TOL_ATR15 * atr15 if atr15 else 0.0
    dentro = []
    for r in ev.itertuples():
        if r.evento not in WYCKOFF_EVENTOS or pd.Timestamp(r.hora) < desde:
            continue
        if not _finito(r.preco):
            continue
        if zona["preco_min"] - tol <= float(r.preco) <= zona["preco_max"] + tol:
            dentro.append(dict(evento=r.evento, hora=str(r.hora), preco=float(r.preco),
                               obs=str(getattr(r, "obs", "") or "")))
    return dentro, cfg


def _detalhe(confirmado, absorcao, ofi_ok, wyckoff_ok, iceberg):
    if confirmado:
        fontes = [n for n, ok in (("OFI a favor", ofi_ok), ("Wyckoff", wyckoff_ok)) if ok]
        extra = " + iceberg" if iceberg else ""
        return f"GATILHO_CONFIRMADO: absorcao + {' + '.join(fontes)}{extra}"
    if not absorcao:
        return "EM_TESTE (sem defesa ainda)"
    return "EM_TESTE (absorcao sem OFI a favor nem evento Wyckoff)"


def estado_zona(con, ativo, zona, agora_ms, direcao):
    """Le a microestrutura do teste da zona -> {"status", "detalhe", "metricas"}.

    `zona` e um dict com preco_min/preco_centro/preco_max (o formato de motor_regioes.regioes) e
    `direcao` e a operacao candidata ("COMPRA" ou "VENDA"). Somente leitura.
    """
    ativo = normalizar(ativo)
    direcao = str(direcao or "").upper()
    if direcao not in ("COMPRA", "VENDA"):
        raise ValueError(f"direcao invalida: {direcao!r}")
    agora_ms = int(agora_ms)
    lo, hi = float(zona["preco_min"]), float(zona["preco_max"])
    preco = preco_atual(con, ativo, agora_ms)
    atr15 = atr_15m(con, ativo, agora_ms)
    metricas = dict(preco=preco, atr15=atr15, preco_min=lo, preco_max=hi, direcao=direcao)
    if preco is None or not atr15:
        return dict(status="DISTANTE", detalhe="sem preco ou sem ATR de 15 min", metricas=metricas)
    if lo <= preco <= hi:
        status = "EM_TESTE"
        metricas["distancia"] = 0.0
    else:
        dist = lo - preco if preco < lo else preco - hi
        status = "PROXIMA" if dist <= ATR_PROXIMA * atr15 else "DISTANTE"
        metricas["distancia"] = float(dist)
        return dict(status=status, detalhe=f"preco a {dist:.2f} pts da faixa", metricas=metricas)

    # --- em teste: absorcao, iceberg, OFI e Wyckoff
    dia = agora_ms // DIA_MS
    contra = "venda" if direcao == "COMPRA" else "compra"
    agressor = "Vendedor" if direcao == "COMPRA" else "Comprador"
    contrato = _contrato_fita(con, dia, ativo)
    fonte_absorcao, iceberg = None, None
    if contrato:
        blocos = _fita_blocos(con, ativo, contrato, dia, agora_ms, agressor)
        if blocos:
            fonte_absorcao = "tryd_fita"
            iceberg = _iceberg(con, contrato, dia, agora_ms, agressor)
    if fonte_absorcao is None:
        blocos = _snap_blocos(con, ativo, dia, agora_ms, contra)
        if blocos:
            fonte_absorcao = "snapshots"
    vol_contra = p80 = desloc = None
    if blocos:
        vols, vol_contra = blocos
        vols = [v for v in vols if v > 0]
        p80 = float(np.percentile(vols, PERCENTIL_ABSORCAO)) if vols else None
    antes = preco_atual(con, ativo, agora_ms - JANELA_ABSORCAO_MIN * 60_000)
    if antes is not None:
        desloc = abs(preco - antes)
    absorcao = bool(p80 is not None and vol_contra is not None and vol_contra >= p80
                    and desloc is not None and desloc < DESLOCAMENTO_ATR15 * atr15)
    metricas.update(fonte_absorcao=fonte_absorcao, vol_contra_5min=vol_contra, p80_blocos_5min=p80,
                    deslocamento_5min=desloc, absorcao=absorcao, iceberg=iceberg,
                    iceberg_ok=bool(iceberg))

    ofi = _ofi(con, ativo, agora_ms)
    ofi_ok, sinal = False, 1.0 if direcao == "COMPRA" else -1.0
    if ofi:
        ofi_ok = bool(ofi["ofi_atual"] * sinal > 0 and
                      (ofi["ofi_anterior"] * sinal <= 0 or abs(ofi["ofi_atual"]) > abs(ofi["ofi_anterior"])))
        metricas.update(ofi_atual=ofi["ofi_atual"], ofi_anterior=ofi["ofi_anterior"], ofi_a_favor=ofi_ok)
    else:
        metricas.update(ofi_atual=None, ofi_anterior=None, ofi_a_favor=False)

    eventos, _cfg = _wyckoff_zona(con, ativo, zona, agora_ms, atr15)
    wyckoff_ok = bool(eventos)
    metricas.update(wyckoff_eventos=eventos, wyckoff_ok=wyckoff_ok)

    confirmado = absorcao and (ofi_ok or wyckoff_ok)
    detalhe = _detalhe(confirmado, absorcao, ofi_ok, wyckoff_ok, bool(iceberg))
    return dict(status="GATILHO_CONFIRMADO" if confirmado else status, detalhe=detalhe, metricas=metricas)
