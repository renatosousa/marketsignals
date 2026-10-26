"""Juros: vencimentos de DI1/DAP pelo codigo, interpolacao da curva DI e inflacao implicita (breakeven).

Taxas em % a.a., base 252 dias uteis (convencao da B3 para DI1 e DAP). Dias uteis sem o calendario de
feriados (np.busday_count, como no resto do projeto): o erro e de 1-3 du em prazos de anos e quase se
cancela na razao (1+DI)/(1+DAP), pois os dois lados usam a mesma contagem.

  breakeven = ((1 + DI(du)) / (1 + DAP(du)) - 1) * 100     DI interpolado no vencimento do DAP
  DI(du): flat-forward entre os vertices (log do fator (1+r)^(du/252) linear em du), padrao B3/ANBIMA.
"""
import math
from datetime import date, timedelta

import numpy as np

MESES = {c: i + 1 for i, c in enumerate("FGHJKMNQUVXZ")}


def _dia_util(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def vencimento(symbol):
    """DI1F27 -> 1o dia util de jan/2027 (1o de janeiro e feriado); DAPK29 -> dia 15 de mai/2029 (ou o
    proximo dia util). None se o codigo nao for de DI1/DAP com vencimento."""
    s = symbol.upper()
    raiz, cod = (s[:3], s[3:]) if s.startswith("DI1") else (s[:3], s[3:]) if s.startswith("DAP") else (None, None)
    if not raiz or len(cod) != 3 or cod[0] not in MESES or not cod[1:].isdigit():
        return None
    mes, ano = MESES[cod[0]], 2000 + int(cod[1:])
    if raiz == "DI1":
        return _dia_util(date(ano, mes, 2 if mes == 1 else 1))
    return _dia_util(date(ano, mes, 15))


def du(d0, d1):
    return int(np.busday_count(d0, d1))


def interp_flat_forward(prazos, taxas, alvo):
    """prazos (du, crescentes) e taxas (% a.a.) -> taxa em `alvo` du. Fora da faixa extrapola com a
    taxa a termo do trecho mais proximo. None se faltar dado."""
    pts = sorted((p, t) for p, t in zip(prazos, taxas) if p and p > 0 and t is not None and not math.isnan(t))
    if not pts or alvo is None or alvo <= 0:
        return None
    if len(pts) == 1:
        return pts[0][1]
    lf = lambda p, t: p / 252 * math.log1p(t / 100)
    i = next((k for k in range(1, len(pts)) if alvo <= pts[k][0]), len(pts) - 1)
    (p0, t0), (p1, t1) = pts[i - 1], pts[i]
    f0, f1 = lf(p0, t0), lf(p1, t1)
    f = f0 + (f1 - f0) * (alvo - p0) / (p1 - p0)
    return (math.exp(f * 252 / alvo) - 1) * 100


def breakeven(di, dap):
    """Taxas % a.a. -> inflacao implicita % a.a."""
    if di is None or dap is None:
        return None
    return ((1 + di / 100) / (1 + dap / 100) - 1) * 100


def breakeven_dap(ref, taxas_di, dap_symbol, taxa_dap):
    """ref: data da cotacao; taxas_di: {DI1Fxx: taxa}; -> (breakeven, di_interpolado, du_dap)."""
    vd = vencimento(dap_symbol)
    if vd is None or taxa_dap is None:
        return None, None, None
    d_dap = du(ref, vd)
    prazos, tx = [], []
    for s, t in taxas_di.items():
        v = vencimento(s)
        if v and t is not None:
            prazos.append(du(ref, v))
            tx.append(t)
    di = interp_flat_forward(prazos, tx, d_dap)
    return breakeven(di, taxa_dap), di, d_dap


def breakeven_tesouro(taxa_pre, taxa_ipca):
    """Inflacao implicita do Tesouro Direto: Prefixado vs IPCA+.
    
    Args:
        taxa_pre: Taxa do Tesouro Prefixado em % a.a.
        taxa_ipca: Taxa real do Tesouro IPCA+ em % a.a. (juro real, sem a inflacao)
    
    Returns:
        Inflacao implicita em % a.a., ou None se faltar dado.
    
    Formula: breakeven = (1 + taxa_pre) / (1 + taxa_ipca) - 1
    
    Exemplo: Prefixado 2029 = 12,64% vs IPCA+ 2029 = IPCA+6,77%
             breakeven = (1.1264 / 1.0677) - 1 = 0.0550 = 5,50% a.a.
    """
    if taxa_pre is None or taxa_ipca is None:
        return None
    return ((1 + taxa_pre / 100) / (1 + taxa_ipca / 100) - 1) * 100


def parear_vencimentos_tesouro(titulos):
    """Pareia titulos Prefixado e IPCA+ por vencimentos proximos (mesma safra).
    
    Args:
        titulos: Lista de dicts com chaves 'tipo_titulo', 'vencimento', 'taxa_compra'/'taxa_venda'
    
    Returns:
        Lista de pares ordenados por vencimento: [
            {
                "safra": 2029,
                "venc_pre": "2029-01-01",
                "venc_ipca": "2029-05-15",
                "taxa_pre": 12.64,
                "taxa_ipca": 6.77,
                "breakeven": 5.50,
                "diferenca_dias": 134
            },
            ...
        ]
    
    Nota: Tesouro Prefixado vence em 01/01, Tesouro IPCA+ vence em 15/05 ou 15/08.
          Pares sao formados por ano (safra), com diferenca de meses aceita.
    """
    from datetime import date
    
    # Separar por tipo
    prefixados = {}
    ipcas = {}
    
    for t in titulos:
        tipo = t.get("tipo_titulo", "")
        venc = t.get("vencimento", "")
        
        # Pegar a taxa media (compra+venda)/2 ou compra se venda nao existir
        taxa_c = t.get("taxa_compra")
        taxa_v = t.get("taxa_venda")
        if taxa_c is not None and taxa_v is not None:
            taxa = (taxa_c + taxa_v) / 2
        elif taxa_c is not None:
            taxa = taxa_c
        else:
            taxa = taxa_v
        
        if taxa is None or not venc:
            continue
        
        try:
            venc_date = date.fromisoformat(venc)
            ano = venc_date.year
        except (ValueError, AttributeError):
            continue
        
        # So os titulos SEM cupom (Tesouro Prefixado = LTN; Tesouro IPCA+ = NTN-B Principal): as versoes "com Juros
        # Semestrais" (NTN-F e NTN-B) tem outra duracao e, por ano, sobrescreviam o titulo certo no dicionario.
        sem_cupom = "Juros Semestrais" not in tipo
        if tipo.startswith("Tesouro Prefixado") and sem_cupom:
            prefixados[ano] = {"vencimento": venc, "taxa": taxa}
        elif tipo.startswith("Tesouro IPCA+") and sem_cupom:
            ipcas[ano] = {"vencimento": venc, "taxa": taxa}
    
    # Parear por ano (safra)
    pares = []
    safras = sorted(set(prefixados.keys()) & set(ipcas.keys()))
    
    for safra in safras:
        pre = prefixados[safra]
        ipca = ipcas[safra]
        
        be = breakeven_tesouro(pre["taxa"], ipca["taxa"])
        
        if be is not None:
            try:
                v_pre = date.fromisoformat(pre["vencimento"])
                v_ipca = date.fromisoformat(ipca["vencimento"])
                diff_dias = abs((v_ipca - v_pre).days)
            except (ValueError, AttributeError):
                diff_dias = None
            
            pares.append({
                "safra": safra,
                "venc_pre": pre["vencimento"],
                "venc_ipca": ipca["vencimento"],
                "taxa_pre": pre["taxa"],
                "taxa_ipca": ipca["taxa"],
                "breakeven": be,
                "diferenca_dias": diff_dias
            })
    
    return pares


if __name__ == "__main__":
    assert vencimento("DI1F27") == date(2027, 1, 4) and vencimento("DAPK29") == date(2029, 5, 15)
    assert vencimento("DAPQ28") == date(2028, 8, 15) and vencimento("WDO$") is None
    assert abs(interp_flat_forward([100, 300], [10, 12], 300) - 12) < 1e-9
    assert 10 < interp_flat_forward([100, 300], [10, 12], 200) < 12
    assert abs(breakeven(12.0, 6.0) - 5.660377) < 1e-5
    assert abs(breakeven_tesouro(12.64, 6.77) - 5.4978) < 0.01
    print("ok")
