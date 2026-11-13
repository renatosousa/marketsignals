"""Calculo do painel novo (v2): coleta, correlacoes com referencia historica, decomposicao do WIN, fluxo do
estrangeiro, fechamento do dia, vol implicita x realizada e alertas.

ESPEC_DASHBOARD.md secoes 6.1 a 6.6, 6.9 e 6.11; adendo da secao 10 (painel novo em /v2, sem mexer no atual).
O dashboard.py so chama: este modulo nao conhece HTTP e nao grava no banco. As funcoes recebem uma conexao
sqlite somente leitura `con` e devolvem dict/list prontos para JSON (None = valor ausente; o dashboard ja
passa tudo por json_limpo).

Convencoes do projeto: ts_ms = hora de Brasilia codificada como epoch UTC; dia = ts_ms // 86_400_000
(11/11/2026 = 20768); minuto do dia = (ts_ms % 86_400_000) // 60_000; datas em texto 'AAAA-MM-DD'.

Referencia de tempo (a de dashboard.dados): t1 = max(ts_ms) dos snapshots do WIN$; fora do pregao vale a
ultima coleta. Textos que vao para a tela usam acento e virgula decimal; o resto do codigo nao usa acento.
"""
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

import correlacoes as corr
import estimador_tryd as et
import players
from backfill_barras import MT5_ATIVOS
from leiloes import feriados

D = 86_400_000
JANELAS = ("3600", "10800", "pregao")
ALVOS = {"WIN": "WIN$", "WDO": "WDO$"}
INICIO_PREGAO_MIN = 9 * 60            # 09:00: inicio da coleta
FIM_PREGAO_MIN = 18 * 60 + 25        # 18:25: fim do pregao
# ultima barra de 1 min esperada no fim do pregao (ts de abertura, minuto do dia), com folga: a barra final
# vista no banco e WIN$ 18:24 e WDO$ 18:29; o DI encerra as 18:00 e a ultima barra dele varia de 17:53 a 17:59
FIM_BARRAS_MIN = {"WIN$": 18 * 60 + 20, "WDO$": 18 * 60 + 25}
FIM_BARRAS_DI = 17 * 60 + 45
BARRAS_COBRADAS = [s for s in MT5_ATIVOS if s != "DI1F27"]  # o DI jan/27 tem ~40 min com negocio por dia: sem
                                                             # barra no fim do pregao nao e falha de coleta
GRAVIDADE = {"ok": 0, "fora do horario": 0, "atrasado": 1, "sem dados": 2, "parado": 3}
# nomes com acento para os textos de tela; o resto vem de correlacoes.NOMES (sem acento)
NOMES_TELA = {"WIN$": "WIN", "WDO$": "dólar", "DI1F27": "DI jan/27", "DI1F28": "DI jan/28",
              "DI1F29": "DI jan/29", "DI1F33": "DI jan/33", "SPX_USD": "S&P 500 em dólar",
              "NDX_USD": "Nasdaq em dólar"}


# ------------------------------------------------------------------ utilidades

def _t1(con):
    """max(ts_ms) dos snapshots do WIN$ (referencia de tempo do painel) ou None sem dados/tabela."""
    try:
        r = con.execute("SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(r[0]) if r and r[0] is not None else None


def _existe(con, tabela):
    """True se a tabela existe no banco (tabelas ausentes nao sao erro aqui)."""
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (tabela,)).fetchone())
    except sqlite3.OperationalError:
        return False


def _hora(ts_ms, dia=None):
    """'HH:MM' do ts (hora de Brasilia codificada como epoch UTC); 'DD/MM HH:MM' quando e de outro dia."""
    hhmm = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M")
    if dia is not None and ts_ms // D != dia:
        d = date.fromisoformat(corr._data(ts_ms // D))
        return f"{d.day:02d}/{d.month:02d} {hhmm}"
    return hhmm


def _dia_mes(data_iso):
    """'DD/MM' de 'AAAA-MM-DD'."""
    return f"{int(data_iso[8:10]):02d}/{int(data_iso[5:7]):02d}"


def _num_br(v, nd=2):
    """Numero com sinal e virgula decimal, para texto de tela."""
    return f"{v:+.{nd}f}".replace(".", ",")


def _eh_pregao(d):
    """True em dia util (seg-sex) que nao e feriado nacional."""
    return d.weekday() < 5 and d not in feriados(d.year)


def _pregao_anterior(d, n=1):
    """O n-esimo pregao util anterior a datetime.date d."""
    while n > 0:
        d -= timedelta(days=1)
        n -= _eh_pregao(d)
    return d


def janela_minutos(t1, janela):
    """(min_ini, min_fim) da janela em minutos do dia: "3600", "10800" ou "pregao" (desde 09:05)."""
    if janela not in JANELAS:
        raise ValueError(f"janela invalida ({' ou '.join(JANELAS)})")
    min_fim = (int(t1) % D) // 60_000
    if janela == "pregao":
        return corr.SESSAO_FUTUROS[0], min_fim
    return max(min_fim - int(janela) // 60, 0), min_fim


class _SeriesDia:
    """Cache das series e retornos de 1 min do dia (ao_vivo=True) dentro de um pedido; o resumo reaproveita
    as mesmas series entre a decomposicao e os pares principais."""

    def __init__(self, con, dia):
        self.con, self.dia = con, dia
        self._serie, self._ret = {}, {}

    def serie(self, s):
        if s not in self._serie:
            self._serie[s] = (corr.serie_1m(self.con, s, self.dia, ao_vivo=True)
                              if self.dia is not None else pd.Series(dtype=float))
        return self._serie[s]

    def retornos(self, s, min_ini=None, min_fim=None):
        """Retornos do dia, cortados ao intervalo [min_ini, min_fim] quando dado."""
        if s not in self._ret:
            self._ret[s] = corr.retornos(self.serie(s), s)
        r = self._ret[s]
        if min_ini is not None:
            r = r[(r.index >= min_ini) & (r.index <= min_fim)]
        return r

    def trecho(self, s, min_ini, min_fim):
        """Serie de s dentro da janela, sem os NaN."""
        x = self.serie(s)
        return x[(x.index >= min_ini) & (x.index <= min_fim)].dropna()


# ------------------------------------------------------------------ correlacoes (2, 3)

def _principais(con, series, data, min_ini, min_fim):
    """Os 4 pares de correlacoes.PRINCIPAIS na janela: {"y", "x", "nome", "corr", "n", "ref", "z", "alerta"}
    (alerta = |z| >= 2 com n >= 120, regra R10/6.11)."""
    out = []
    for py, px in corr.PRINCIPAIS:
        p = corr.par(series.retornos(py, min_ini, min_fim), series.retornos(px, min_ini, min_fim))
        ref = corr.referencia(con, py, px, data) if data else None
        z = corr.z(p["corr"], ref)
        out.append({"y": py, "x": px, "nome": corr.NOMES.get(px, px), "corr": p["corr"], "n": p["n"],
                    "ref": ref, "z": z, "alerta": z is not None and abs(z) >= 2 and p["n"] >= 120})
    return out


def _matriz(con, series, data, min_ini, min_fim):
    """Matriz NxN da janela (None na diagonal e com n < 60) e a diferenca contra o normal (hoje - media da
    referencia; None sem referencia)."""
    sims = corr.MATRIZ
    n = len(sims)
    hoje = [[None] * n for _ in range(n)]
    dif = [[None] * n for _ in range(n)]
    for i in range(n):
        ri = series.retornos(sims[i], min_ini, min_fim)
        for j in range(i + 1, n):
            p = corr.par(ri, series.retornos(sims[j], min_ini, min_fim))
            ref = corr.referencia(con, sims[i], sims[j], data) if data else None
            hoje[i][j] = hoje[j][i] = p["corr"]
            if p["corr"] is not None and ref is not None:
                dif[i][j] = dif[j][i] = p["corr"] - ref["media"]
    return {"simbolos": sims, "nomes": [corr.NOMES.get(s, s) for s in sims], "hoje": hoje, "dif": dif}


def correlacoes_api(con, janela="3600", alvo="WIN"):
    """Painel de correlacoes (ESPEC 6.3 a 6.5) na janela ("3600", "10800" ou "pregao") para o alvo
    ("WIN" ou "WDO"): tabela com referencia historica e beta em pontos, pares principais, correlacao movel
    (janela de 60 min sobre o pregao inteiro), matriz e decomposicao do WIN."""
    if alvo not in ALVOS:
        raise ValueError("alvo invalido (WIN ou WDO)")
    if janela not in JANELAS:
        raise ValueError(f"janela invalida ({' ou '.join(JANELAS)})")
    y = ALVOS[alvo]
    t1 = _t1(con)
    dia = t1 // D if t1 is not None else None
    data = corr._data(dia) if dia is not None else None
    series = _SeriesDia(con, dia)
    min_ini, min_fim = janela_minutos(t1, janela) if t1 is not None else (0, 0)

    if t1 is not None:
        trecho_alvo = series.trecho(y, min_ini, min_fim)
        preco_fim = float(trecho_alvo.iloc[-1]) if len(trecho_alvo) else None
    else:
        preco_fim = None

    tabela = []
    for x in (corr.PARES_WIN if alvo == "WIN" else corr.PARES_WDO):
        p = corr.par(series.retornos(y, min_ini, min_fim), series.retornos(x, min_ini, min_fim))
        ref = corr.referencia(con, y, x, data) if data else None
        z = corr.z(p["corr"], ref)
        linha = {"x": x, "nome": corr.NOMES.get(x, x), **p, "ref": ref, "z": z}
        if p["corr"] is None:
            linha["motivo"] = f"amostra curta, n = {p['n']}"
        linha["beta_pts"] = (p["beta"] * preco_fim if corr.eh_taxa(x) else p["beta"] * preco_fim * 0.01) \
            if p["beta"] is not None and preco_fim is not None else None
        tabela.append(linha)
    tabela.sort(key=lambda l: (l["z"] is None, -abs(l["z"] or 0)))   # |z| decrescente; sem z no fim

    movel = []
    for py, px in corr.PRINCIPAIS:
        ref = corr.referencia(con, py, px, data) if data else None
        m = corr.movel(series.retornos(py), series.retornos(px)) if dia is not None \
            else pd.Series(dtype=float)
        movel.append({"par": f"{py}|{px}",
                      "t": [dia * 86400 + int(i) * 60 for i in m.index],
                      "corr": [float(v) if v == v else None for v in m.to_numpy()],
                      "p10": ref["p10"] if ref else None, "p90": ref["p90"] if ref else None})

    return {"janela": janela, "alvo": alvo,
            "t0": dia * 86400 + min_ini * 60 if dia is not None else None,
            "t1": t1 / 1000 if t1 is not None else None,
            "n_minutos": min_fim - min_ini,
            "tabela": tabela, "principais": _principais(con, series, data, min_ini, min_fim),
            "movel": movel, "matriz": _matriz(con, series, data, min_ini, min_fim),
            "decomposicao": decomposicao(con, dia, janela, _series=series)}


def decomposicao(con, dia, janela, _series=None):
    """Decomposicao do retorno do WIN na janela (ESPEC 6.3): betas de fatores_dia (linhas de alvo='WIN$' com
    a maior data <= a do dia), contribuicao de cada fator e a parte propria, em % e pontos.
    -> {"total_pct", "total_pts", "fatores", "propria_pct", "propria_pts", "r2_hoje", "n", "betas_data",
    "r2_treino", "data", "janela", "texto"}; {"motivo": "sem betas: rode correlacoes.py atualizar"} sem
    betas; None em dia sem preco do WIN na janela."""
    if dia is None:
        return None
    dia = corr._dia(dia)
    data = corr._data(dia)
    try:
        linhas = con.execute(
            "SELECT data, fator, beta, r2, n FROM fatores_dia WHERE alvo='WIN$' AND data="
            "(SELECT max(data) FROM fatores_dia WHERE alvo='WIN$' AND data<=?) ORDER BY fator",
            (data,)).fetchall()
    except sqlite3.OperationalError:
        linhas = []
    if not linhas:
        return {"motivo": "sem betas: rode correlacoes.py atualizar"}
    series = _series or _SeriesDia(con, dia)
    t1 = _t1(con)
    if t1 is not None and t1 // D == dia:      # dia em curso: janela ate o ultimo dado
        min_ini, min_fim = janela_minutos(t1, janela)
    else:                                      # dia fechado: janela ate o fim da sessao dos futuros
        min_ini, min_fim = janela_minutos(dia * D + corr.SESSAO_FUTUROS[1] * 60_000, janela)
    trecho = series.trecho("WIN$", min_ini, min_fim)
    if not len(trecho):
        return None
    betas = {f: b for _, f, b, _, _ in linhas}
    rets = {s: series.retornos(s, min_ini, min_fim) for s in ["WIN$"] + list(betas)}
    d = corr.decompor(rets, betas, float(trecho.iloc[0]))
    d["betas_data"] = linhas[0][0]
    d["r2_treino"] = linhas[0][3]
    d["data"] = data
    d["janela"] = janela
    if d["n"] < corr.N_MIN:
        d["texto"] = "amostra curta"
    elif d["r2_hoje"] is None or d["r2_hoje"] < 0.05:
        d["texto"] = "o normal não explica hoje"
    else:
        d["texto"] = f"o normal explica {d['r2_hoje'] * 100:.0f}% do minuto a minuto de hoje"
    return d


# ------------------------------------------------------------------ fluxo (4)

def _corr_baldes(a_min, b_min, div_min, minimo):
    """Correlacao entre a diferenca de a e o log-retorno de b em baldes de div_min (ultimo valor do balde,
    ffill no trecho comum as duas series). -> (corr, n de baldes usados); (None, n) abaixo do minimo."""
    if not a_min or not b_min:
        return None, 0
    a = {m // div_min: v for m, v in a_min.items()}
    b = {m // div_min: v for m, v in b_min.items()}
    b0, b1 = max(min(a), min(b)), min(max(a), max(b))
    if b1 - b0 + 1 < minimo:
        return None, b1 - b0 + 1
    va, vb, xa, xb = [], [], None, None
    for k in range(b0, b1 + 1):
        xa, xb = a.get(k, xa), b.get(k, xb)
        va.append(xa)
        vb.append(xb)
    da = np.diff(np.array(va, dtype=float))
    rb = np.diff(np.log(np.array(vb, dtype=float)))
    ok = ~np.isnan(da) & ~np.isnan(rb)
    if ok.sum() < 2 or np.std(da[ok]) == 0 or np.std(rb[ok]) == 0:
        return None, int(ok.sum())
    return float(np.corrcoef(da[ok], rb[ok])[0, 1]), int(ok.sum())


def _cesta_indice(con, dia, hoje):
    """Cesta acumulada x variacao % do IND$ na grade de 1 min das 10:00 as 16:55 (ESPEC 6.8) e a correlacao
    de 5 min da diferenca da cesta com o log-retorno do indice (minimo de 24 baldes)."""
    lo, hi = 10 * 60, 16 * 60 + 55
    cesta = {(t // 60) % 1440: v for t, v in zip(hoje["cesta"]["t"], hoje["cesta"]["acumulado_mi"])}
    ind = {}
    try:
        for ts, mid in con.execute("SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol='IND$' "
                                   "AND ts_ms>=? AND ts_ms<?",
                                   (dia * D + lo * 60_000, dia * D + (hi + 1) * 60_000)):
            if mid is not None:
                ind[(ts % D) // 60_000] = mid
    except sqlite3.OperationalError:
        pass
    cesta_mi, ind_pct, atual_c, atual_i = [], [], None, None
    base = ind[min(ind)] if ind else None
    for m in range(lo, hi + 1):
        if m in cesta:
            atual_c = cesta[m]
        if m in ind:
            atual_i = ind[m]
        cesta_mi.append(atual_c)
        ind_pct.append((atual_i / base - 1) * 100 if atual_i is not None and base else None)
    corr_5m, n = _corr_baldes(cesta, ind, 5, et.MIN_BALDES_5M)
    return {"t": [dia * 86400 + m * 60 for m in range(lo, hi + 1)], "cesta_mi": cesta_mi,
            "ind_pct": ind_pct, "corr_5m": corr_5m, "n": n}


def fluxo_api(con, dia=None, ativo=None):
    """Painel do fluxo do estrangeiro (ESPEC 6.6 a 6.8): placar contra o oficial da B3, estimativa do dia
    (estimador do Tryd + cesta de agressao), mapa grupo x ativo, quem acompanha o preco e cesta x indice.
    dia = indice do dia ou 'AAAA-MM-DD'; None = ultimo dia de tryd_saldo."""
    if dia is None:
        r = con.execute("SELECT max(ts_ms) FROM tryd_saldo").fetchone()
        dia = int(r[0]) // D if r and r[0] is not None else None
    else:
        dia = corr._dia(dia)
    vazio_hoje = {"por_ativo": [], "tryd_mi": 0.0, "cesta": {"mi": 0.0, "t": [], "acumulado_mi": []}}
    vazio_cesta = {"t": [], "cesta_mi": [], "ind_pct": [], "corr_5m": None, "n": 0}
    if dia is None:
        return {"dia": None, "data": None, "placar": et.placar(con), "hoje": vazio_hoje,
                "mapa": {"ativos": [], "grupos": list(players.GRUPOS),
                         "valores": {g: {} for g in players.GRUPOS}},
                "ativos": [], "ativo": None, "acompanha": [], "cesta_indice": vazio_cesta}
    data = corr._data(dia)
    ativos = players.ativos_do_dia(con, dia)
    ativo = ativo or (ativos[0] if ativos else None)
    hoje = et.hoje(con, dia)
    return {"dia": dia, "data": data, "placar": et.placar(con), "hoje": hoje,
            "mapa": et.mapa_grupos(con, dia), "ativos": ativos, "ativo": ativo,
            "acompanha": et.acompanha_preco(con, dia, ativo) if ativo else [],
            "cesta_indice": _cesta_indice(con, dia, hoje)}


# ------------------------------------------------------------------ coleta (5)

def _max_ts(con, sql, *params):
    """max(ts_ms) de uma consulta, ou None (tabela ausente ou vazia nao sao erro aqui)."""
    try:
        r = con.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else None
    except sqlite3.OperationalError:
        return None


def _max_ts_opcoes(con):
    """max(ts_ms) de opcoes_resumo: o indice e (subjacente, ts_ms), entao o max global so sai rapido por
    subjacente (a consulta sem filtro varre 500 mil linhas, ~200 ms)."""
    try:
        subs = [r[0] for r in con.execute("SELECT DISTINCT subjacente FROM opcoes_resumo")]
    except sqlite3.OperationalError:
        return None
    ts = None
    for s in subs:
        r = _max_ts(con, "SELECT max(ts_ms) FROM opcoes_resumo WHERE subjacente=?", s)
        if r is not None:
            ts = r if ts is None else max(ts, r)
    return ts


def _max_ts_fita(con):
    """max(ts_ms) de tryd_fita (42 milhoes de linhas). O indice (dia, ativo, ts_ms) responde na hora a
    max(dia) e a max(ts_ms) por (dia, ativo), mas nao a max(ts_ms) global (~2 s); como ts_ms codifica o dia,
    o max global esta no max(dia), entao basta varrer os ativos daquele dia."""
    try:
        r = con.execute("SELECT max(dia) FROM tryd_fita").fetchone()
    except sqlite3.OperationalError:
        return None
    dia = r[0] if r else None
    if dia is None:
        return None
    try:
        ativos = [x[0] for x in con.execute(
            "SELECT DISTINCT ativo FROM tryd_saldo WHERE ts_ms>=? AND ts_ms<?",
            (int(dia) * D, (int(dia) + 1) * D))]
    except sqlite3.OperationalError:
        ativos = []
    if not ativos:      # dia sem saldo: descobre os ativos pela propria fita (169 ms no banco cheio)
        ativos = [x[0] for x in con.execute("SELECT DISTINCT ativo FROM tryd_fita WHERE dia=?", (int(dia),))]
    ts = None
    for a in ativos:
        t = _max_ts(con, "SELECT max(ts_ms) FROM tryd_fita WHERE dia=? AND ativo=?", int(dia), a)
        if t is not None:
            ts = t if ts is None else max(ts, t)
    return ts


def _fonte(nome, ultimo, idade_s, estado, detalhe=None):
    return {"nome": nome, "ultimo": ultimo, "idade_s": idade_s, "estado": estado, "detalhe": detalhe}


def coleta(con, agora_ms):
    """Estado das fontes de dados (ESPEC 6.1): uma entrada por fonte com o ultimo dado e o estado
    ("ok", "atrasado", "parado", "fora do horario" ou "sem dados"). Pregao = dia util (sem os feriados de
    leiloes.feriados) das 09:00 as 18:25; cada fonte so cobra dentro do seu horario; fonte parada = sem
    leitura a mais de 6x o limite de atraso (o WIN: 6 x 30 s = 180 s). -> {"fontes": [...], "em_dia": N,
    "total": M, "pior": "ok"|"atrasado"|"parado"}."""
    from dashboard import parado_ha      # import local: o dashboard importa este modulo
    agora_ms = int(agora_ms)
    dia = agora_ms // D
    d_hoje = date.fromisoformat(corr._data(dia))
    min_agora = (agora_ms % D) // 60_000
    pregao = _eh_pregao(d_hoje) and INICIO_PREGAO_MIN <= min_agora <= FIM_PREGAO_MIN
    d1 = _pregao_anterior(d_hoje, 1).isoformat()      # pregao util anterior
    d2 = _pregao_anterior(d_hoje, 2).isoformat()      # dois pregoes uteis atras
    dentro = lambda h0, h1: pregao and h0 <= min_agora <= h1

    def por_idade(nome, ts, h0, h1, atraso_s):
        """Estado de uma fonte por idade do ultimo ts; None = sem dados; fora do horario = sem cobranca."""
        if ts is None:
            return _fonte(nome, None, None, "sem dados", "sem dados na tabela")
        idade = (agora_ms - ts) / 1000
        if not dentro(h0, h1):
            return _fonte(nome, _hora(ts, dia), idade, "fora do horario")
        estado = "parado" if idade > 6 * atraso_s else "atrasado" if idade > atraso_s else "ok"
        detalhe = None
        if estado == "parado":
            detalhe = f"sem leitura há {idade / 60:.0f} min"
        elif estado == "atrasado":
            detalhe = f"atraso de {idade:.0f} s"
        return _fonte(nome, _hora(ts, dia), idade, estado, detalhe)

    def por_data(nome, sql, h0, h1, criterio, detalhe_atraso):
        """Estado de uma fonte diaria: atrasada quando a data mais recente viola o criterio (d1/d2/n dias)."""
        try:
            r = con.execute(sql).fetchone()
        except sqlite3.OperationalError:
            return _fonte(nome, None, None, "sem dados", "sem dados na tabela")
        dt = r[0] if r else None
        if dt is None:
            return _fonte(nome, None, None, "sem dados", "sem dados na tabela")
        idade = (agora_ms - corr._dia(dt) * D) / 1000
        ultimo = _dia_mes(dt)
        if not dentro(h0, h1):
            return _fonte(nome, ultimo, idade, "fora do horario")
        atrasada = criterio(dt)
        return _fonte(nome, ultimo, idade, "atrasado" if atrasada else "ok",
                     detalhe_atraso(dt) if atrasada else None)

    fontes = []

    # WIN (livro): preco congelado no pregao = coleta parada mesmo com snapshot novo (conexao.py, lacunas.py)
    ts = _max_ts(con, "SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'")
    f = por_idade("WIN (livro)", ts, INICIO_PREGAO_MIN, FIM_PREGAO_MIN, 30)
    if f["estado"] == "ok":
        try:
            congelado = parado_ha(con, "WIN$")
        except sqlite3.OperationalError:
            congelado = None
        if congelado is not None and congelado >= 180:
            f = _fonte(f["nome"], f["ultimo"], f["idade_s"], "parado",
                       f"preço congelado há {congelado:.0f} s")
    fontes.append(f)

    fontes.append(por_idade("BOVA11 (livro)", _max_ts(con, "SELECT max(ts_ms) FROM snapshots "
                                                     "WHERE symbol='BOVA11'"), 10 * 60, 17 * 60, 30))
    fontes.append(por_idade("Macro", _max_ts(con, "SELECT max(ts_ms) FROM macro_cotacoes "
                                                  "WHERE symbol='WDO$'"), INICIO_PREGAO_MIN, FIM_PREGAO_MIN, 30))

    # Opcoes: por vencimento; a idade da fonte e a pior entre os vencimentos com leitura no dia
    ts_por_venc = {}
    try:
        ts_por_venc = dict(con.execute("SELECT vencimento, max(ts_ms) FROM opcoes_resumo "
                                       "WHERE ts_ms>=? AND ts_ms<? GROUP BY vencimento",
                                       (dia * D, (dia + 1) * D)).fetchall())
    except sqlite3.OperationalError:
        pass
    ts_ult = max(ts_por_venc.values()) if ts_por_venc else _max_ts_opcoes(con)
    if ts_ult is None:
        fontes.append(_fonte("Opções mensais e semanais", None, None, "sem dados", "sem dados na tabela"))
    else:
        idades = {v: (agora_ms - t) / 1000 for v, t in ts_por_venc.items()} or {"": (agora_ms - ts_ult) / 1000}
        pior_venc, pior_idade = max(idades.items(), key=lambda kv: kv[1])
        if not dentro(10 * 60, 17 * 60):
            fontes.append(_fonte("Opções mensais e semanais", _hora(ts_ult, dia), pior_idade, "fora do horario"))
        else:
            estado = "parado" if pior_idade > 6 * 180 else "atrasado" if pior_idade > 180 else "ok"
            detalhe = None
            if estado != "ok":
                detalhe = (f"vencimento {pior_venc} sem leitura há {pior_idade / 60:.0f} min" if pior_venc
                           else f"sem leitura há {pior_idade / 60:.0f} min")
            fontes.append(_fonte("Opções mensais e semanais", _hora(ts_ult, dia), pior_idade, estado, detalhe))

    fontes.append(por_idade("Agressão da cesta", _max_ts(con, "SELECT max(ts_ms) FROM agressao_1min"),
                            10 * 60, 16 * 60 + 55, 180))

    # Tryd: o pior entre o saldo e a fita
    ts_sal = _max_ts(con, "SELECT max(ts_ms) FROM tryd_saldo")
    ts_fit = _max_ts_fita(con)
    if ts_sal is None and ts_fit is None:
        fontes.append(_fonte("Tryd saldo e fita", None, None, "sem dados", "sem dados na tabela"))
    else:
        ts_tryd = max(t for t in (ts_sal, ts_fit) if t is not None)
        f = por_idade("Tryd saldo e fita", ts_tryd, INICIO_PREGAO_MIN, FIM_PREGAO_MIN, 180)
        if f["estado"] != "ok" and ts_sal is not None and ts_fit is not None:
            pior = "fita" if (agora_ms - ts_fit) > (agora_ms - ts_sal) else "saldo"
            detalhe = (f["detalhe"] or "atrasado") + f" ({pior})"
            f = _fonte(f["nome"], f["ultimo"], f["idade_s"], f["estado"], detalhe)
        fontes.append(f)

    fontes.append(por_idade("Regime", _max_ts(con, "SELECT max(ts_ms) FROM regime_live"),
                            INICIO_PREGAO_MIN, FIM_PREGAO_MIN, 180))
    fontes.append(por_idade("VIX", _max_ts(con, "SELECT max(ts_ms) FROM macro_cotacoes "
                                                "WHERE symbol='IB:VIX'"), INICIO_PREGAO_MIN, FIM_PREGAO_MIN,
                            1800))
    fontes.append(por_data("Fluxo oficial B3", "SELECT max(data_ref) FROM fluxo_diario",
                           10 * 60, FIM_PREGAO_MIN, lambda dt: dt < d2,
                           lambda dt: f"esperava data de referência a partir de {d2}"))
    fontes.append(por_data("Posição em aberto B3", "SELECT max(sessao) FROM b3_posicoes_resumo",
                           10 * 60, FIM_PREGAO_MIN, lambda dt: dt < d1,
                           lambda dt: f"esperava sessão a partir de {d1}"))
    fontes.append(por_data("ANBIMA", "SELECT max(data) FROM anbima_ettj",
                           INICIO_PREGAO_MIN, FIM_PREGAO_MIN, lambda dt: dt < d1,
                           lambda dt: f"esperava data a partir de {d1}"))
    fontes.append(por_data("Leilões do Tesouro", "SELECT max(data) FROM leilao_resultados",
                           INICIO_PREGAO_MIN, FIM_PREGAO_MIN,
                           lambda dt: (d_hoje - date.fromisoformat(dt)).days > 7,
                           lambda dt: f"último leilão há {(d_hoje - date.fromisoformat(dt)).days} dias corridos"))
    fontes.append(por_data("Focus", "SELECT max(data) FROM focus_ipca",
                           INICIO_PREGAO_MIN, FIM_PREGAO_MIN,
                           lambda dt: (d_hoje - date.fromisoformat(dt)).days > 10,
                           lambda dt: f"última atualização há {(d_hoje - date.fromisoformat(dt)).days} dias corridos"))

    # Barras de 1 min: cada simbolo de backfill_barras.MT5_ATIVOS tem que chegar ao fim do pregao anterior
    if not _existe(con, "barras_1m"):
        fontes.append(_fonte("Barras de 1 min", None, None, "sem dados", "sem dados na tabela"))
    else:
        dia_d1 = _pregao_anterior(d_hoje).toordinal() - date(1970, 1, 1).toordinal()
        faltando, ts_bar = [], None
        for s in BARRAS_COBRADAS:
            r = con.execute("SELECT max(ts_ms % ?) FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<?",
                            (D, s, dia_d1 * D, (dia_d1 + 1) * D)).fetchone()
            fim = FIM_BARRAS_DI if s.startswith("DI1") else FIM_BARRAS_MIN.get(s, FIM_BARRAS_DI)
            if r is None or r[0] is None or r[0] < fim * 60_000:
                faltando.append(s)
            # max por simbolo usa a chave primaria (symbol, ts_ms); o max global varre 2,5 milhoes (~90 ms)
            t = _max_ts(con, "SELECT max(ts_ms) FROM barras_1m WHERE symbol=?", s)
            if t is not None:
                ts_bar = t if ts_bar is None else max(ts_bar, t)
        if ts_bar is None:
            ts_bar = _max_ts(con, "SELECT max(ts_ms) FROM barras_1m")
        if ts_bar is None:
            fontes.append(_fonte("Barras de 1 min", None, None, "sem dados", "sem dados na tabela"))
        elif not dentro(INICIO_PREGAO_MIN, FIM_PREGAO_MIN):
            fontes.append(_fonte("Barras de 1 min", _dia_mes(corr._data(ts_bar // D)), None, "fora do horario"))
        else:
            fontes.append(_fonte("Barras de 1 min", _dia_mes(corr._data(ts_bar // D)),
                                 (agora_ms - ts_bar) / 1000, "atrasado" if faltando else "ok",
                                 f"sem barras de {', '.join(faltando)} até o fim do pregão anterior"
                                 if faltando else None))

    pior = max(GRAVIDADE[f["estado"]] for f in fontes)
    return {"fontes": fontes, "em_dia": sum(f["estado"] in ("ok", "fora do horario") for f in fontes),
            "total": len(fontes),
            "pior": "parado" if pior >= 3 else "atrasado" if pior >= 1 else "ok"}


# ------------------------------------------------------------------ vol (6)

def _implicita_dia(con, dia):
    """(atm_iv x 100 do vencimento mais proximo com t_anos >= 5/252 na ultima leitura do dia, vencimento)
    em opcoes_resumo (mesma regra de grafico_quant.implicito; cada vencimento tem sua ultima leitura)."""
    try:
        rows = con.execute("SELECT vencimento, t_anos, atm_iv, max(ts_ms) FROM opcoes_resumo "
                           "WHERE subjacente='BOVA11' AND ts_ms>=? AND ts_ms<? AND atm_iv IS NOT NULL "
                           "GROUP BY vencimento", (dia * D, (dia + 1) * D)).fetchall()
    except sqlite3.OperationalError:
        return None, None
    rows = sorted(r for r in rows if r[1] is not None and r[1] >= 5 / 252)
    if not rows:
        return None, None
    venc, _t, iv, _ts = rows[0]
    return (iv * 100) if iv is not None else None, venc


def _ultima_linha_regime(con, dia):
    """Ultima linha de regime_live do dia: {"rotulo", "rv_rel", "rv_dia_real", "rv_dia_prev"} ou None."""
    try:
        r = con.execute("SELECT rotulo, rv_rel, rv_dia_real, rv_dia_prev FROM regime_live "
                        "WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms DESC LIMIT 1",
                        (dia * D, (dia + 1) * D)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(zip(("rotulo", "rv_rel", "rv_dia_real", "rv_dia_prev"), r)) if r else None


def _pregoes_regime(con, n=30):
    """Os ultimos `n` dias com linha em regime_live, do mais antigo para o mais novo."""
    try:
        return sorted(int(r[0]) for r in con.execute(
            "SELECT DISTINCT ts_ms / ? FROM regime_live ORDER BY 1 DESC LIMIT ?", (D, int(n))))
    except sqlite3.OperationalError:
        return []


def vol_cartao(con, dia):
    """Vol implicita x realizada (ESPEC 6.9) do dia: implicita = atm_iv x 100 do vencimento mais proximo com
    t_anos >= 5/252 na ultima leitura; realizada = rv_dia_real x sqrt(252) e prevista = rv_dia_prev x
    sqrt(252) da ultima linha de regime_live; diferenca em pontos de vol; serie dos ultimos 30 pregoes
    (um ponto por dia, mesma regra) e correlacao vol x preco em baldes de 5 min (minimo de 40)."""
    dia = corr._dia(dia)
    implicita, venc = _implicita_dia(con, dia)
    linha = _ultima_linha_regime(con, dia)
    anual = lambda v: v * math.sqrt(252) if v is not None else None
    realizada = anual(linha["rv_dia_real"]) if linha else None
    prevista = anual(linha["rv_dia_prev"]) if linha else None
    serie = {"datas": [], "implicita": [], "realizada": [], "prevista": []}
    for d in _pregoes_regime(con, 30):
        iv, _ = _implicita_dia(con, d)
        l = _ultima_linha_regime(con, d)
        serie["datas"].append(corr._data(d))
        serie["implicita"].append(iv)
        serie["realizada"].append(anual(l["rv_dia_real"]) if l else None)
        serie["prevista"].append(anual(l["rv_dia_prev"]) if l else None)
    vol_preco = None
    if venc:
        iv_min, spot_min = {}, {}
        try:
            for ts, iv, spot in con.execute("SELECT ts_ms, atm_iv, spot FROM opcoes_resumo "
                                            "WHERE subjacente='BOVA11' AND vencimento=? AND ts_ms>=? AND ts_ms<?",
                                            (venc, dia * D, (dia + 1) * D)):
                if iv is not None and spot is not None:
                    m = (ts % D) // 60_000
                    iv_min[m], spot_min[m] = iv, spot
        except sqlite3.OperationalError:
            pass
        vol_preco = _corr_baldes(iv_min, spot_min, 5, 40)[0]
    return {"data": corr._data(dia), "vencimento": venc, "implicita": implicita, "realizada": realizada,
            "prevista": prevista,
            "dif": implicita - realizada if implicita is not None and realizada is not None else None,
            "serie": serie, "vol_preco": vol_preco}


# ------------------------------------------------------------------ alertas (7) e resumo (8)

def _texto_descolamento(p):
    """Texto do alerta de descolamento (ESPEC 6.11): "WIN x dolar em -0,15; o normal e -0,47 (z +2,9)",
    com os nomes de NOMES_TELA, virgula decimal e o sinal de vezes da tela."""
    nome_y = NOMES_TELA.get(p["y"], corr.NOMES.get(p["y"], p["y"]))
    nome_x = NOMES_TELA.get(p["x"], corr.NOMES.get(p["x"], p["x"]))
    return (f"{nome_y} × {nome_x} em {_num_br(p['corr'])}; "
            f"o normal é {_num_br(p['ref']['media'])} (z {_num_br(p['z'], 1)})")


def alertas(coleta_, regime_atual, principais):
    """Lista unica de alertas do resumo (ESPEC 6.11), do mais grave para o menos: fontes paradas (critico),
    fontes atrasadas (atencao), alertas do regime (regime_atual["alertas"]: dicts {"nivel", "texto"} ou
    textos; "serio" vira atencao) e descolamentos dos pares principais (info). Sem alerta: uma linha
    "Sem alertas"."""
    out = []
    for f in coleta_.get("fontes", []):
        if f["estado"] == "parado":
            out.append({"nivel": "critico", "texto": f"Coleta parada: {f['nome']}"
                        + (f" ({f['detalhe']})" if f.get("detalhe") else "")})
    for f in coleta_.get("fontes", []):
        if f["estado"] == "atrasado":
            out.append({"nivel": "atencao", "texto": f"Coleta atrasada: {f['nome']}"
                        + (f" ({f['detalhe']})" if f.get("detalhe") else "")})
    for a in ((regime_atual or {}).get("alertas") or []):
        texto = a.get("texto") if isinstance(a, dict) else a
        if not texto:
            continue
        nivel = a.get("nivel") if isinstance(a, dict) else None
        out.append({"nivel": "atencao" if nivel == "serio" else "info", "texto": texto})
    for p in principais:
        if p.get("alerta") and p.get("corr") is not None and p.get("ref") and p.get("z") is not None:
            out.append({"nivel": "info", "texto": _texto_descolamento(p)})
    if not out:
        out.append({"nivel": "info", "texto": "Sem alertas"})
    return out


def resumo_api(con, agora_ms):
    """Resumo do painel novo (ESPEC 5.3 e 6.1/6.3/6.4/6.9/6.11): coleta, regime, decomposicao na janela
    pregao, pares principais, vol implicita x realizada e alertas. Orcamento: 0,5 s com cache frio
    (as series da decomposicao e dos pares principais sao as mesmas)."""
    from dashboard import regime as regime_dash    # import local: o dashboard importa este modulo
    agora_ms = int(agora_ms)
    t1 = _t1(con)
    coleta_ = coleta(con, agora_ms)
    reg = regime_dash(con, t1) if t1 is not None else None
    dia = t1 // D if t1 is not None else None
    data = corr._data(dia) if dia is not None else None
    if t1 is not None:
        min_ini, min_fim = janela_minutos(t1, "pregao")
        series = _SeriesDia(con, dia)
        dec = decomposicao(con, dia, "pregao", _series=series)
        principais = _principais(con, series, data, min_ini, min_fim)
        vol = vol_cartao(con, dia)
    else:
        dec, principais, vol = None, [], None
    return {"coleta": coleta_, "regime": reg, "decomposicao": dec, "principais": principais,
            "vol": vol, "alertas": alertas(coleta_, reg and reg["atual"], principais),
            "t1": t1 / 1000 if t1 is not None else None}


# ------------------------------------------------------------------ fechamento (9)

FAIXAS_WIN = (("09-10", 9 * 60, 10 * 60), ("10-13", 10 * 60, 13 * 60),
              ("13-16", 13 * 60, 16 * 60), ("16-fim", 16 * 60, None))


def _precos_dia(con, simbolo, dia):
    """Precos de `simbolo` no dia: barras de 1 min quando o simbolo tiver barras (abertura = open da
    primeira, minima/maxima das barras, ultimo = close da ultima), senao mids (snapshots de WIN$ e BOVA11
    com spread > 0; macro_cotacoes para os demais). -> {"por_min", "abertura", "minima", "maxima",
    "ultimo"} ou None sem dados no dia."""
    lo, hi = dia * D, (dia + 1) * D
    if _existe(con, "barras_1m"):
        rows = con.execute("SELECT ts_ms, open, high, low, close FROM barras_1m WHERE symbol=? AND ts_ms>=? "
                           "AND ts_ms<? AND close IS NOT NULL ORDER BY ts_ms", (simbolo, lo, hi)).fetchall()
        if rows:
            return {"por_min": {ts % D // 60_000: c for ts, _o, _h, _l, c in rows},
                    "abertura": rows[0][1],
                    "minima": min((r[3] for r in rows if r[3] is not None), default=None),
                    "maxima": max((r[2] for r in rows if r[2] is not None), default=None),
                    "ultimo": rows[-1][4]}
    sql = ("SELECT ts_ms, mid FROM snapshots WHERE symbol=? AND spread>0 AND ts_ms>=? AND ts_ms<?"
           if simbolo in ("WIN$", "BOVA11") else
           "SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<?")
    try:
        rows = [(ts, m) for ts, m in con.execute(sql, (simbolo, lo, hi)) if m is not None]
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    return {"por_min": {ts % D // 60_000: m for ts, m in rows}, "abertura": rows[0][1],
            "minima": min(m for _, m in rows), "maxima": max(m for _, m in rows), "ultimo": rows[-1][1]}


def _anterior(con, simbolo, dia, fn):
    """Valor de `fn(precos do dia)` no pregao anterior com dados (procura ate 15 dias atras) ou None."""
    for _ in range(15):
        dia -= 1
        p = _precos_dia(con, simbolo, dia)
        if p:
            return fn(p)
    return None


def _ohlc(con, simbolo, dia):
    """{"abertura", "minima", "maxima", "ultimo", "anterior", "var_pct"} do simbolo no dia; o anterior e o
    ultimo do pregao anterior pela mesma regra."""
    p = _precos_dia(con, simbolo, dia)
    if not p:
        return {"abertura": None, "minima": None, "maxima": None, "ultimo": None, "anterior": None,
                "var_pct": None}
    anterior = _anterior(con, simbolo, dia, lambda q: q["ultimo"])
    return {"abertura": p["abertura"], "minima": p["minima"], "maxima": p["maxima"], "ultimo": p["ultimo"],
            "anterior": anterior,
            "var_pct": (p["ultimo"] / anterior - 1) * 100 if anterior else None}


def _faixas_win(p):
    """Variacao % do WIN por faixa de horario (09-10, 10-13, 13-16, 16-fim), encadeada: do fim de uma faixa
    ao fim da seguinte, para a soma dar o dia. -> [{"faixa", "var_pct"}]."""
    minutos = sorted(p["por_min"])
    if not minutos:
        return [{"faixa": nome, "var_pct": None} for nome, _, _ in FAIXAS_WIN]
    out = []
    p_ini = p["por_min"][minutos[0]]
    for nome, _m0, m1 in FAIXAS_WIN:
        limite = [m for m in minutos if m < m1] if m1 is not None else minutos
        p_fim = p["por_min"][limite[-1]] if limite else None
        var = (p_fim / p_ini - 1) * 100 if p_fim is not None and p_ini else None
        out.append({"faixa": nome, "var_pct": var})
        if p_fim is not None:
            p_ini = p_fim
    return out


def _taxa_di(con, simbolo, dia):
    """(ultimo mid do dia, ultimo mid do pregao anterior) de um DI em macro_cotacoes."""
    def taxa(d):
        try:
            r = con.execute("SELECT mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<? "
                            "AND mid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1",
                            (simbolo, d * D, (d + 1) * D)).fetchone()
        except sqlite3.OperationalError:
            return None
        return r[0] if r else None
    t = taxa(dia)
    ant = None
    for _ in range(15):
        dia -= 1
        ant = taxa(dia)
        if ant is not None:
            break
    return t, ant


def _iv_por_venc(con, dia):
    """{vencimento: atm_iv} da ultima leitura de cada vencimento do dia (opcoes_resumo, BOVA11)."""
    try:
        return {v: iv for v, iv, _ts in con.execute(
            "SELECT vencimento, atm_iv, max(ts_ms) FROM opcoes_resumo WHERE subjacente='BOVA11' "
            "AND ts_ms>=? AND ts_ms<? AND atm_iv IS NOT NULL GROUP BY vencimento",
            (dia * D, (dia + 1) * D))}
    except sqlite3.OperationalError:
        return {}


def _players_fim(con, dia):
    """Saldo final por grupo dos futuros WIN e WDO do dia (players.saldo_do_dia, sem a fita):
    {ativo: {"maiores": [ate 3 x {"grupo", "saldo"}], "menores": [ate 3]}}."""
    out = {}
    for ativo in players.ativos_do_dia(con, dia):
        if not (ativo.startswith("WIN") or ativo.startswith("WDO")) or not players._FUTURO.match(ativo):
            continue
        m = players.saldo_do_dia(con, dia, ativo)
        if not m:
            continue
        fins = sorted(((g, s["saldo"][-1]) for g, s in m["series"].items() if s.get("saldo")),
                      key=lambda x: -x[1])
        out[ativo] = {"maiores": [{"grupo": g, "saldo": s} for g, s in fins if s > 0][:3],
                      "menores": [{"grupo": g, "saldo": s} for g, s in reversed(fins) if s < 0][:3]}
    return out


def fechamento_api(con, dia=None):
    """Fechamento do dia (ESPEC 6.2): WIN/WDO/indice (abertura, minima, maxima, ultimo e variacao contra o
    fechamento anterior), variacao do WIN por faixa de horario, DI (taxa e variacao em pontos-base), acoes
    e ETFs de MACRO_GRAFICOS ordenadas pela variacao, VIX, vol ATM por vencimento (atm_iv x 100 e variacao
    em pontos de vol contra o pregao anterior), regime, saldo final por grupo no WIN e no WDO e estimador
    do estrangeiro. dia = indice do dia ou 'AAAA-MM-DD'; None = ultimo dia com snapshots do WIN$."""
    from dashboard import MACRO_GRAFICOS
    if dia is None:
        r = con.execute("SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'").fetchone()
        dia = int(r[0]) // D if r and r[0] is not None else None
    else:
        dia = corr._dia(dia)
    vazio = {"abertura": None, "minima": None, "maxima": None, "ultimo": None, "anterior": None,
             "var_pct": None}
    if dia is None:
        return {"dia": None, "data": None, "win": vazio, "wdo": vazio, "indice": vazio, "win_faixas": [],
                "di": [], "acoes": [], "vix": None, "vol": [], "regime": None, "players": {},
                "estrangeiro": None, "texto_final": "Leitura dos dados, não recomendação."}
    p_win = _precos_dia(con, "WIN$", dia)
    di = []
    for s in ("DI1F28", "DI1F29", "DI1F33"):  # jan/28 no lugar do jan/27, que quase nao negocia
        t, ant = _taxa_di(con, s, dia)
        di.append({"simbolo": s, "taxa": t,
                   "var_bps": (t - ant) * 100 if t is not None and ant is not None else None})
    acoes, vistos = [], set()
    for syms in MACRO_GRAFICOS.values():
        for s in syms:
            if s in vistos or s.endswith("$"):
                continue
            vistos.add(s)
            t, ant = _taxa_di(con, s, dia)
            acoes.append({"symbol": s, "var_pct": (t / ant - 1) * 100
                          if t is not None and ant is not None else None})
    acoes.sort(key=lambda x: -(x["var_pct"] if x["var_pct"] is not None else -1e18))
    hoje_iv, ant = _iv_por_venc(con, dia), {}
    d_ant = dia
    for _ in range(15):
        d_ant -= 1
        ant = _iv_por_venc(con, d_ant)
        if ant:
            break
    vol = [{"vencimento": v, "atm_iv": (iv * 100) if iv is not None else None,
            "var_dia": (iv - ant.get(v)) * 100 if iv is not None and ant.get(v) is not None else None}
           for v, iv in sorted(hoje_iv.items())]
    itens = et.calcular(con, dia)
    fins = [i["fin_liq"] for i in itens if i["fin_liq"] is not None]
    linha = _ultima_linha_regime(con, dia)
    return {"dia": dia, "data": corr._data(dia),
            "win": _ohlc(con, "WIN$", dia), "wdo": _ohlc(con, "WDO$", dia), "indice": _ohlc(con, "IND$", dia),
            "win_faixas": _faixas_win(p_win) if p_win else [{"faixa": n, "var_pct": None}
                                                            for n, _, _ in FAIXAS_WIN],
            "di": di, "acoes": acoes, "vix": _taxa_di(con, "IB:VIX", dia)[0], "vol": vol,
            "regime": {"rotulo": linha["rotulo"], "rv_rel": linha["rv_rel"]} if linha else None,
            "players": _players_fim(con, dia),
            "estrangeiro": sum(fins) / 1e6 if fins else None,
            "texto_final": "Leitura dos dados, não recomendação."}
