"""Metricas, z-scores, alertas e agregados dos leiloes do Tesouro (dados de coletor_leiloes.py).

Por linha de resultado (titulo, vencimento, volta):
  absorcao      venda / oferta (1,0 = lote integral; < 1 = aceitacao parcial). NAO e bid-to-cover: o arquivo do Tesouro
                nao traz o volume de propostas.
  spread_bps    (taxa de corte - taxa media) x 10.000: dispersao das propostas aceitas
  pu_implicito  financeiro / venda (R$ por titulo)
  bacen_share   venda_bacen / (venda + venda_bacen): o Bacen leva quantidade ADICIONAL ao mesmo PU
  concessao_bps (taxa de corte - taxa indicativa ANBIMA da vespera) x 10.000, para o mesmo vencimento (LTN, NTN-F, NTN-B).
                So existe desde 14/09/2026 (historico da ANBIMA em anbima_titulos); LFT fica NULL
  curva_bps     taxa de corte contra a ETTJ da ANBIMA da vespera interpolada no prazo (pre para LTN/NTN-F, IPCA para NTN-B)
  duracao_mod / dv01_brl  duration modificada (base 252, feriados nacionais da ANBIMA calculados aqui, valor na data de
                liquidacao, taxa media) e DV01 = duration x financeiro x 0,0001. Compra e Extra Compra tem sinal
                inverso; LFT e Troca ficam NULL
  pu_modelo / vna_implicito  verificacao do fluxo: PU recalculado pela taxa media (LTN, NTN-F) e VNA que a NTN-B implica
  z_*           z-score contra as observacoes ANTERIORES (nunca a propria nem as do mesmo dia), janela de 1 e de 3 anos,
                minimo de 8 observacoes; por titulo+tipo+volta+vencimento e, se faltar amostra, pela faixa de prazo
                (ate 2, 2 a 5, 5 a 10 e acima de 10 anos). NULL se nenhum dos dois chega a 8.

Alertas (leilao_alertas, sem duplicar por resultado+tipo): ver LIMITES e gerar_alertas().
Uso: python leiloes.py recalcular [--tudo]   |   python leiloes.py relatorio [--dia AAAA-MM-DD] [--saida DIR]
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import banco
from coletor_leiloes import SCHEMA as SCHEMA_RES, agora_br

AQUI = Path(__file__).parent

SCHEMA = SCHEMA_RES + """
CREATE TABLE IF NOT EXISTS leilao_metricas (
    resultado_id INTEGER PRIMARY KEY REFERENCES leilao_resultados(id),
    absorcao REAL, spread_bps REAL, pu_implicito REAL, bacen_share REAL,
    concessao_bps REAL, anbima_data TEXT, curva_bps REAL,
    du_venc INTEGER, duracao_mod REAL, dv01_brl REAL, pu_modelo REAL, vna_implicito REAL,
    z_absorcao_1a REAL, z_absorcao_3a REAL, z_spread_1a REAL, z_spread_3a REAL,
    z_tamanho_1a REAL, z_tamanho_3a REAL, z_concessao_1a REAL, z_concessao_3a REAL, z_nivel TEXT,
    calculado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leilao_alertas (
    id INTEGER PRIMARY KEY, resultado_id INTEGER NOT NULL, tipo TEXT NOT NULL, severidade TEXT NOT NULL,
    mensagem TEXT NOT NULL, criado_em TEXT NOT NULL, entregue INTEGER NOT NULL DEFAULT 0,
    UNIQUE (resultado_id, tipo)
);
"""

LIMITES = dict(
    absorcao_fraca=0.5,        # 1a volta de venda abaixo disso = leilao fraco
    z_absorcao=-2.0,           # z de absorcao abaixo disso = fraco
    z_concessao=2.0,           # z de concessao acima disso = fraco
    z_tamanho=2.0,             # z do financeiro aceito acima disso = lote grande
    dv01_percentil=95,         # DV01 do dia acima desse percentil do historico = lote grande
    dv01_historico_min=40,     # dias de leilao anteriores necessarios para o percentil valer
)
MIN_OBS = 8
JANELAS = {"1a": 365, "3a": 3 * 365}
PRIORIDADE = {"info": 0, "baixa": 1, "aviso": 2, "alta": 3}
VENDAS = ("VENDA", "EXTRA_VENDA")
COMPRAS = ("COMPRA", "EXTRA_COMPRA")
PRE, REAL = ("LTN", "NTN-F"), ("NTN-B",)


# ------------------------------------------------------------------ calendario ANBIMA (calculado, nao baixado)
def pascoa(ano):
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


def feriados(ano):
    """Feriados nacionais que a ANBIMA nao negocia. Consciencia Negra (20/11) so vale desde 2024."""
    p = pascoa(ano)
    f = {date(ano, 1, 1), date(ano, 4, 21), date(ano, 5, 1), date(ano, 9, 7), date(ano, 10, 12), date(ano, 11, 2),
         date(ano, 11, 15), date(ano, 12, 25), p - timedelta(days=48), p - timedelta(days=47), p - timedelta(days=2),
         p + timedelta(days=60)}
    if ano >= 2024:
        f.add(date(ano, 11, 20))
    return f


HOL = np.array(sorted(d for ano in range(2000, 2085) for d in feriados(ano)), dtype="datetime64[D]")


def ok(x):
    """valor numerico presente (nao None, nao NaN)."""
    return x is not None and x == x


def f0(x):
    return float(x) if ok(x) else 0.0


def py(x):
    """numpy/NaN -> tipo nativo do Python para o sqlite (NaN vira NULL)."""
    if x is None:
        return None
    if isinstance(x, (float, np.floating)):
        return float(x) if x == x else None
    if isinstance(x, (int, np.integer)):
        return int(x)
    return x


def d64(s):
    return np.datetime64(s, "D")


def du(d0, d1):
    """Dias uteis de d0 (inclusive) a d1 (exclusive): d0 util => conta (d0, d1] ate d1 util."""
    return np.busday_count(d64(d0), d64(d1), holidays=HOL)


def somar_meses(d, n):
    m = d.month - 1 + n
    ano, mes = d.year + m // 12, m % 12 + 1
    dia = min(d.day, [31, 29 if ano % 4 == 0 and (ano % 100 or ano % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes - 1])
    return date(ano, mes, dia)


def fluxos(titulo, venc, liq):
    """(du, fluxo) por titulo: LTN paga 1000; NTN-F 1000 + cupom de 10% a.a. semestral (48,8088); NTN-B em fracao do VNA
    com cupom de 6% a.a. semestral. Datas de pagamento rolam para o proximo dia util. Fluxos antes ou na liquidacao saem."""
    venc, liq = date.fromisoformat(venc), date.fromisoformat(liq)
    if titulo == "LTN":
        datas, cfs = [venc], [1000.0]
    else:
        cupom = 1000 * (1.10 ** 0.5 - 1) if titulo == "NTN-F" else 1.06 ** 0.5 - 1
        datas, d = [], venc
        while d > liq:
            datas.append(d)
            d = somar_meses(d, -6)
        datas.reverse()
        principal = 1000.0 if titulo == "NTN-F" else 1.0
        cfs = [cupom] * len(datas)
        cfs[-1] += principal
    pag = np.busday_offset(np.array([d64(d) for d in datas]), 0, roll="forward", holidays=HOL)
    dus = np.busday_count(d64(liq), pag, holidays=HOL)
    manter = dus > 0
    return dus[manter].astype(float), np.array(cfs)[manter]


def preco_duration(titulo, venc, liq, y):
    """(preco por titulo [NTN-B: fracao do VNA], duration modificada em anos uteis/252) ou (None, None)."""
    try:
        t, cf = fluxos(titulo, venc, liq)
    except (ValueError, TypeError):
        return None, None
    if len(t) == 0:
        return None, None
    pv = cf / (1 + y) ** (t / 252)
    p = pv.sum()
    return float(p), float((t / 252 * pv).sum() / p / (1 + y))


# ------------------------------------------------------------------ ANBIMA (vespera)
def carregar_anbima(con):
    """({(titulo, venc): [(data, taxa % a.a.)]}, {data: (du, pre, ipca) ordenados}); vazio se a tabela nao existir."""
    taxas, curvas = {}, {}
    try:
        for t, v, d, x in con.execute("SELECT titulo,vencimento,data,tx_indicativa FROM anbima_titulos "
                                      "WHERE titulo IN ('LTN','NTN-F','NTN-B') AND tx_indicativa IS NOT NULL ORDER BY data"):
            taxas.setdefault((t, v), []).append((d, x))
        for d, du_, ipca, pre in con.execute("SELECT data,vertice_du,ettj_ipca,ettj_pre FROM anbima_ettj ORDER BY data,vertice_du"):
            curvas.setdefault(d, []).append((du_, pre, ipca))
    except sqlite3.OperationalError:
        pass
    return taxas, {d: np.array(v, dtype=float) for d, v in curvas.items()}


def vespera(lista, data, max_dias=7):
    """Ultima (data, valor) estritamente anterior a `data`, a no maximo `max_dias` corridos; None se nao houver."""
    ant = [x for x in lista if x[0] < data]
    if not ant:
        return None
    d, v = ant[-1]
    return ant[-1] if (date.fromisoformat(data) - date.fromisoformat(d)).days <= max_dias else None


# ------------------------------------------------------------------ metricas basicas
def basicas(df, anbima):
    """DataFrame de leilao_resultados -> DataFrame com as metricas por linha (mesmo indice)."""
    taxas, curvas = anbima
    n = len(df)
    col = {c: np.full(n, np.nan) for c in ("absorcao", "spread_bps", "pu_implicito", "bacen_share", "concessao_bps", "curva_bps",
                                           "duracao_mod", "dv01_brl", "pu_modelo", "vna_implicito")}
    du_venc = np.full(n, np.nan)
    anbima_data = [None] * n
    for k, r in enumerate(df.itertuples(index=False)):
        venda = f0(r.venda)
        if ok(r.oferta) and r.oferta > 0 and ok(r.venda):
            col["absorcao"][k] = r.venda / r.oferta
        if venda > 0 and ok(r.tx_corte) and ok(r.tx_media):
            col["spread_bps"][k] = (r.tx_corte - r.tx_media) * 1e4
        if venda > 0 and f0(r.financeiro) > 0:
            col["pu_implicito"][k] = r.financeiro / venda
        if f0(r.venda_bacen) > 0:
            col["bacen_share"][k] = r.venda_bacen / (venda + r.venda_bacen)
        if venda <= 0 or r.titulo not in PRE + REAL or not ok(r.tx_media) or not r.liquidacao:
            continue
        # concessao e curva: vespera da ANBIMA
        if ok(r.tx_corte):
            v = vespera(taxas.get((r.titulo, r.vencimento), []), r.data)
            if v:
                col["concessao_bps"][k] = r.tx_corte * 1e4 - v[1] * 100
                anbima_data[k] = v[0]
                c = curvas.get(v[0])
                if c is not None and len(c):
                    prazo = du(v[0], r.vencimento)
                    if c[0, 0] <= prazo <= c[-1, 0]:
                        ref = np.interp(prazo, c[:, 0], c[:, 2 if r.titulo in REAL else 1])
                        col["curva_bps"][k] = r.tx_corte * 1e4 - ref * 100
        # duration e DV01 na data de liquidacao, taxa media
        p, dur = preco_duration(r.titulo, r.vencimento, r.liquidacao, r.tx_media)
        if p is None:
            continue
        du_venc[k] = du(r.liquidacao, r.vencimento)
        col["duracao_mod"][k] = dur
        if r.titulo in PRE:
            col["pu_modelo"][k] = p
        elif ok(col["pu_implicito"][k]):
            col["vna_implicito"][k] = col["pu_implicito"][k] / p
        if f0(r.financeiro) > 0:
            if r.tipo in VENDAS:
                col["dv01_brl"][k] = dur * r.financeiro * 1e-4
            elif r.tipo in COMPRAS:
                col["dv01_brl"][k] = -dur * r.financeiro * 1e-4
    out = pd.DataFrame(col, index=df.index)
    out["du_venc"] = du_venc
    out["anbima_data"] = anbima_data
    return out


# ------------------------------------------------------------------ z-scores
def faixa(data, venc):
    anos = (date.fromisoformat(venc) - date.fromisoformat(data)).days / 365.25
    return "ate 2a" if anos <= 2 else "2a-5a" if anos <= 5 else "5a-10a" if anos <= 10 else "acima 10a"


def z_de(ordem, d, x, alvo):
    """z-score de cada posicao em `alvo` contra as observacoes anteriores (data estritamente menor), nas duas janelas.
    ordem/d/x: arrays do grupo ordenados por data; x NaN = sem observacao. -> {janela: array com NaN onde nao calcula}"""
    z = {j: np.full(len(x), np.nan) for j in JANELAS}
    valido = ~np.isnan(x)
    for i in alvo:
        if not valido[i]:
            continue
        ant = valido & (d < d[i])
        for j, dias in JANELAS.items():
            m = ant & (d >= d[i] - dias)
            if m.sum() >= MIN_OBS:
                sd = x[m].std(ddof=1)
                if sd > 0:
                    z[j][i] = (x[i] - x[m].mean()) / sd
    return z


def zscores(df, met, alvo_idx):
    """z de absorcao, spread, tamanho e concessao das linhas `alvo_idx` (indices de df). Devolve DataFrame (indice alvo)."""
    base = pd.DataFrame({
        "ord": np.arange(len(df)), "d": [date.fromisoformat(x).toordinal() for x in df["data"]],
        "titulo": df["titulo"].values, "tipo": df["tipo"].values, "volta": df["volta"].values, "venc": df["vencimento"].values,
        "faixa": [faixa(a, b) for a, b in zip(df["data"], df["vencimento"])],
        "absorcao": met.absorcao.values, "spread": met.spread_bps.values,
        "tamanho": np.where((df.venda.fillna(0) > 0).values, df.financeiro.values, np.nan),
        "concessao": met.concessao_bps.values,
    }, index=df.index)
    pos = {ix: k for k, ix in enumerate(df.index)}
    alvo_pos = np.array([pos[i] for i in alvo_idx])
    alvo_set = set(alvo_pos.tolist())
    saida = {f"z_{m}_{j}": np.full(len(df), np.nan) for m in ("absorcao", "spread", "tamanho", "concessao") for j in JANELAS}
    nivel = np.array([None] * len(df), dtype=object)
    for nome_nivel, chaves in (("vencimento", ["titulo", "tipo", "volta", "venc"]), ("faixa", ["titulo", "tipo", "volta", "faixa"])):
        for _, g in base.groupby(chaves, sort=False):
            gp = g["ord"].values
            if not alvo_set.intersection(gp.tolist()):
                continue
            o = np.argsort(g["d"].values, kind="stable")
            gp, d = gp[o], g["d"].values[o]
            alvo_o = [k for k, p in enumerate(gp) if p in alvo_set]
            for m in ("absorcao", "spread", "tamanho", "concessao"):
                z = z_de(None, d, g[m].values[o], alvo_o)
                for j in JANELAS:
                    col = saida[f"z_{m}_{j}"]
                    for k in alvo_o:
                        if col[gp[k]] != col[gp[k]] and z[j][k] == z[j][k]:  # so preenche quem o nivel anterior nao cobriu
                            col[gp[k]] = z[j][k]
                            if m == "absorcao" and j == "3a" and nivel[gp[k]] is None:
                                nivel[gp[k]] = nome_nivel
    out = pd.DataFrame(saida, index=df.index).loc[alvo_idx]
    out["z_nivel"] = nivel[alvo_pos]
    return out


# ------------------------------------------------------------------ alertas
def fmt_n(x, nd=0):
    return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def desc(r):
    return f"{r['titulo']} {r['vencimento'][:7]} ({r['volta']}a volta)"


def gerar_alertas(df, met, alvo_idx):
    """Alertas das linhas `alvo_idx`: lista de (indice, tipo, severidade, mensagem). `met` ja tem os z-scores."""
    L = LIMITES
    saida = []
    j = df.join(met)
    for ix in alvo_idx:
        r = j.loc[ix]
        venda, oferta, t = f0(r["venda"]), f0(r["oferta"]), r["tipo"]
        if t == "VENDA" and oferta > 0:
            fraco = []
            if r["volta"] == 1 and ok(r["absorcao"]) and r["absorcao"] < L["absorcao_fraca"]:
                fraco.append(f"absorcao {r['absorcao']:.2f} (so {fmt_n(venda)} de {fmt_n(oferta)} aceitos)")
            if r["volta"] == 1 and ok(r["z_absorcao_3a"]) and r["z_absorcao_3a"] < L["z_absorcao"]:
                fraco.append(f"z de absorcao {r['z_absorcao_3a']:+.1f}")
            if r["volta"] == 1 and ok(r["z_concessao_3a"]) and r["z_concessao_3a"] > L["z_concessao"]:
                fraco.append(f"z de concessao {r['z_concessao_3a']:+.1f} ({r['concessao_bps']:+.0f} bps sobre a ANBIMA)")
            if fraco:
                saida.append((ix, "LEILAO_FRACO", "alta", f"{desc(r)}: " + "; ".join(fraco)))
            elif 0 < venda < oferta:
                saida.append((ix, "ACEITACAO_PARCIAL", "info", f"{desc(r)}: aceitou {fmt_n(venda)} de {fmt_n(oferta)} ({r['absorcao']:.0%})"))
            if venda == 0:
                saida.append((ix, "VENDA_ZERO", "aviso" if r["volta"] == 1 else "baixa", f"{desc(r)}: nada aceito de {fmt_n(oferta)} ofertados"))
            if r["volta"] == 1 and venda == oferta and ok(r["concessao_bps"]) and r["concessao_bps"] <= 0:
                irma = j[(j["data"] == r["data"]) & (j["titulo"] == r["titulo"]) & (j["tipo"] == t)
                         & (j["vencimento"] == r["vencimento"]) & (j["volta"] == 2)]
                if len(irma) and f0(irma.iloc[0]["oferta"]) > 0 and irma.iloc[0]["venda"] == irma.iloc[0]["oferta"]:
                    saida.append((ix, "LEILAO_FORTE", "info", f"{desc(r)}: lote integral nas duas voltas, concessao {r['concessao_bps']:+.0f} bps"))
        if t in VENDAS + COMPRAS and ok(r["z_tamanho_3a"]) and r["z_tamanho_3a"] > L["z_tamanho"]:
            saida.append((ix, "LOTE_GRANDE", "info", f"{desc(r)} {t}: R$ {fmt_n(r['financeiro'] / 1e9, 2)} bi, z de tamanho {r['z_tamanho_3a']:+.1f}"))
        if f0(r["venda_bacen"]) > 0:
            saida.append((ix, "BACEN", "info", f"{desc(r)}: Bacen levou {fmt_n(r['venda_bacen'])} titulos alem dos {fmt_n(venda)} vendidos ({r['bacen_share']:.0%} do total)"))
    # extras: um alerta por (dia, titulo, tipo) na linha de maior financeiro
    ex = j.loc[[i for i in alvo_idx if j.loc[i, "tipo"] in ("EXTRA_VENDA", "EXTRA_COMPRA")]]
    for (d, titulo, t), g in ex.groupby(["data", "titulo", "tipo"]):
        fin = g.financeiro.fillna(0)
        ix = fin.idxmax()
        nome = "extra venda" if t == "EXTRA_VENDA" else "extra compra (recompra pelo Tesouro)"
        saida.append((ix, t, "aviso", f"{d} {titulo}: leilao {nome}, {len(g)} vencimentos, R$ {fmt_n(fin.sum() / 1e9, 2)} bi aceitos "
                                      f"({fmt_n(g.venda.fillna(0).sum())} de {fmt_n(g.oferta.fillna(0).sum())} titulos ofertados)"))
    return saida


def alertas_dv01(df, met, alvo_idx):
    """LOTE_GRANDE por DV01 do dia: dias de venda cujo DV01 total passa do percentil do historico anterior."""
    L = LIMITES
    j = df.join(met)
    j = j[j.tipo.isin(VENDAS) & j.dv01_brl.notna()]
    dia = j.groupby("data").dv01_brl.sum().sort_index()
    saida = []
    alvo_datas = set(df.loc[alvo_idx, "data"])
    for d in dia.index:
        if d not in alvo_datas:
            continue
        ant = dia[dia.index < d]
        if len(ant) < L["dv01_historico_min"]:
            continue
        corte = np.percentile(ant.values, L["dv01_percentil"])
        if dia[d] > corte:
            ix = j[j.data == d].dv01_brl.idxmax()
            saida.append((ix, "LOTE_GRANDE_DV01", "aviso",
                          f"{d}: DV01 de R$ {fmt_n(dia[d] / 1e6, 2)} mi no dia, acima do percentil {L['dv01_percentil']} do historico (R$ {fmt_n(corte / 1e6, 2)} mi)"))
    return saida


# ------------------------------------------------------------------ orquestracao
COLS_RES = ("id,data,titulo,tipo,volta,liquidacao,vencimento,oferta,tx_media,tx_corte,venda,financeiro,venda_bacen,financeiro_bacen")


def ler_resultados(con):
    return pd.read_sql_query(f"SELECT {COLS_RES} FROM leilao_resultados ORDER BY data,id", con)  # numericos: float com NaN


def atualizar(db, ids=None, alertas_desde=None):
    """Recalcula metricas (e z-scores) das linhas `ids` (None = todas) e grava os alertas das que tiverem data >=
    `alertas_desde` (AAAA-MM-DD; None = sem alertas). -> (linhas calculadas, alertas novos)."""
    db.executescript(SCHEMA)
    df = ler_resultados(db)
    if df.empty:
        return 0, 0
    alvo = df.index[df.id.isin(ids)] if ids is not None else df.index
    if len(alvo) == 0:
        return 0, 0
    met = basicas(df, carregar_anbima(db))
    z = zscores(df, met, list(alvo))
    met = met.join(z)
    agora = agora_br().isoformat(timespec="seconds")
    cols = ["absorcao", "spread_bps", "pu_implicito", "bacen_share", "concessao_bps", "anbima_data", "curva_bps", "du_venc",
            "duracao_mod", "dv01_brl", "pu_modelo", "vna_implicito"] + [f"z_{m}_{j}" for m in ("absorcao", "spread", "tamanho", "concessao") for j in JANELAS] + ["z_nivel"]
    linhas = [(int(df.id[i]), *[py(met.at[i, c]) for c in cols], agora) for i in alvo]
    with db:
        db.executemany("INSERT OR REPLACE INTO leilao_metricas (resultado_id," + ",".join(cols) + ",calculado_em) VALUES (" +
                       ",".join("?" * (len(cols) + 2)) + ")", linhas)
    novos = 0
    if alertas_desde:
        alvo_a = [i for i in alvo if df.at[i, "data"] >= alertas_desde]
        todos = gerar_alertas(df, met, alvo_a) + alertas_dv01(df, met, alvo_a)
        with db:
            for ix, tipo, sev, msg in todos:
                cur = db.execute("INSERT OR IGNORE INTO leilao_alertas (resultado_id,tipo,severidade,mensagem,criado_em) VALUES (?,?,?,?,?)",
                                 (int(df.id[ix]), tipo, sev, msg, agora))
                novos += cur.rowcount
    print(f"metricas: {len(linhas)} linhas; alertas novos: {novos}", flush=True)
    return len(linhas), novos


# ------------------------------------------------------------------ agregados para a pagina e os relatorios
def proximos_leiloes(hoje=None):
    """Estimativa pelo padrao de 2026 (NAO e calendario oficial): terca LFT+NTN-B, quinta LTN+NTN-F; feriados saem."""
    hoje = hoje or agora_br().date()
    out, d = [], hoje
    while len(out) < 4:
        if d.weekday() in (1, 3) and d not in feriados(d.year):
            out.append({"data": d.isoformat(), "titulos": "LFT, NTN-B" if d.weekday() == 1 else "LTN, NTN-F",
                        "origem": "estimativa (padrao semanal de 2026)"})
        d += timedelta(days=1)
    return out


def montar(con, dia=None):
    """JSON da pagina /leiloes: dia escolhido (padrao: o ultimo leilao), linhas com metricas, alertas, resumo por titulo,
    semanas recentes, serie de absorcao e proximos leiloes estimados."""
    vazio = {"dias": [], "dia": None, "linhas": [], "alertas": [], "resumo": [], "semanas": [], "serie": {},
             "proximos": proximos_leiloes(), "ultima_atualizacao": None}
    try:
        con.execute("SELECT 1 FROM leilao_metricas LIMIT 1")
    except sqlite3.OperationalError:
        return vazio | {"aviso": "Sem dados de leiloes no banco: rode python coletor_leiloes.py backfill."}
    df = pd.read_sql_query(
        "SELECT r.*,m.absorcao,m.spread_bps,m.pu_implicito,m.bacen_share,m.concessao_bps,m.curva_bps,m.duracao_mod,m.dv01_brl,"
        "m.z_absorcao_1a,m.z_absorcao_3a,m.z_spread_3a,m.z_tamanho_3a,m.z_concessao_3a,m.z_nivel "
        "FROM leilao_resultados r LEFT JOIN leilao_metricas m ON m.resultado_id=r.id ORDER BY r.data,r.id", con)
    dias = sorted(df.data.unique())
    dia = dia if dia in dias else (dias[-1] if dias else None)
    out = {"dias": dias[-60:][::-1], "dia": dia, "linhas": [], "alertas": [], "resumo": [], "semanas": [], "serie": {},
           "proximos": proximos_leiloes(), "ultima_atualizacao": None}
    if dia is None:
        return out
    out["ultima_atualizacao"] = con.execute("SELECT MAX(atualizado_em) FROM leilao_resultados").fetchone()[0]
    d = df[df.data == dia]
    cols = ["id", "titulo", "tipo", "volta", "vencimento", "liquidacao", "oferta", "venda", "financeiro", "tx_media", "tx_corte", "absorcao",
            "spread_bps", "concessao_bps", "curva_bps", "bacen_share", "venda_bacen", "dv01_brl", "duracao_mod", "z_absorcao_3a",
            "z_spread_3a", "z_tamanho_3a", "z_concessao_3a", "z_nivel"]
    out["linhas"] = json.loads(d[cols].to_json(orient="records"))
    out["alertas"] = [dict(zip(("tipo", "severidade", "mensagem", "criado_em", "titulo", "vencimento", "volta"), r)) for r in con.execute(
        "SELECT a.tipo,a.severidade,a.mensagem,a.criado_em,r.titulo,r.vencimento,r.volta FROM leilao_alertas a "
        "JOIN leilao_resultados r ON r.id=a.resultado_id WHERE r.data=? ORDER BY CASE a.severidade WHEN 'alta' THEN 0 WHEN 'aviso' THEN 1 "
        "WHEN 'baixa' THEN 2 ELSE 3 END, a.id", (dia,))]
    for (titulo, tipo), g in d.groupby(["titulo", "tipo"]):
        o, vd = g.oferta.fillna(0).sum(), g.venda.fillna(0).sum()
        out["resumo"].append({"titulo": titulo, "tipo": tipo, "oferta": o, "venda": vd, "financeiro": g.financeiro.fillna(0).sum(),
                              "absorcao": vd / o if o else None, "dv01_brl": g.dv01_brl.sum() if g.dv01_brl.notna().any() else None,
                              "venda_bacen": g.venda_bacen.fillna(0).sum()})
    # semanas: financeiro e DV01 por familia (prefixado = LTN+NTN-F; juro real = NTN-B; LFT so financeiro); recompras a parte
    v = df[df.tipo.isin(VENDAS + COMPRAS) & (df.venda.fillna(0) > 0)].copy()
    v["semana"] = [(date.fromisoformat(x) - timedelta(days=date.fromisoformat(x).weekday())).isoformat() for x in v.data]
    v["familia"] = np.where(v.tipo.isin(COMPRAS), "Recompra",
                            np.where(v.titulo.isin(PRE), "Prefixado", np.where(v.titulo == "NTN-B", "Juro real", "LFT")))
    g = v.groupby(["semana", "familia"]).agg(financeiro=("financeiro", "sum"), dv01=("dv01_brl", "sum")).reset_index()
    if len(g):
        out["semanas"] = json.loads(g[g.semana >= sorted(g.semana.unique())[-12:][0]].to_json(orient="records"))
    # serie: absorcao da 1a volta de venda, media ponderada pela oferta, por dia e titulo (ultimos 12 meses)
    s = df[(df.tipo == "VENDA") & (df.volta == 1) & (df.oferta.fillna(0) > 0) & (df.data >= (date.fromisoformat(dias[-1]) - timedelta(days=365)).isoformat())]
    for titulo, g in s.groupby("titulo"):
        a = g.groupby("data").apply(lambda x: x.venda.fillna(0).sum() / x.oferta.sum(), include_groups=False)
        out["serie"][titulo] = [[k, round(float(x), 4)] for k, x in a.items()]
    return out


def relatorio(con, dia, saida):
    """Markdown e CSV do dia em `saida` (dados/relatorios por padrao)."""
    m = montar(con, dia)
    if m["dia"] is None:
        print("sem leiloes no banco")
        return 1
    Path(saida).mkdir(parents=True, exist_ok=True)
    base = Path(saida) / f"leilao_{m['dia']}"
    pd.DataFrame(m["linhas"]).to_csv(base.with_suffix(".csv"), index=False, sep=";", decimal=",")
    L = [f"# Leiloes do Tesouro de {m['dia']}", ""]
    if m["alertas"]:
        L += ["## Alertas", ""] + [f"- **{a['severidade']}** {a['mensagem']}" for a in m["alertas"] if PRIORIDADE[a["severidade"]] >= 2] + [""]
    L += ["## Por titulo", "", "| Titulo | Tipo | Ofertado | Vendido | Absorcao | Financeiro (R$ mi) | DV01 (R$ mil) |", "|---|---|---:|---:|---:|---:|---:|"]
    for r in m["resumo"]:
        L.append(f"| {r['titulo']} | {r['tipo']} | {fmt_n(r['oferta'])} | {fmt_n(r['venda'])} | "
                 f"{'-' if r['absorcao'] is None else format(r['absorcao'], '.0%')} | {fmt_n(r['financeiro'] / 1e6, 1)} | "
                 f"{'-' if r['dv01_brl'] is None else fmt_n(r['dv01_brl'] / 1e3, 1)} |")
    L += ["", "Absorcao = venda / oferta (o arquivo do Tesouro nao traz o volume de propostas, entao nao ha bid-to-cover).", ""]
    base.with_suffix(".md").write_text("\n".join(L), encoding="utf-8")
    print(f"gravado: {base}.md e .csv")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Metricas e alertas dos leiloes do Tesouro")
    ap.add_argument("comando", choices=("recalcular", "relatorio"))
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--dia")
    ap.add_argument("--saida", default=str(AQUI / "dados" / "relatorios"))
    ap.add_argument("--tudo", action="store_true", help="recalcular: gera alertas de todo o historico (padrao: ultimos 10 dias)")
    a = ap.parse_args(argv)
    db = banco.abrir(a.db, SCHEMA)
    try:
        if a.comando == "recalcular":
            atualizar(db, None, "0000-00-00" if a.tudo else (agora_br().date() - timedelta(days=10)).isoformat())
            return 0
        return relatorio(db, a.dia, a.saida)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
