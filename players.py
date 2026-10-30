"""Evolucao dos players do WIN no dia, das corretoras do Tryd (coletor_tryd.py: tryd_saldo e tryd_fita) -> pagina /players.

Corretora NAO e tipo de investidor. O mapa abaixo e um PRIOR (hipotese de quem costuma operar por cada casa), nao um
dado: XP, BTG, Genial e Ideal misturam pessoa fisica e institucional; so os bancos globais sugerem estrangeiro. A B3
publica o fluxo por tipo de investidor apenas das acoes, com 2 pregoes de atraso (fluxo_estrangeiro.py), e e com elas que
o mapa deve ser ajustado e validado quando houver semanas de coleta. Market maker/HFT nao e publicado pela B3: aqui so
aparece o giro de cada corretora (volume negociado / |saldo|), que e um sinal de quem opera dos dois lados, nao uma prova.

Series (todas do WINZ26, ts em segundos, hora de Brasilia como epoch "UTC", como no banco):
  saldo   posicao liquida acumulada no dia por grupo (contratos): soma de qtd = qtdc - qtdv das corretoras do grupo.
          A soma de todos os grupos e zero (cada negocio tem comprador e vendedor).
  agr     saldo de agressao acumulado por grupo (sagr = agrc - agrv): quem comprou/vendeu a mercado.
  preco   ultimo negocio da fita em cada ponto.
  rlp     fita: negocios com agressor "RLP". Na fita de 30/10/2026 TODOS os 6.000 negocios RLP tem a mesma corretora
          nas duas pontas (XP, BTG, Genial, Santander...): o RLP e o fluxo do varejo cruzado dentro da corretora com o
          provedor de liquidez, entao o saldo liquido por grupo seria sempre zero. Mede-se o volume: RLP acumulado por
          grupo, % do volume capturado que foi RLP por faixa e, por corretora, a parte do volume dela que foi RLP
          (sinal de varejo). A fita capta 45-75% dos negocios (a janela do Tryd guarda 31 linhas), entao e uma amostra;
          o saldo acima e a fonte exata dos totais.
"""
import numpy as np

DIA_MS = 86_400_000
PONTOS = 600
GRUPOS = ["Estrangeiro", "Institucional", "Varejo", "Misto", "Liquidez", "Outros"]
DESCRICAO = {
    "Estrangeiro": "bancos globais (Goldman, Morgan Stanley, JP Morgan, UBS, Merrill, Citi)",
    "Institucional": "bancos e gestoras locais (Itau, Santander Institucional, Safra, ABN...)",
    "Varejo": "plataformas de pessoa fisica (Ideal, Agora, Inter, Ativa, Nova Futura, C6...)",
    "Misto": "XP, BTG, Genial, Santander: pessoa fisica e institucional juntos",
    "Liquidez": "intermediadoras (Tullett, BGC Liquidez)",
    "Outros": "corretoras sem grupo no mapa",
}
MAPA = {
    "Goldman": "Estrangeiro", "Morgan": "Estrangeiro", "JP Morgan": "Estrangeiro", "UBS": "Estrangeiro",
    "Merrill": "Estrangeiro", "Citigroup": "Estrangeiro",
    "Itau": "Institucional", "Santander Institucional": "Institucional", "Safra": "Institucional",
    "ABN": "Institucional", "Stonex": "Institucional",
    "Ideal": "Varejo", "Agora": "Varejo", "Inter": "Varejo", "Ativa": "Varejo", "Nova Futura": "Varejo",
    "C6": "Varejo", "Terra": "Varejo", "Necton": "Varejo", "Mirae": "Varejo", "Warren Rena": "Varejo",
    "CM Capital": "Varejo", "PAGINVEST": "Varejo", "LEV": "Varejo",
    "XP": "Misto", "BTG": "Misto", "Genial": "Misto", "Santander": "Misto",
    "Tullett": "Liquidez", "BGC Liquidez": "Liquidez",
}


def grupo(corretora):
    return MAPA.get(corretora, "Outros")


def _reduzir(ts, pontos=PONTOS):
    """Indices de ate `pontos` instantes igualmente espacados (sempre inclui o ultimo)."""
    n = len(ts)
    if n <= pontos:
        return np.arange(n)
    idx = np.unique(np.linspace(0, n - 1, pontos).round().astype(int))
    return idx


def saldo_por_grupo(linhas):
    """linhas = [(ts_ms, brk, qtd, sagr, qtdc, qtdv)] de um dia -> (ts_ms, {grupo: {"saldo": [], "agr": []}}, ultima foto).
    Uma corretora que ainda nao apareceu na tabela vale 0 (a tabela so lista quem ja operou)."""
    if not linhas:
        return [], {}, []
    por_ts = {}
    for ts, brk, qtd, sagr, qtdc, qtdv in linhas:
        por_ts.setdefault(ts, {})[brk] = (qtd or 0.0, sagr or 0.0, qtdc or 0.0, qtdv or 0.0)
    tempos = sorted(por_ts)
    saldo = {g: np.zeros(len(tempos)) for g in GRUPOS}
    agr = {g: np.zeros(len(tempos)) for g in GRUPOS}
    for i, ts in enumerate(tempos):
        for brk, (q, s, _, _) in por_ts[ts].items():
            saldo[grupo(brk)][i] += q
            agr[grupo(brk)][i] += s
    idx = _reduzir(tempos)
    ts_ms = [tempos[i] for i in idx]
    series = {g: {"saldo": saldo[g][idx].tolist(), "agr": agr[g][idx].tolist()} for g in GRUPOS
              if np.any(saldo[g]) or np.any(agr[g])}
    ultima = por_ts[tempos[-1]]
    return ts_ms, series, ultima


def tabela(ultima, rlp_por_brk):
    """Linhas da tabela por corretora na ultima foto, ordenadas por volume negociado."""
    out = []
    for brk, (q, s, c, v) in ultima.items():
        vol = c + v
        out.append({"corretora": brk, "grupo": grupo(brk), "saldo": q, "agr": s, "volume": vol,
                    "giro": vol / abs(q) if q else None,
                    "rlp": rlp_por_brk.get(brk)})
    out.sort(key=lambda r: -r["volume"])
    return out


def fita_rlp(fita, bins_s=300):
    """fita = [(ts_ms, hora, qtd, preco, comprador, vendedor, agressor)] -> serie de RLP e preco.
    Por faixa de `bins_s` s: volume total, volume de RLP e volume de RLP por grupo (da corretora
    compradora; no RLP a compradora e a vendedora sao a mesma)."""
    if not fita:
        return {"t": [], "pct_rlp": [], "preco": [], "acumulado": {}}, {}
    a = np.array([(r[0], r[2] or 0, r[3] or 0) for r in fita], dtype=float)
    ts = a[:, 0] // 1000
    faixa = (ts // bins_s).astype(int)
    faixas = np.unique(faixa)
    pos = {f: i for i, f in enumerate(faixas)}
    vol, vol_rlp, preco = np.zeros(len(faixas)), np.zeros(len(faixas)), np.full(len(faixas), np.nan)
    por_grupo = {g: np.zeros(len(faixas)) for g in GRUPOS}
    tot, rlp_b = {}, {}
    for (ts_ms, _, q, p, comp, vend, agr), f in zip(fita, faixa):
        i, q = pos[f], q or 0
        vol[i] += q
        preco[i] = p if p else preco[i]
        for b in (comp, vend):
            tot[b] = tot.get(b, 0) + q
        if agr == "RLP":
            vol_rlp[i] += q
            por_grupo[grupo(comp)][i] += q
            for b in (comp, vend):
                rlp_b[b] = rlp_b.get(b, 0) + q
    pct = {b: rlp_b.get(b, 0) / v for b, v in tot.items() if v}  # parte do volume da corretora que foi RLP
    t = (faixas * bins_s + bins_s // 2).tolist()
    return {"t": t, "pct_rlp": np.where(vol > 0, vol_rlp / np.where(vol > 0, vol, 1), np.nan).tolist(),
            "preco": preco.tolist(),
            "acumulado": {g: np.cumsum(v).tolist() for g, v in por_grupo.items() if np.any(v)}}, pct


def montar(con, dia, ativo="WINZ26"):
    """Resposta do /api/players. `dia` = indice do dia (ts_ms // 86400000) ou None para o ultimo com saldo."""
    if dia is None:
        dia = con.execute("SELECT max(ts_ms) FROM tryd_saldo WHERE ativo=?", (ativo,)).fetchone()[0]
        if dia is None:
            return {"vazio": True, "ativo": ativo}
        dia //= DIA_MS
    lo, hi = dia * DIA_MS, (dia + 1) * DIA_MS
    linhas = con.execute("SELECT ts_ms, brk, qtd, sagr, qtdc, qtdv FROM tryd_saldo WHERE ativo=? AND ts_ms>=? AND ts_ms<? "
                         "ORDER BY ts_ms", (ativo, lo, hi)).fetchall()
    if not linhas:
        return {"vazio": True, "ativo": ativo, "dia": dia}
    ts_ms, series, ultima = saldo_por_grupo(linhas)
    fita = con.execute("SELECT ts_ms, hora, qtd, preco, comprador, vendedor, agressor FROM tryd_fita "
                       "WHERE ativo=? AND dia=? ORDER BY num", (ativo, dia)).fetchall()
    rlp, pct = fita_rlp(fita)
    dias = [r[0] for r in con.execute("SELECT DISTINCT ts_ms / ? FROM tryd_saldo WHERE ativo=? ORDER BY 1", (DIA_MS, ativo))]
    return {
        "ativo": ativo, "dia": dia, "dias": dias,
        "t": [x // 1000 for x in ts_ms], "series": series,
        "foto_inicio": linhas[0][0] // 1000, "foto_fim": ts_ms[-1] // 1000, "fotos": len({r[0] for r in linhas}),
        "rlp": {**rlp, "t": rlp["t"]},
        "corretoras": tabela(ultima, pct),
        "grupos": {g: DESCRICAO[g] for g in GRUPOS},
        "negocios_fita": len(fita),
    }
