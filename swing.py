"""Playbook 3 de swing trade (ESPEC_V3.md 6.3.C): areas de valor, VWAPs ancoradas e invalidacao da tese.

O daytrade do motor_regioes.py olha 1 a 3 horas; aqui o mapa e de 3 a 15 pregoes. Para WIN$, WDO$,
BOVA11 e DI1F29 o modulo monta:

  area de valor de 20 e de 60 pregoes   POC, VAH e VAL das barras de 1 min com volume (grafico_quant.perfil
                                        sobre os dias do calendario B3); o DI1F29 usa as barras de 1 min
                                        do proprio contrato;
  VWAP ancorada                         na ultima rolagem do contrato (vencimento calculado: WIN nos meses
                                        pares pela quarta mais proxima do dia 15, WDO no primeiro pregao do
                                        mes), no ultimo Copom conhecido (calendario_eventos ou
                                        calendario_manual.csv; sem ele, a ultima mudanca da Selic no
                                        bcb_selic) e no inicio do mes;
  movimento esperado semanal            +- IV ATM x preco x raiz(5/252) e os quantis empiricos de 5
                                        pregoes do proprio ativo;
  estrutura de 60 min                   maxima e minima das ultimas 2 barras fechadas de 60 min.

`zonas_swing` agrupa esses niveis com o mesmo algoritmo do motor_regioes (guloso por densidade, largura
de 2 delta), com delta = 0,5 x ATR diario (horizonte de dias, nao de horas), classifica pelo vies com as
mesmas regras do daytrade e devolve zonas no formato do motor_regioes com horizonte 'SWING'.

`invalidacao` devolve frases objetivas com numeros para a tese vigente: DI1F29, fluxo do estrangeiro de
5 pregoes (fluxo_diario, so o que ja foi publicado) e a VWAP de 20 pregoes do BOVA11.

DI1F29: o preco do contrato e a taxa (% a.a.), entao COMPRA/VENDA nas zonas sao do contrato futuro --
VENDA significa apostar na queda da taxa (compra do DI futuro), nao venda do ativo.

Causalidade: so le ts_ms <= agora_ms; o fluxo oficial entra pela data de publicacao (D+2) e a posicao em
aberto das opcoes e D-1. Somente leitura.
"""
import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import calendario_b3 as cal
import calendario_macro as cmac
import grafico_quant as gq
import motor_regioes as mr
import opcoes_estruturas as oe

DIA_MS = 86_400_000
ATIVOS = ("WIN$", "WDO$", "BOVA11", "DI1F29")
DIAS_VA = (20, 60)
DELTA_ATR_SWING = 0.5
FORCA_MIN = 35.0
MAX_POR_LADO = 3
MULT_STOP_ATR = 0.5
DIAS_QUANTIS = 5
MAX_QUANTIS = 120
PESOS = {"va20": 25.0, "va60": 25.0, "vwap_anc": 20.0, "semana": 15.0, "quantis": 15.0,
         "quantis_extremos": 25.0, "estrutura60": 15.0}
Z5 = {"q05": 1.6449, "q15": 1.0364, "q85": 1.0364, "q95": 1.6449}
CSV_MANUAL = Path(__file__).resolve().parent / "dados" / "calendario_manual.csv"


def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _dia_idx(ts_ms):
    return int(ts_ms) // DIA_MS


def _finito(x):
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


def _br(x, casas=2):
    """Numero no formato da tela (milhar com ponto, decimal com virgula); 'n/d' sem valor."""
    if not _finito(x):
        return "n/d"
    s = f"{float(x):,.{casas}f}"
    return s.replace(",", "#").replace(".", ",").replace("#", ".")


# ------------------------------------------------------------------ dados por ativo

def _dados_dia(con, ativo, dia, fim_ms=None, cache=None):
    """Barras de 1 min do dia (ts_ms, last, vc, vv); o DI1F29 e lido direto de barras_1m."""
    if ativo in mr.ATIVOS:
        return mr._dados_dia(con, ativo, dia, fim_ms, cache)
    chave = (ativo, int(dia))
    if fim_ms is None and cache is not None and chave in cache:
        return cache[chave]
    ini, fim = int(dia) * DIA_MS + 540 * 60_000, int(dia) * DIA_MS + (18 * 60 + 30) * 60_000
    if fim_ms is not None:
        fim = min(fim, int(fim_ms))
    df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
    if ini < fim:
        try:
            df = pd.read_sql("SELECT ts_ms, close last, coalesce(volume, 0) vc, 0.0 vv FROM barras_1m "
                             "WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND close > 0 ORDER BY ts_ms",
                             con, params=(ativo, ini, fim))
        except sqlite3.OperationalError:
            df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
    if df.empty:
        df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
    else:
        df = df.astype({"ts_ms": "int64", "last": float, "vc": float, "vv": float})
    if fim_ms is None and cache is not None:
        cache[chave] = df
    return df


def _dias(con, ativo, dia_idx, n):
    """Ate n pregoes com dados ANTES de dia_idx, mais recente primeiro (calendario B3)."""
    if ativo in mr.ATIVOS:
        return mr.dias_com_dados(con, ativo, dia_idx, n)
    out, d = [], int(dia_idx)
    for _ in range(int(n) * 3 + 10):
        d = mr._dia_anterior(d)
        try:
            tem = con.execute("SELECT 1 FROM barras_1m WHERE symbol=? AND ts_ms BETWEEN ? AND ? LIMIT 1",
                              (ativo, d * DIA_MS, (d + 1) * DIA_MS)).fetchone()
        except sqlite3.OperationalError:
            tem = None
        if tem:
            out.append(d)
            if len(out) >= n:
                break
    return out


def _extremos(con, ativo, dia, cache=None):
    """(maxima, minima, ultimo) do dia; para o DI sai das barras de 1 min."""
    if ativo in mr.ATIVOS:
        return mr._extremos_dia(con, ativo, dia, cache)
    df = _dados_dia(con, ativo, dia, cache=cache)
    if df.empty:
        return None
    return float(df.last.max()), float(df.last.min()), float(df.last.iloc[-1])


def _preco_info(con, ativo, agora_ms):
    """(preco, data) do ultimo dado disponivel ate agora; cai no fechamento de D-1 sem barras do dia."""
    dia = _dia_idx(agora_ms)
    if ativo in mr.ATIVOS:
        p = mr.preco_atual(con, ativo, agora_ms)
        if p:
            return float(p), _iso(dia)
    df = _dados_dia(con, ativo, dia, int(agora_ms))
    if not df.empty:
        return float(df.last.iloc[-1]), _iso(dia)
    for d in _dias(con, ativo, dia, 2):
        e = _extremos(con, ativo, d)
        if e and _finito(e[2]):
            return float(e[2]), _iso(d)
    return None, None


def _preco(con, ativo, agora_ms):
    return _preco_info(con, ativo, agora_ms)[0]


def _atr_diario(con, ativo, dia_idx, n=14, cache=None):
    """ATR de n pregoes fechados antes do dia; o DI1F29 usa maxima/minima das barras de 1 min."""
    if ativo in mr.ATIVOS:
        return mr.atr_diario(con, ativo, dia_idx, n, cache)
    ext = []
    for d in reversed(_dias(con, ativo, dia_idx, n)):
        e = _extremos(con, ativo, d, cache)
        if e:
            ext.append(e)
    if len(ext) < 2:
        return None
    trs, anterior = [], None
    for alta, baixa, ultimo in ext:
        tr = alta - baixa
        if anterior is not None:
            tr = max(tr, abs(alta - anterior), abs(baixa - anterior))
        trs.append(tr)
        anterior = ultimo
    return float(np.mean(trs)) if trs else None


def _closes(con, ativo, dia_idx, n, cache=None):
    """Fechamentos dos n pregoes antes do dia (do mais antigo ao mais novo)."""
    out = []
    for d in reversed(_dias(con, ativo, dia_idx, n)):
        e = _extremos(con, ativo, d, cache)
        if e and _finito(e[2]):
            out.append(float(e[2]))
    return out


def _perfil_va(df, passo):
    """POC/VAH/VAL das barras (mesma conta do grafico_quant.perfil) com bins inteiros.

    O grafico_quant.perfil monta a grade com np.arange(min, max, passo); com passo fracionario (0,01 do
    BOVA11, 0,005 do DI1F29) o arange nao casa bit a bit com as chaves do groupby e quase todo o volume
    se perde (POC cai no minimo e a area de valor fica de um bin). Aqui o indice do bin e inteiro e o
    preco sai de indice x passo, o que fecha a conta para qualquer passo.
    """
    if df is None or df.empty or (df.vc + df.vv).sum() <= 0:
        return None
    idx = np.rint(df.last.to_numpy(float) / passo).astype(np.int64)
    g = pd.DataFrame({"i": idx, "vc": df.vc.to_numpy(float), "vv": df.vv.to_numpy(float)})
    g = g.groupby("i").sum()
    grade = np.arange(int(g.index.min()), int(g.index.max()) + 1)
    g = g.reindex(grade, fill_value=0.0)
    vol = (g.vc + g.vv).to_numpy(float)
    k = int(vol.argmax())
    e = dd = k
    acc, tot = vol[k], vol.sum()
    while acc < gq.AREA_VALOR * tot and (e > 0 or dd < len(vol) - 1):
        ve = vol[e - 1] if e > 0 else -1.0
        vd = vol[dd + 1] if dd < len(vol) - 1 else -1.0
        if ve >= vd:
            e -= 1
            acc += vol[e]
        else:
            dd += 1
            acc += vol[dd]
    preco = grade.astype(float) * passo
    return dict(passo=passo, preco=preco.tolist(), vc=g.vc.tolist(), vv=g.vv.tolist(),
                poc=float(preco[k]), vah=float(preco[dd] + passo / 2.0), val=float(preco[e] - passo / 2.0))


def _perfil(con, ativo, dias, passo, cache):
    """Perfil de volume (POC/VAH/VAL) das barras de 1 min dos dias pedidos."""
    partes = [_dados_dia(con, ativo, d, cache=cache) for d in dias]
    partes = [p for p in partes if not p.empty]
    if not partes:
        return None
    df = pd.concat(partes, ignore_index=True)
    if (df.vc + df.vv).sum() <= 0:
        return None
    return _perfil_va(df, passo)


def _vwap(con, ativo, de_idx, agora_ms, cache):
    """VWAP ponderada por volume de de_idx ate agora (inclusive), com o dia corrente cortado em agora."""
    dia = _dia_idx(agora_ms)
    if de_idx is None or de_idx > dia:
        return None, 0, 0.0
    soma_pv = soma_v = 0.0
    simples, n_dias = [], 0
    for d in cal.pregoes(_iso(de_idx), _iso(dia)):
        idx = (d - date(1970, 1, 1)).days
        df = _dados_dia(con, ativo, idx, agora_ms if idx == dia else None, cache)
        if df.empty:
            continue
        n_dias += 1
        v = (df.vc + df.vv).to_numpy(float)
        tot = float(v.sum())
        if tot > 0:
            soma_pv += float((df.last.to_numpy(float) * v).sum())
            soma_v += tot
        else:
            simples.append(float(df.last.mean()))
    if soma_v > 0:
        return soma_pv / soma_v, n_dias, soma_v
    if simples:
        return float(np.mean(simples)), n_dias, 0.0
    return None, n_dias, 0.0


# ------------------------------------------------------------------ ancoras

def _rolagem(ativo, dia_iso):
    """Data da ultima rolagem do contrato: (iso, descricao) ou (None, motivo).

    WIN$ vence nos meses pares (quarta mais proxima do dia 15, antecipada para o pregao anterior);
    WDO$ no primeiro pregao do mes. BOVA11 e DI1F29 nao rolam (o DI1F29 tem vencimento fixo).
    """
    d = date.fromisoformat(dia_iso)
    if ativo == "BOVA11":
        return None, "BOVA11 e ETF a vista: nao ha rolagem de contrato"
    if ativo == "DI1F29":
        return None, "DI1F29 tem vencimento fixo (jan/2029): nao ha rolagem"
    melhor = None
    ano, mes = d.year, d.month
    for _ in range(30):
        if ativo == "WIN$" and mes % 2 == 0:
            exp = cmac._pregiao_ou_antes(cmac.quarta_mais_proxima_do_15(ano, mes))
        elif ativo == "WDO$":
            exp = cmac.primeiro_pregao(ano, mes)
        else:
            exp = None
        if exp and exp <= d and (melhor is None or exp > melhor):
            melhor = exp
        mes -= 1
        if mes == 0:
            mes, ano = 12, ano - 1
    if melhor is None:
        return None, "sem vencimento anterior no calendario"
    return melhor.isoformat(), ("vencimento WIN" if ativo == "WIN$" else "vencimento WDO")


def _ultimo_copom(con, dia_iso):
    """(iso, fonte) do ultimo Copom conhecido ate dia_iso; sem ele, a ultima mudanca da Selic."""
    dia = date.fromisoformat(dia_iso)
    try:
        r = con.execute("SELECT max(ts_ms) FROM calendario_eventos WHERE evento LIKE '%opom%' AND ts_ms < ?",
                        ((dia - date(1970, 1, 1)).days * DIA_MS,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and r[0]:
        return _iso(int(r[0]) // DIA_MS), "calendario_eventos"
    try:
        manuais = [e for e in cmac.ler_manual(CSV_MANUAL) if "opom" in e["evento"].lower()]
        manuais = [(e["ts_ms"] // DIA_MS, e["evento"]) for e in manuais if _iso(e["ts_ms"] // DIA_MS) <= dia_iso]
        if manuais:
            return _iso(max(manuais)[0]), "calendario_manual"
    except Exception:
        pass
    try:
        rows = con.execute("SELECT data, meta FROM bcb_selic WHERE data <= ? ORDER BY data", (dia_iso,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    anterior, ultima = None, None
    for d, m in rows:
        if anterior is not None and m != anterior:
            ultima = d
        anterior = m
    if ultima:
        return str(ultima)[:10], "mudanca da Selic no bcb_selic (aprox. da reuniao)"
    return None, "sem Copom no calendario nem mudanca de Selic no banco"


def _inicio_mes(dia_iso):
    d = date.fromisoformat(dia_iso)
    return d.replace(day=1).isoformat()


# ------------------------------------------------------------------ movimento e IV

def _iv_atm(con, agora_ms, ativo):
    """IV ATM do vencimento mais proximo com 5+ dias uteis: BOVA11 para WIN$/BOVA11, DOL para WDO$."""
    agora_ms = int(agora_ms)
    dia_iso = _iso(_dia_idx(agora_ms))
    if ativo == "DI1F29":
        return None, "DI1F29 e taxa: a IV ATM das opcoes de indice nao se aplica"
    try:
        if ativo == "WDO$":
            pos = gq.posicoes_dolar(con, dia_iso)
            if not pos:
                return None, "sem b3_dolar_series"
            s = pos["series"].copy()
            s["du"] = [cal.du_ate_vencimento(dia_iso, v) for v in s.venc]
            s = s[s.du >= oe.DU_MIN]
            if s.empty:
                return None, "sem vencimento de dolar com 5+ dias uteis"
            v = s.sort_values("du").venc.iloc[0]
            s = s[s.venc == v]
            fator = float(s.fator.dropna().iloc[0]) if s.fator.notna().any() else 1.0
            spot = _preco(con, "WDO$", agora_ms)
            if not spot:
                return None, "sem preco do WDO$"
            g = s.groupby("strike").iv.mean().reset_index()
            return oe._interp_iv(g, float(spot) * fator), f"DOL {v} (D-1)"
        resumos = oe._resumos(con, agora_ms)
        validos = oe._vencimentos_validos(resumos, dia_iso)
        if not validos:
            return None, "opcoes_resumo do BOVA11 sem vencimento com 5+ dias uteis"
        return validos[0]["atm_iv"], f"BOVA11 {validos[0]['vencimento']}"
    except (sqlite3.OperationalError, ValueError):
        return None, "sem superficie de opcoes"


def _quantis_5d(con, ativo, agora_ms, preco, cache):
    """Quantis empiricos dos retornos de 5 pregoes (janelas sobrepostas) -> niveis de preco."""
    dia = _dia_idx(agora_ms)
    closes = _closes(con, ativo, dia, MAX_QUANTIS, cache)
    if len(closes) < 25 or not preco:
        return None, 0
    p = np.array(closes, dtype=float)
    r = p[DIAS_QUANTIS:] / p[:-DIAS_QUANTIS] - 1.0
    r = r[np.isfinite(r)]
    if len(r) < 15:
        return None, len(r)
    q = {k: float(preco * (1.0 + np.quantile(r, v))) for k, v in
         (("q05", 0.05), ("q15", 0.15), ("q50", 0.50), ("q85", 0.85), ("q95", 0.95))}
    q["n"] = int(len(r))
    return q, len(r)


def _estrutura_60m(con, ativo, agora_ms, cache):
    """Maxima e minima das duas ultimas barras fechadas de 60 min do dia.

    Usa o fechamento de cada barra de 1 min (a serie que o motor_regioes._dados_dia entrega): e a
    estrutura de 60 min dos fechamentos, nao o extremo intrabarra.
    """
    dia = _dia_idx(agora_ms)
    df = _dados_dia(con, ativo, dia, int(agora_ms), cache)
    if df.empty:
        return None
    cfg = mr.ATIVOS.get(ativo)
    ini = dia * DIA_MS + (cfg["sessao"][0] if cfg else 540) * 60_000
    passo = 60 * 60_000
    b = df.assign(k=(df.ts_ms - ini) // passo).groupby("k").agg(alta=("last", "max"), baixa=("last", "min"),
                                                                fim=("ts_ms", "max"))
    b = b[(ini + (b.index + 1) * passo) <= int(agora_ms)]
    if len(b) < 1:
        return None
    ult = b.iloc[-2:] if len(b) >= 2 else b.iloc[-1:]
    return dict(alta=float(ult.alta.max()), baixa=float(ult.baixa.min()), barras=int(len(ult)))


# ------------------------------------------------------------------ mapa

def mapa_swing(con, ativo, agora_ms, cache=None):
    """Mapa de swing do ativo: areas de valor, VWAPs ancoradas, movimento semanal e estrutura de 60 min."""
    ativo = mr.normalizar(ativo) if str(ativo).upper() in mr.ALIAS else str(ativo).upper()
    if ativo not in ATIVOS:
        raise ValueError(f"ativo de swing desconhecido: {ativo}")
    cache = {} if cache is None else cache
    agora_ms = int(agora_ms)
    dia = _dia_idx(agora_ms)
    dia_iso = _iso(dia)
    preco, data_preco = _preco_info(con, ativo, agora_ms)
    out = dict(ativo=ativo, dia=dia_iso, preco=preco, preco_data=data_preco, areas={}, vwaps={}, faltando=[])
    if not preco:
        out["faltando"].append("sem preco no instante")
        return out
    if data_preco and data_preco < dia_iso:
        out["faltando"].append(f"sem barras do dia: preco de {data_preco}")
    passo = mr.ATIVOS[ativo]["passo"] if ativo in mr.ATIVOS else 0.005
    for n in DIAS_VA:
        dias = _dias(con, ativo, dia, n)
        if not dias:
            out["faltando"].append(f"sem barras de 1 min para a area de {n} pregoes")
            continue
        pf = _perfil(con, ativo, dias, passo, cache)
        if pf:
            out["areas"][f"{n}d"] = dict(poc=pf["poc"], vah=pf["vah"], val=pf["val"],
                                         dias=len(dias), passo=pf["passo"])
        else:
            out["faltando"].append(f"area de {n} pregoes sem volume")
    for nome, (de, _desc) in {
        "rolagem": _rolagem(ativo, dia_iso),
        "copom": _ultimo_copom(con, dia_iso),
        "mes": (_inicio_mes(dia_iso), "inicio do mes"),
    }.items():
        valor, n_dias, vol = _vwap(con, ativo, (date.fromisoformat(de) - date(1970, 1, 1)).days if de else None,
                                   agora_ms, cache)
        out["vwaps"][nome] = dict(valor=valor, ancora=de, dias=n_dias, volume=vol)
        if valor is None:
            out["faltando"].append(f"VWAP ancorada em {nome} indisponivel"
                                   + (" (ancora fora do historico)" if de else ""))
    iv, fonte_iv = _iv_atm(con, agora_ms, ativo)
    out["iv_atm"], out["iv_fonte"] = iv, fonte_iv
    if iv:
        sigma = float(preco) * float(iv) * math.sqrt(5.0 / 252.0)
        out["movimento_semanal"] = dict(iv=iv, sigma_pts=sigma, minimo=float(preco) - sigma,
                                        maximo=float(preco) + sigma, pregoes=5)
    else:
        out["movimento_semanal"] = None
        out["faltando"].append(f"movimento esperado semanal: {fonte_iv}")
    q, n_q = _quantis_5d(con, ativo, agora_ms, preco, cache)
    out["quantis_5d"] = dict(q, n=n_q) if q else None
    if not q:
        out["faltando"].append("quantis empiricos de 5 pregoes sem historico")
    out["estrutura_60m"] = _estrutura_60m(con, ativo, agora_ms, cache)
    out["atr_diario"] = _atr_diario(con, ativo, dia, cache=cache)
    return out


def _niveis(mapa):
    """Niveis candidatos do mapa de swing -> lista de {preco, fonte, peso, rotulo, tag}."""
    out = []

    def add(preco, fonte, rotulo, tag=None):
        if _finito(preco) and float(preco) > 0:
            out.append(dict(preco=float(preco), fonte=fonte, peso=PESOS[fonte], rotulo=rotulo, tag=tag))

    for chave, fonte, rotulo in (("20d", "va20", "20 pregoes"), ("60d", "va60", "60 pregoes")):
        a = mapa["areas"].get(chave)
        if not a:
            continue
        add(a["poc"], fonte, f"POC {rotulo}")
        add(a["vah"], fonte, f"VAH {rotulo}", "vah")
        add(a["val"], fonte, f"VAL {rotulo}", "val")
    for nome, rotulo in (("rolagem", "rolagem"), ("copom", "Copom"), ("mes", "mes")):
        v = mapa["vwaps"].get(nome, {}).get("valor")
        add(v, "vwap_anc", f"VWAP {rotulo}")
    mov = mapa.get("movimento_semanal")
    if mov:
        add(mov["maximo"], "semana", "Movimento semanal +1 sigma")
        add(mov["minimo"], "semana", "Movimento semanal -1 sigma")
    q = mapa.get("quantis_5d")
    if q:
        for k in ("q15", "q85"):
            add(q.get(k), "quantis", f"{k} de 5 pregoes", k)
        for k in ("q05", "q95"):
            add(q.get(k), "quantis_extremos", f"{k} de 5 pregoes", k)
    e = mapa.get("estrutura_60m")
    if e:
        add(e["alta"], "estrutura60", "Maxima 60 min", "vah")
        add(e["baixa"], "estrutura60", "Minima 60 min", "val")
    return out


def _plano_swing(zona, direcao, atr_d, zonas, mapa, custo_lado):
    """Stop 0,5 x ATR diario alem da borda e alvos nas zonas/POC/VWAP seguintes; R:R liquido."""
    entrada = zona["preco_centro"]
    if not atr_d:
        return dict(entrada_min=zona["preco_min"], entrada_max=zona["preco_max"], stop=None,
                    alvo_1=None, alvo_2=None, rr=None)
    stop = zona["preco_max"] + MULT_STOP_ATR * atr_d if direcao == "VENDA" else \
        zona["preco_min"] - MULT_STOP_ATR * atr_d
    if direcao == "VENDA":
        base = zona["preco_min"]
        cand = [z["preco_centro"] for z in zonas if z is not zona and z["preco_centro"] < base
                and z["forca"] >= FORCA_MIN]
        cand += [v for v in (mapa.get("poc_hoje"), mapa["vwaps"].get("mes", {}).get("valor"),
                             mapa["areas"].get("20d", {}).get("poc")) if _finito(v) and v < base]
        cand = sorted(set(round(float(v), 4) for v in cand), reverse=True)
    else:
        base = zona["preco_max"]
        cand = [z["preco_centro"] for z in zonas if z is not zona and z["preco_centro"] > base
                and z["forca"] >= FORCA_MIN]
        cand += [v for v in (mapa.get("poc_hoje"), mapa["vwaps"].get("mes", {}).get("valor"),
                             mapa["areas"].get("20d", {}).get("poc")) if _finito(v) and v > base]
        cand = sorted(set(round(float(v), 4) for v in cand))
    a1 = cand[0] if cand else None
    a2 = cand[1] if len(cand) > 1 else None
    rr = None
    if a1 is not None:
        custo = 2 * custo_lado
        if direcao == "VENDA" and stop > entrada:
            rr = ((entrada - a1) - custo) / ((stop - entrada) + custo)
        elif direcao == "COMPRA" and entrada > stop:
            rr = ((a1 - entrada) - custo) / ((entrada - stop) + custo)
    return dict(entrada_min=zona["preco_min"], entrada_max=zona["preco_max"], stop=float(stop),
                alvo_1=a1, alvo_2=a2, rr=float(rr) if rr is not None else None)


def zonas_swing(con, ativo, agora_ms, vies=0.0, cache=None):
    """Zonas de swing (horizonte 'SWING') do ativo no instante, no formato do motor_regioes.regioes.

    Agrupa os niveis do mapa de swing com delta = 0,5 x ATR diario e classifica pelo vies com as mesmas
    regras do daytrade (a favor, contra e alvo). Ate 3 zonas acima e 3 abaixo com forca >= 35.
    """
    if str(ativo).upper() in mr.ALIAS:
        ativo = mr.normalizar(ativo)
    if ativo not in ATIVOS:
        raise ValueError(f"ativo de swing desconhecido: {ativo}")
    cache = {} if cache is None else cache
    agora_ms = int(agora_ms)
    mapa = mapa_swing(con, ativo, agora_ms, cache)
    preco = mapa.get("preco")
    niveis = _niveis(mapa)
    atr_d = mapa.get("atr_diario")
    if not preco or not niveis or not atr_d or atr_d <= 0:
        return []
    cfg = mr.ATIVOS.get(ativo, dict(custo_lado=0.005 if ativo == "DI1F29" else 0.01))
    delta = DELTA_ATR_SWING * atr_d
    zonas = mr.clusterizar(niveis, delta)
    acima = [z for z in zonas if z["preco_centro"] > preco and z["forca"] >= FORCA_MIN]
    abaixo = [z for z in zonas if z["preco_centro"] <= preco and z["forca"] >= FORCA_MIN]
    acima.sort(key=lambda z: (-z["forca"], z["preco_centro"] - preco))
    abaixo.sort(key=lambda z: (-z["forca"], preco - z["preco_centro"]))
    selecionadas = [(z, "ACIMA") for z in acima[:MAX_POR_LADO]] + [(z, "ABAIXO") for z in abaixo[:MAX_POR_LADO]]
    selecionadas.sort(key=lambda par: -par[0]["preco_centro"])
    out, contagem = [], {"ACIMA": 0, "ABAIXO": 0}
    mapa["poc_hoje"] = _poc_do_dia(con, ativo, agora_ms, cache)
    for zona, lado in selecionadas:
        contagem[lado] += 1
        alinhamento, direcao = mr._classificar(zona, preco, vies)
        plano = _plano_swing(zona, direcao, atr_d, zonas, mapa, cfg["custo_lado"])
        out.append(dict(id_zona=mr._id_zona(lado, contagem[lado]), horizonte="SWING", ativo=ativo, lado=lado,
                        preco_min=zona["preco_min"], preco_centro=zona["preco_centro"],
                        preco_max=zona["preco_max"], forca=zona["forca"], componentes=zona["componentes"],
                        alinhamento=alinhamento, direcao=direcao,
                        contem_preco=bool(zona["preco_min"] <= preco <= zona["preco_max"]),
                        distancia_pts=float(zona["preco_centro"] - preco),
                        distancia_pct=float((zona["preco_centro"] - preco) / preco * 100),
                        **plano))
    return out


def _poc_do_dia(con, ativo, agora_ms, cache):
    """POC do pregao ate agora (referencia de alvo do swing)."""
    dia = _dia_idx(agora_ms)
    df = _dados_dia(con, ativo, dia, int(agora_ms), cache)
    cfg = mr.ATIVOS.get(ativo)
    if df.empty or (df.vc + df.vv).sum() <= 0:
        return None
    pf = _perfil_va(df, cfg["passo"] if cfg else 0.005)
    return pf["poc"] if pf else None


# ------------------------------------------------------------------ invalidacao

def _fluxo_estrangeiro(con, dia_iso, n=5):
    """Saldo do estrangeiro nos ultimos n pregoes JA PUBLICADOS (data_pub <= dia_iso) -> (soma_mil, n, data)."""
    try:
        rows = con.execute("SELECT data_ref, saldo_mil, data_pub FROM fluxo_diario "
                           "WHERE tipo='Investidor Estrangeiro' AND data_pub <= ? ORDER BY data_ref DESC",
                           (dia_iso,)).fetchall()
    except sqlite3.OperationalError:
        return None, 0, None
    if not rows:
        return None, 0, None
    ult = rows[0]
    soma = float(sum(r[1] or 0.0 for r in rows[:n]))
    return soma, min(n, len(rows)), str(ult[0])[:10]


def _di(con, agora_ms):
    """(ultimo, 5 pregoes atras, data) do DI1F29 em % a.a. ate agora."""
    dia = _dia_idx(agora_ms)
    atual, data_atual = _preco_info(con, "DI1F29", agora_ms)
    closes = _closes(con, "DI1F29", dia, 6)
    anterior = closes[0] if closes else None
    return atual, anterior, data_atual


def invalidacao(con, agora_ms, vies=0.0):
    """Frases objetivas de invalidacao da tese vigente, com os numeros do instante -> lista de str."""
    agora_ms = int(agora_ms)
    dia_iso = _iso(_dia_idx(agora_ms))
    vies = float(vies or 0.0)
    frases = []
    di_atual, di_5d, data_di = _di(con, agora_ms)
    soma, n_fluxo, data_fluxo = _fluxo_estrangeiro(con, dia_iso)
    bova = mr.preco_atual(con, "BOVA11", agora_ms) or _preco(con, "BOVA11", agora_ms)
    vwap20 = None
    dias20 = _dias(con, "BOVA11", _dia_idx(agora_ms), 20)
    if dias20 and bova:
        vwap20, _, _ = _vwap(con, "BOVA11", min(dias20), agora_ms, {})
    quando_di = f"fechamento de {data_di}" if data_di and data_di < dia_iso else "ultimo"
    saldo_fluxo = (f"saldo publicado de {_br(soma / 1e6, 2)} bi em {n_fluxo} pregoes ate {data_fluxo}"
                   if soma is not None else "sem fluxo publicado")
    if vies <= -25:
        p1 = (f"Tese vendedora vale enquanto DI1F29 fechar acima de {_br(di_atual)}% a.a. "
              f"({quando_di} {_br(di_atual)}%, {_br(di_5d)}% cinco pregoes atras)")
        p2 = f"o estrangeiro seguir vendedor em 5 pregoes ({saldo_fluxo})"
        p3 = (f"e BOVA11 ficar abaixo da VWAP de 20 pregoes (R$ {_br(vwap20)}; ultimo R$ {_br(bova)})"
              if vwap20 is not None and bova else "e BOVA11 ficar abaixo da VWAP de 20 pregoes")
        frases.append(f"{p1}, {p2} {p3}.")
        if soma is not None and soma >= 0:
            frases.append(f"ATENCAO: o fluxo publicado do estrangeiro esta comprador ({saldo_fluxo}): a "
                          f"condicao de fluxo da tese vendedora esta rompida.")
        if vwap20 is not None and bova and bova > vwap20:
            frases.append(f"ATENCAO: BOVA11 ja esta acima da VWAP de 20 pregoes (R$ {_br(vwap20)}): a "
                          f"condicao de preco da tese vendedora esta rompida.")
        frases.append("Rompimento simetrico: WIN$ acima da maxima da semana anterior com fluxo estrangeiro "
                      "publicado positivo em 5 pregoes desfaz a leitura de queda.")
    elif vies >= 25:
        p1 = (f"Tese compradora vale enquanto DI1F29 fechar abaixo de {_br(di_atual)}% a.a. "
              f"({quando_di} {_br(di_atual)}%, {_br(di_5d)}% cinco pregoes atras)")
        p2 = f"o estrangeiro seguir comprador em 5 pregoes ({saldo_fluxo})"
        p3 = (f"e BOVA11 ficar acima da VWAP de 20 pregoes (R$ {_br(vwap20)}; ultimo R$ {_br(bova)})"
              if vwap20 is not None and bova else "e BOVA11 ficar acima da VWAP de 20 pregoes")
        frases.append(f"{p1}, {p2} {p3}.")
        if soma is not None and soma < 0:
            frases.append(f"ATENCAO: o fluxo publicado do estrangeiro esta vendedor ({saldo_fluxo}): a "
                          f"condicao de fluxo da tese compradora esta rompida.")
        if vwap20 is not None and bova and bova < vwap20:
            frases.append(f"ATENCAO: BOVA11 ja esta abaixo da VWAP de 20 pregoes (R$ {_br(vwap20)}): a "
                          f"condicao de preco da tese compradora esta rompida.")
    else:
        frases.append(f"Sem vies direcional (|vies| < 25): a tese nao esta definida; DI1F29 em "
                      f"{_br(di_atual)}% a.a. e BOVA11 em R$ {_br(bova)} contra VWAP de 20 pregoes de "
                      f"R$ {_br(vwap20)}.")
        frases.append("A leitura muda se o DI1F29 romper a maxima/minima de 5 pregoes ou se o fluxo "
                      "publicado do estrangeiro trocar de sinal.")
    if _finito(di_atual) and _finito(di_5d):
        variacao = di_atual - di_5d
        frases.append(f"Juros: DI1F29 variou {_br(variacao)} p.p. em 5 pregoes "
                      f"({'abertura' if variacao > 0 else 'fechamento'} de curva).")
    if soma is not None:
        frases.append(f"Fluxo: o ultimo dado publicado do estrangeiro e de {data_fluxo} (D+2); "
                      f"saldo de {_br(soma / 1e6, 2)} bi em {n_fluxo} pregoes.")
    return frases


def _agora_de(texto, agora_ms=None):
    """Converte 'AAAA-MM-DD HH:MM' na convencao do banco (hora de Brasilia como epoch UTC)."""
    if not texto:
        return int(agora_ms)
    d, _, h = str(texto).partition(" ")
    dia = (date.fromisoformat(d) - date(1970, 1, 1)).days
    hh, _, mm = (h or "00:00").partition(":")
    return dia * DIA_MS + (int(hh) * 60 + int(mm or 0)) * 60_000


def main(argv=None):
    """Imprime o mapa, as zonas de swing e a invalidacao do instante.

    Uso: python swing.py [--ativo WIN$] [--vies -60] [--agora "2026-11-17 15:00"] [--db ...]
    """
    import argparse
    import json
    import relogio
    ap = argparse.ArgumentParser(description="Zonas de swing e invalidacao da tese")
    ap.add_argument("--ativo", default="WIN$")
    ap.add_argument("--vies", type=float, default=0.0)
    ap.add_argument("--agora", default=None, help='ex.: "2026-11-17 15:00" (padrao: agora)')
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--mapa", action="store_true", help="imprime tambem o mapa (areas, VWAPs, movimento)")
    args = ap.parse_args(argv)
    agora = _agora_de(args.agora, relogio.local_br_ms())
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.mapa:
            print(json.dumps(mapa_swing(con, args.ativo, agora), ensure_ascii=False, indent=1, default=str))
        zonas = zonas_swing(con, args.ativo, agora, args.vies)
        print(json.dumps(zonas, ensure_ascii=False, indent=1, default=str))
        print("\nInvalidacao da tese:")
        for f in invalidacao(con, agora, args.vies):
            print("-", f)
    finally:
        con.close()
    return zonas


if __name__ == "__main__":
    main()
