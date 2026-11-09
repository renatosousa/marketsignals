"""Evolucao dos players no dia (WIN, WDO e as acoes com janela no Tryd), das corretoras do Tryd (coletor_tryd.py: tryd_saldo
e tryd_fita) -> pagina /players?ativo=.

Corretora NAO e tipo de investidor. O mapa abaixo e um PRIOR (hipotese de quem costuma operar por cada casa), nao um
dado: XP, BTG, Genial e Ideal misturam pessoa fisica e institucional (XP, BTG e Genial, as maiores, tem grupo proprio para
nao esconder uma atras da outra); so os bancos globais sugerem estrangeiro. A B3
publica o fluxo por tipo de investidor apenas das acoes, com 2 pregoes de atraso (fluxo_estrangeiro.py), e e com elas que
o mapa deve ser ajustado e validado quando houver semanas de coleta. Market maker/HFT nao e publicado pela B3: aqui so
aparece o giro de cada corretora (volume negociado / |saldo|), que e um sinal de quem opera dos dois lados, nao uma prova.

Series (de um ativo, ts em segundos, hora de Brasilia como epoch "UTC", como no banco). Futuros em contratos; acoes em
acoes (BOVA11 em cotas), com foto a cada 60 s e sem fita (o coletor so le o saldo delas: sem RLP nem agressao estimada):
  saldo   posicao liquida acumulada no dia por grupo: soma de qtd = qtdc - qtdv das corretoras do grupo.
          A soma de todos os grupos e zero (cada negocio tem comprador e vendedor).
  agr     saldo de agressao acumulado por grupo (sagr = agrc - agrv): quem comprou/vendeu a mercado.
  rlp     fita: negocios com agressor "RLP". Na fita de 30/10/2026 TODOS os 6.000 negocios RLP tem a mesma corretora
          nas duas pontas (XP, BTG, Genial, Santander...): o RLP e o fluxo do varejo cruzado dentro da corretora com o
          provedor de liquidez, entao o saldo liquido por grupo seria sempre zero. Mede-se o volume: RLP acumulado por
          grupo, % do volume capturado que foi RLP por faixa e, por corretora, a parte do volume dela que foi RLP
          (sinal de varejo). A fita capta 45-75% dos negocios (a janela do Tryd guarda 31 linhas), entao e uma amostra;
          o saldo acima e a fonte exata dos totais.
"""
import datetime
import re

import numpy as np

DIA_MS = 86_400_000
ORDEM_ATIVOS = ("WIN", "WDO", "PETR4", "VALE3", "ITUB4", "BBDC4", "BOVA11")  # ordem do seletor (prefixo: o vencimento muda)
_FUTURO = re.compile(r"^(WIN|WDO|IND|DOL)[FGHJKMNQUVXZ]\d\d$")
PONTOS = 600
GRUPOS = ["Estrangeiro", "Institucional", "Varejo", "XP", "BTG", "Genial", "Misto", "Liquidez", "Outros"]
DESCRICAO = {
    "Estrangeiro": "bancos globais (Goldman, Morgan Stanley, JP Morgan, UBS, Merrill, Citi)",
    "Institucional": "bancos e gestoras locais (Itau, Santander Institucional, Safra, ABN...)",
    "Varejo": "plataformas de pessoa fisica (Ideal, Agora, Inter, Ativa, Nova Futura, C6...)",
    "XP": "XP sozinha: pessoa fisica e institucional juntos",
    "BTG": "BTG sozinho: pessoa fisica e institucional juntos",
    "Genial": "Genial sozinha: pessoa fisica e institucional juntos",
    "Misto": "Santander: pessoa fisica e institucional juntos (XP, BTG e Genial tem grupo proprio)",
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
    "XP": "XP", "BTG": "BTG", "Genial": "Genial", "Santander": "Misto",
    "Tullett": "Liquidez", "BGC Liquidez": "Liquidez",
    # so aparecem nas acoes (4/11/2026: menos de 0,1% do volume de PETR4 e VALE3)
    "NuInvest": "Varejo", "SCOTIABANK": "Estrangeiro",
}


def grupo(corretora):
    return MAPA.get(corretora, "Outros")


def unidade(ativo):
    return "contratos" if _FUTURO.match(ativo) else "cotas" if ativo.endswith("11") else "ações"


def _ordem_ativo(ativo):
    return next((i for i, p in enumerate(ORDEM_ATIVOS) if ativo.startswith(p)), len(ORDEM_ATIVOS)), ativo


def ativos_do_dia(con, dia):
    """Ativos com foto de saldo no dia (intervalo da chave primaria, que comeca por ts_ms), na ordem do seletor."""
    return sorted((r[0] for r in con.execute("SELECT DISTINCT ativo FROM tryd_saldo WHERE ts_ms>=? AND ts_ms<?",
                                             (dia * DIA_MS, (dia + 1) * DIA_MS))), key=_ordem_ativo)


def _reduzir(ts, pontos=PONTOS):
    """Indices de ate `pontos` instantes igualmente espacados (sempre inclui o ultimo)."""
    n = len(ts)
    if n <= pontos:
        return np.arange(n)
    idx = np.unique(np.linspace(0, n - 1, pontos).round().astype(int))
    return idx


def _num(v):
    return v if isinstance(v, (int, float)) else None  # vazio ou texto (DDE instavel) = sem leitura


def saldo_por_grupo(linhas):
    """linhas = [(ts_ms, brk, qtd, sagr, qtdc, qtdv)] de um dia -> (ts_ms, {grupo: {"saldo": [], "agr": []}}, ultima foto, tem_agr).
    `agr` so entra se a janela do Tryd preencher as colunas de agressao (3/11/2026 vieram vazias o dia todo).
    Uma corretora que ainda nao apareceu na tabela vale 0 (a tabela so lista quem ja operou).
    Celula vazia ou linha incoerente (qtd != qtdc - qtdv) vale o ultimo valor lido da corretora: o saldo e acumulado no
    dia e nao zera. Em 3/11/2026 o Tryd devolveu celulas vazias em 4% das linhas do WINZ26 (36% das fotos, nunca no WDO):
    a corretora "zerava" por uma foto e voltava, e o grupo dava saltos de ida e volta de 3 a 70 mil contratos (UBS,
    Morgan, Goldman, Safra, Itau, ABN)."""
    if not linhas:
        return [], {}, {}, False
    por_ts, tem_agr = {}, False
    for ts, brk, qtd, sagr, qtdc, qtdv in linhas:
        por_ts.setdefault(ts, []).append((brk, _num(qtd), _num(sagr), _num(qtdc), _num(qtdv)))
    tempos = sorted(por_ts)
    saldo = {g: np.zeros(len(tempos)) for g in GRUPOS}
    agr = {g: np.zeros(len(tempos)) for g in GRUPOS}
    atual = {}  # corretora -> [qtd, sagr, qtdc, qtdv]: o ultimo valor valido de cada campo
    for i, ts in enumerate(tempos):
        for brk, q, s, c, v in por_ts[ts]:
            a = atual.setdefault(brk, [0.0, 0.0, 0.0, 0.0])
            incoerente = None not in (q, c, v) and abs(q - (c - v)) >= 0.5  # lida enquanto a janela mudava
            if not incoerente:
                for j, x in ((0, q), (2, c), (3, v)):
                    if x is not None:
                        a[j] = x
            if s is not None:
                a[1], tem_agr = s, True
        for brk, (q, s, _, _) in atual.items():
            saldo[grupo(brk)][i] += q
            agr[grupo(brk)][i] += s
    idx = _reduzir(tempos)
    ts_ms = [tempos[i] for i in idx]
    series = {g: {"saldo": saldo[g][idx].tolist(), **({"agr": agr[g][idx].tolist()} if tem_agr else {})} for g in GRUPOS
              if np.any(saldo[g]) or np.any(agr[g])}
    ultima = {brk: tuple(a) for brk, a in atual.items()}
    return ts_ms, series, ultima, tem_agr


def agressao_da_fita(agg, bins_min=1):
    """ESTIMATIVA do saldo de agressao pela fita, para quando a janela de saldo do Tryd nao preenche as colunas de agressao
    (3/11/2026). Negocio com agressor "Comprador": a corretora compradora comprou a mercado (+qtd); "Vendedor": a vendedora
    vendeu a mercado (-qtd); RLP, Direto e Leilao nao entram. So conta o que a fita capturou (35-75% dos negocios) e o
    erro cresce com a rajada: serve para a direcao, nao para o valor.
    agg = [(minuto, comprador, vendedor, agressor, soma_qtd, n)] (fita_agregada) -> ({t, series:{grupo: acumulado}},
    {corretora: saldo})."""
    if not agg:
        return {"t": [], "series": {}}, {}
    faixas = sorted({r[0] // bins_min for r in agg})
    pos = {f: i for i, f in enumerate(faixas)}
    por_grupo = {g: np.zeros(len(faixas)) for g in GRUPOS}
    por_brk = {}
    for minuto, comp, vend, agr, q, _ in agg:
        q = q or 0
        if agr == "Comprador":
            por_grupo[grupo(comp)][pos[minuto // bins_min]] += q
            por_brk[comp] = por_brk.get(comp, 0) + q
        elif agr == "Vendedor":
            por_grupo[grupo(vend)][pos[minuto // bins_min]] -= q
            por_brk[vend] = por_brk.get(vend, 0) - q
    t = [f * bins_min * 60 + bins_min * 30 for f in faixas]
    series = {g: np.cumsum(v).tolist() for g, v in por_grupo.items() if np.any(v)}
    return {"t": t, "series": series}, por_brk


_CACHE_FITA = {}  # (ativo, dia) -> {"upto": minuto (exclusivo), "agg": {(minuto, comprador, vendedor, agressor): [qtd, n]}}
MINUTOS_ABERTOS = 2  # os ultimos minutos sao relidos a cada chamada: negocios descartados na leitura entram na passada seguinte


# 6/11/2026: com negocios de dois dias na janela, o Tryd poe a data na hora ("05/11/2026 18:31:22.221") e os 100 ultimos
# negocios de ontem entraram com o dia de hoje. O `num` deles (72 milhoes) e maior que o de hoje (15 milhoes): o ultimo
# negocio "do dia" virou um de ontem capturado as 08:55 e o RLP/agressao do /players parou as 08:57 o dia todo.
# 9/11/2026: com a janela SO com negocios de ontem (antes da abertura) o Tryd nao poe a data, e os 100 de sexta entraram
# as 08:55 com a hora sem data (18:24...). Negocio com hora depois do instante da captura (+2 min de folga) e de outro dia.
_SO_DO_DIA = ("AND NOT (hora LIKE '__/__/____%' AND substr(hora, 1, 10) <> ?) "
              "AND NOT (hora GLOB '[0-9][0-9]:*' AND substr(hora, 1, 8) > time(ts_ms / 1000 + 120, 'unixepoch'))")


def _data_br(dia):
    return (datetime.date(1970, 1, 1) + datetime.timedelta(days=dia)).strftime("%d/%m/%Y")


def fita_agregada(con, ativo, dia):
    """A fita do dia agregada por minuto/corretoras/agressor. Ler os 1,7 milhao de negocios das 11h no Python levava 4 s e
    crescia; no SQLite sao 2 s por chamada. Aqui os minutos fechados (todos menos os ultimos MINUTOS_ABERTOS) sao somados
    uma vez e guardados em _CACHE_FITA; cada chamada so le o que veio depois (indice ix_tryd_fita_ts, ver coletor_tryd.py).
    -> (agg [(minuto, comprador, vendedor, agressor, soma_qtd, n)], n, num_min, num_max)."""
    d = _data_br(dia)
    ult = con.execute(f"SELECT ts_ms FROM tryd_fita WHERE ativo=? AND dia=? {_SO_DO_DIA} ORDER BY num DESC LIMIT 1",
                      (ativo, dia, d)).fetchone()
    if not ult:
        return [], 0, None, None
    n, nmin, nmax = con.execute(f"SELECT count(*), min(num), max(num) FROM tryd_fita WHERE ativo=? AND dia=? {_SO_DO_DIA}",
                                (ativo, dia, d)).fetchone()
    q = ("SELECT ts_ms / 60000, comprador, vendedor, agressor, sum(qtd), count(*) FROM tryd_fita WHERE ativo=? AND dia=? "
         f"AND ts_ms >= ? AND ts_ms < ? {_SO_DO_DIA} GROUP BY 1, 2, 3, 4")
    est = _CACHE_FITA.setdefault((ativo, dia), {"upto": 0, "agg": {}})
    alvo = max(est["upto"], ult[0] // 60000 - MINUTOS_ABERTOS)
    if alvo > est["upto"]:
        for m, c, v, ag, qtd, k in con.execute(q, (ativo, dia, est["upto"] * 60000, alvo * 60000, d)):
            acc = est["agg"].setdefault((m, c, v, ag), [0, 0])
            acc[0] += qtd or 0
            acc[1] += k
        est["upto"] = alvo
    agg = [(*k, v[0], v[1]) for k, v in est["agg"].items()]
    agg += list(con.execute(q, (ativo, dia, est["upto"] * 60000, (ult[0] // 60000 + 2) * 60000, d)))
    return agg, n, nmin, nmax


def cobertura_fita(n, nmin, nmax):
    """Parte dos negocios que a fita capturou: `num` anda de 10 em 10, entao (max - min) / 10 + 1 e o total esperado."""
    return n / ((nmax - nmin) // 10 + 1) if n and n > 1 else None


def tabela(ultima, rlp_por_brk, agr_est=None, sem_agr=False):
    """Linhas da tabela por corretora na ultima foto, ordenadas por volume negociado. sem_agr: nem a janela nem a fita
    trazem agressao (acoes sem as colunas Agr.), entao a coluna fica vazia em vez de zero."""
    out = []
    for brk, (q, s, c, v) in ultima.items():
        vol = c + v
        agr = None if sem_agr else s if agr_est is None else agr_est.get(brk, 0)
        out.append({"corretora": brk, "grupo": grupo(brk), "saldo": q, "agr": agr,
                    "volume": vol,
                    "giro": vol / abs(q) if q else None,
                    "rlp": rlp_por_brk.get(brk)})
    out.sort(key=lambda r: -r["volume"])
    return out


def fita_rlp(agg, bins_min=5):
    """agg (fita_agregada) -> serie de RLP. Por faixa de `bins_min` min: volume total, volume de RLP e volume de
    RLP por grupo (da corretora compradora; no RLP a compradora e a vendedora sao a mesma)."""
    if not agg:
        return {"t": [], "pct_rlp": [], "acumulado": {}}, {}
    faixas = sorted({r[0] // bins_min for r in agg})
    pos = {f: i for i, f in enumerate(faixas)}
    vol, vol_rlp = np.zeros(len(faixas)), np.zeros(len(faixas))
    por_grupo = {g: np.zeros(len(faixas)) for g in GRUPOS}
    tot, rlp_b = {}, {}
    for minuto, comp, vend, agr, q, _ in agg:
        i, q = pos[minuto // bins_min], q or 0
        vol[i] += q
        for b in (comp, vend):
            tot[b] = tot.get(b, 0) + q
        if agr == "RLP":
            vol_rlp[i] += q
            por_grupo[grupo(comp)][i] += q
            for b in (comp, vend):
                rlp_b[b] = rlp_b.get(b, 0) + q
    pct = {b: rlp_b.get(b, 0) / v for b, v in tot.items() if v}  # parte do volume da corretora que foi RLP
    t = [f * bins_min * 60 + bins_min * 30 for f in faixas]
    return {"t": t, "pct_rlp": np.where(vol > 0, vol_rlp / np.where(vol > 0, vol, 1), np.nan).tolist(),
            "acumulado": {g: np.cumsum(v).tolist() for g, v in por_grupo.items() if np.any(v)}}, pct


def abertura_ms(ativo):
    """Inicio do pregao (ms desde a meia-noite): contratos 09:00, acoes 10:00. Antes disso nao ha negocio e a janela de
    Saldo do Tryd ainda mostra a tabela do pregao anterior (9/11/2026: PETR4 de sexta gravada das 08:55 em diante, com
    9 das 36 corretoras diferentes da ultima foto de sexta, entao sem_tabela_de_ontem nao a reconhecia)."""
    return (9 if _FUTURO.match(ativo) else 10) * 3_600_000


def sem_tabela_de_ontem(con, ativo, lo, linhas):
    """Tira do inicio do dia as fotos iguais a ultima do dia anterior: antes da abertura das acoes (10:00) a janela de Saldo
    por corretora do Tryd ainda mostra a tabela de ontem, e o coletor a gravava com a hora de hoje (6/11/2026: PETR4 das
    08:55 em diante com o saldo final de 5/11). Nos contratos a tabela ja abre zerada."""
    ant = con.execute("SELECT max(ts_ms) FROM tryd_saldo WHERE ativo=? AND ts_ms<?", (ativo, lo)).fetchone()[0]
    if ant is None or not linhas:
        return linhas
    ref = set(con.execute("SELECT brk, qtd, qtdc, qtdv FROM tryd_saldo WHERE ativo=? AND ts_ms=?", (ativo, ant)))
    i = 0
    while i < len(linhas):
        j = i
        while j < len(linhas) and linhas[j][0] == linhas[i][0]:
            j += 1
        if {(r[1], r[2], r[4], r[5]) for r in linhas[i:j]} != ref:
            break
        i = j
    return linhas[i:]


def desde_o_zeramento(linhas, queda=0.5, ate_h=10.5):
    """Corta as fotos de antes do zeramento da tabela. Ate a abertura o Tryd mostra o saldo do pregao anterior e so zera
    quando o pregao comeca (contratos 09:00, acoes 10:00); se a tabela de ontem nao for identica a ultima foto gravada
    (6/11/2026: WINZ26 e WDOZ26 das 08:55 as 09:00), sem_tabela_de_ontem nao a reconhece. Pior que a serie errada: a
    corretora que estava na tabela de ontem e ainda nao operou hoje ficava com o saldo de ontem o dia todo. Zeramento = a
    soma de qtdc (compras acumuladas) cai abaixo de `queda` x a da foto anterior e continua abaixo na foto seguinte (uma
    leitura incompleta isolada nao conta), antes das `ate_h` horas: em 4/11/2026 a ultima foto (18:29:51) veio quase
    vazia e, sem esse limite, o dia inteiro seria descartado."""
    tempos, tot = [], {}
    for ts, _, _, _, c, _ in linhas:
        if ts not in tot:
            tempos.append(ts)
            tot[ts] = 0.0
        tot[ts] += _num(c) or 0.0
    corte = None
    for i in range(1, len(tempos) - 1):
        if tempos[i] % DIA_MS >= ate_h * 3_600_000:
            break
        antes, agora, depois = tot[tempos[i - 1]], tot[tempos[i]], tot[tempos[i + 1]]
        if antes > 0 and agora < queda * antes and depois < queda * antes:
            corte = tempos[i]
    return linhas if corte is None else [r for r in linhas if r[0] >= corte]


def montar(con, dia, ativo=None):
    """Resposta do /api/players. `dia` = indice do dia (ts_ms // 86400000) ou None para o ultimo com saldo do ativo;
    `ativo` = codigo do Tryd (WINZ26, PETR4...) ou None para o primeiro do seletor (o WIN) no ultimo dia coletado."""
    if dia is None:
        q = "SELECT max(ts_ms) FROM tryd_saldo" + (" WHERE ativo=?" if ativo else "")
        dia = con.execute(q, (ativo,) if ativo else ()).fetchone()[0]
        if dia is None:
            return {"vazio": True, "ativo": ativo, "ativos": []}
        dia //= DIA_MS
    ativos = ativos_do_dia(con, dia)
    if ativo is None:
        if not ativos:
            return {"vazio": True, "ativo": None, "dia": dia, "ativos": []}
        ativo = ativos[0]
    lo, hi = dia * DIA_MS, (dia + 1) * DIA_MS
    linhas = con.execute("SELECT ts_ms, brk, qtd, sagr, qtdc, qtdv FROM tryd_saldo WHERE ativo=? AND ts_ms>=? AND ts_ms<? "
                         "ORDER BY ts_ms", (ativo, lo, hi)).fetchall()
    havia = bool(linhas)
    linhas = [r for r in linhas if r[0] - lo >= abertura_ms(ativo)]
    linhas = sem_tabela_de_ontem(con, ativo, lo, linhas)
    linhas = desde_o_zeramento(linhas)
    if not linhas:
        return {"vazio": True, "ativo": ativo, "dia": dia, "ativos": ativos, "unidade": unidade(ativo),
                **({"motivo": "tabela_de_ontem"} if havia else {})}
    ts_ms, series, ultima, tem_agr = saldo_por_grupo(linhas)
    agg, n_fita, nmin, nmax = fita_agregada(con, ativo, dia)
    rlp, pct = fita_rlp(agg)
    agr_fita, agr_brk = agressao_da_fita(agg) if not tem_agr and agg else (None, None)
    dias = [r[0] for r in con.execute("SELECT DISTINCT ts_ms / ? FROM tryd_saldo WHERE ativo=? ORDER BY 1", (DIA_MS, ativo))]
    return {
        "ativo": ativo, "dia": dia, "dias": dias, "ativos": ativos, "unidade": unidade(ativo),
        "t": [x // 1000 for x in ts_ms], "series": series, "agr_disponivel": tem_agr,
        "foto_inicio": linhas[0][0] // 1000, "foto_fim": ts_ms[-1] // 1000, "fotos": len({r[0] for r in linhas}),
        "rlp": {**rlp, "t": rlp["t"]},
        "agr_fita": agr_fita, "agr_cobertura_fita": cobertura_fita(n_fita, nmin, nmax) if agr_fita else None,
        "corretoras": tabela(ultima, pct, agr_brk if agr_fita else None, sem_agr=not tem_agr and not agg),
        "grupos": {g: DESCRICAO[g] for g in GRUPOS},
        "negocios_fita": n_fita,
    }
