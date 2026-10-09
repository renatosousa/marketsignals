"""Fluxo de investidores da B3 (BDI > Indicadores > "Participacao dos investidores"), por tipo.

O que a B3 publica (validado em 08/10/2026):
  - compras e vendas em R$ mil por tipo de investidor (Estrangeiro, Institucionais, Individuais,
    Instituicoes Financeiras, Outros), ACUMULADAS DO MES, com defasagem de 2 pregoes (D+2):
    a publicacao do dia P traz o acumulado do mes ate o pregao P-2.
  - o fluxo de UM dia sai da diferenca entre duas publicacoes consecutivas do mesmo mes; a 1a
    publicacao de cada mes ja e o fluxo do 1o pregao.
  - conferido: diferenca de 05/10/2026 = +R$ 10,06 bi, igual ao numero divulgado pela imprensa; e o
    acumulado de setembro (compras 436.290,9 / vendas 427.184,0) bate com a planilha "Dados de Mercado".
  - escopo: mercado de acoes (a vista e fracionario, ETF de renda fixa, leiloes, termo, opcoes e
    blocos). NAO inclui futuros (WIN, WDO, DI): a B3 nao publica fluxo diario por tipo de investidor ali.
  - historico: so ~21 publicacoes recentes ficam no BDI. Por isso o modulo grava tudo no banco e a
    serie cresce um dia por dia.

Tabelas (book.db):
  fluxo_acumulado(data_pub, tipo, compras_mil, vendas_mil)    - cru, como publicado
  fluxo_diario(data_ref, tipo, compras_mil, vendas_mil, saldo_mil, data_pub) - derivado

Uso: python fluxo_estrangeiro.py [--dias 30]      (atualiza e imprime o fluxo diario do estrangeiro)
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import requests

import banco

URL = "https://arquivos.b3.com.br/bdi/table/SharesInvesVolum/{d}/{d}/1/50"
ESTRANGEIRO = "Investidor Estrangeiro"
SCHEMA = """
CREATE TABLE IF NOT EXISTS fluxo_acumulado (
    data_pub TEXT NOT NULL, tipo TEXT NOT NULL, compras_mil REAL, vendas_mil REAL, capturado_em TEXT,
    PRIMARY KEY (data_pub, tipo)
);
CREATE TABLE IF NOT EXISTS fluxo_diario (
    data_ref TEXT NOT NULL, tipo TEXT NOT NULL, compras_mil REAL, vendas_mil REAL, saldo_mil REAL,
    data_pub TEXT, PRIMARY KEY (data_ref, tipo)
);
"""
REVISITAR_DIAS = 6  # publicacoes recentes sao refeitas a cada execucao (a B3 republica/ajusta)


def buscar_publicacao(data_pub):
    """-> {tipo: (compras_mil, vendas_mil)} da publicacao do dia, ou {} se nao ha (feriado/fora da janela)."""
    r = requests.post(URL.format(d=data_pub), json={}, timeout=30)
    r.raise_for_status()
    linhas = r.json().get("table", {}).get("values", [])
    return {v[0]: (float(v[1]), float(v[3])) for v in linhas if v and v[1] is not None and v[3] is not None}


def dias_uteis(inicio, fim):
    d = inicio
    while d <= fim:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def pregoes(con):
    """Datas (AAAA-MM-DD) em que houve pregao, pelas barras do WIN$ (sessoes com >= 300 barras)."""
    q = ("SELECT date(ts_ms/1000, 'unixepoch') d FROM barras_1m WHERE symbol='WIN$' "
         "GROUP BY d HAVING count(*) >= 300 ORDER BY d")
    return [r[0] for r in con.execute(q)]


def recalcular_diario(con):
    """Refaz fluxo_diario a partir de fluxo_acumulado."""
    acum = pd.read_sql("SELECT data_pub, tipo, compras_mil, vendas_mil FROM fluxo_acumulado ORDER BY data_pub", con)
    if acum.empty:
        return 0
    pubs = sorted(acum.data_pub.unique())
    # calendario de pregoes = pregoes das barras + datas de publicacao (so existem em dia util)
    cal = sorted(set(pregoes(con)) | set(pubs))
    pos = {d: i for i, d in enumerate(cal)}
    ref = {p: cal[pos[p] - 2] for p in pubs if pos[p] >= 2}  # D+2
    linhas = []
    for tipo, g in acum.groupby("tipo"):
        g = g.set_index("data_pub").sort_index()
        anterior = None
        for p, row in g.iterrows():
            if p not in ref:
                continue
            mes = ref[p][:7]
            if anterior is not None and anterior[0][:7] == mes and (pos[p] - pos[anterior[1]]) == 1:
                c, v = row.compras_mil - anterior[2], row.vendas_mil - anterior[3]
            elif anterior is None or anterior[0][:7] != mes:
                # 1a publicacao do mes = fluxo do 1o pregao, mas so se a anterior (que fecha o mes
                # anterior) existe; a primeira publicacao da janela nao e um dia isolado
                if anterior is None:
                    anterior = (ref[p], p, row.compras_mil, row.vendas_mil)
                    continue
                c, v = row.compras_mil, row.vendas_mil
            else:  # buraco na serie de publicacoes: nao da para isolar o dia
                anterior = (ref[p], p, row.compras_mil, row.vendas_mil)
                continue
            linhas.append((ref[p], tipo, c, v, c - v, p))
            anterior = (ref[p], p, row.compras_mil, row.vendas_mil)
    con.execute("DELETE FROM fluxo_diario")
    con.executemany("INSERT INTO fluxo_diario VALUES (?,?,?,?,?,?)", linhas)
    con.commit()
    return len(linhas)


def atualizar(db_path, dias=30, verbose=False):
    """Baixa as publicacoes dos ultimos `dias` dias corridos e recalcula o fluxo diario.
    -> (publicacoes_gravadas, linhas_diarias)."""
    con = banco.abrir(db_path, SCHEMA)
    try:
        hoje = date.today()
        existentes = {r[0] for r in con.execute("SELECT DISTINCT data_pub FROM fluxo_acumulado")}
        novas = 0
        for d in dias_uteis(hoje - timedelta(days=dias), hoje):
            s = d.isoformat()
            if s in existentes and (hoje - d).days > REVISITAR_DIAS:
                continue
            try:
                pub = buscar_publicacao(s)
            except requests.RequestException as e:
                if verbose:
                    print(f"{s}: falha ({e.__class__.__name__})")
                continue
            if ESTRANGEIRO not in pub:  # feriado, fora da janela do BDI ou ainda nao publicado
                continue
            agora = datetime.now().isoformat(timespec="seconds")
            con.executemany("INSERT OR REPLACE INTO fluxo_acumulado VALUES (?,?,?,?,?)",
                            [(s, t, c, v, agora) for t, (c, v) in pub.items()])
            novas += 1
        con.commit()
        return novas, recalcular_diario(con)
    finally:
        con.close()


def serie(db_path, tipo=ESTRANGEIRO):
    """DataFrame indexado por data_ref com compras/vendas/saldo (R$ mil) do tipo de investidor."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        df = pd.read_sql("SELECT data_ref, compras_mil, vendas_mil, saldo_mil, data_pub FROM fluxo_diario "
                         "WHERE tipo=? ORDER BY data_ref", con, params=(tipo,))
    finally:
        con.close()
    return df.set_index("data_ref")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--db", default=str(banco.Path(__file__).parent / "dados" / "book.db"))
    a = ap.parse_args()
    n, m = atualizar(a.db, a.dias, verbose=True)
    print(f"{n} publicacoes gravadas, {m} linhas de fluxo diario")
    s = serie(a.db)
    s["saldo_bi"] = s.saldo_mil / 1e6
    print(s[["compras_mil", "vendas_mil", "saldo_bi", "data_pub"]].round(2).to_string())
