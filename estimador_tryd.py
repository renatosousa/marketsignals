"""Estimador do fluxo do estrangeiro pelo saldo por corretora do Tryd, e o placar contra o oficial da B3.

A B3 publica o fluxo por tipo de investidor so das acoes, com 2 pregoes de atraso (fluxo_estrangeiro.py). Enquanto o
rotulo nao sai, a janela "Saldo por corretora" do Tryd (tryd_saldo, coletor_tryd.py) da uma estimativa para o dia: a
ultima foto coerente de cada corretora a partir das 10:00, somada pelas corretoras cujo grupo no mapa e "Estrangeiro".
Grupo de corretora e uma hipotese de quem opera por cada casa (players.MAPA), nao o tipo de investidor da B3: o placar
existe justamente para medir o quanto a estimativa acompanha o oficial (ESPEC_DASHBOARD.md, secoes 6.6 e 6.7).

Grava (atualizar, chamado na partida de coletor_regime.py) a tabela tryd_estrangeiro_dia: uma linha por acao com foto
no dia, com saldo_qtd (acoes), fin_liq (R$), vol_fin_grupo (R$ do grupo), vol_fin_total (R$ do ativo / 2) e foto_ms.
Le tryd_saldo, fluxo_diario, features_fluxo_dia, agressao_1min, snapshots e macro_cotacoes. As demais funcoes so leem.

Uso: python estimador_tryd.py atualizar [--db dados/book.db]   (grava tryd_estrangeiro_dia dos dias fechados)
     python estimador_tryd.py placar [--db dados/book.db]      (so le: imprime a tabela e o resumo)
"""
import argparse
import bisect
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import banco
import players
import relogio
from leiloes import feriados

SCHEMA = """CREATE TABLE IF NOT EXISTS tryd_estrangeiro_dia (
    data TEXT NOT NULL, ativo TEXT NOT NULL,
    saldo_qtd REAL, fin_liq REAL, vol_fin_grupo REAL, vol_fin_total REAL, foto_ms INTEGER,
    PRIMARY KEY (data, ativo));"""
GRUPO = "Estrangeiro"
ABERTURA_ACOES_MS = 10 * 3_600_000
DIA_MS = 86_400_000
TIPO_B3 = "Investidor Estrangeiro"  # tipo em fluxo_diario (fluxo_estrangeiro.ESTRANGEIRO, sem importar o requests)
MIN_BALDES_5M = 24  # acompanha_preco: correlacao de 5 min exige esta amostra
MULT_FUTURO = {"WIN": 0.20, "WDO": 10.0}  # R$ por ponto, por contrato
LIMIAR_ROTULO = 0.25
CAPTURA_MIN = 0.9  # cesta com captura menor fica fora do resumo: ate 27/10 o polling captava ~80% e trocava o sinal


def _num(v):
    return v if isinstance(v, (int, float)) else None  # vazio ou texto (DDE instavel) = sem leitura


def _data(dia):
    return (date(1970, 1, 1) + timedelta(days=dia)).isoformat()


def _dia(data):
    return (date.fromisoformat(data) - date(1970, 1, 1)).days


def _hoje_dia():
    return int(relogio.local_br_ms()) // DIA_MS


def _sinal(x):
    return (x > 0) - (x < 0)


def _corr(a, b):
    """Correlacao de Pearson; None se a amostra for curta ou uma das series for constante."""
    if len(a) != len(b) or len(a) < 2:
        return None
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    c = float(np.corrcoef(a, b)[0, 1])
    return c if c == c else None


def _data_prevista(data, n_pregoes=2):
    """Data AAAA-MM-DD + `n_pregoes` pregoes: proximos dias uteis, sem os feriados nacionais de leiloes.feriados."""
    d, fer, faltam = date.fromisoformat(data), {}, n_pregoes
    while faltam:
        d += timedelta(days=1)
        if d.year not in fer:
            fer[d.year] = feriados(d.year)
        if d.weekday() < 5 and d not in fer[d.year]:
            faltam -= 1
    return d.isoformat()


def ultima_foto_por_corretora(con, ativo, dia):
    """Ultima foto valida de cada corretora do ativo no dia, a partir da abertura das acoes (10:00): antes disso a
    janela do Tryd ainda mostra a tabela do pregao anterior (players.abertura_ms). Valida = qtd, qtdc e qtdv numericos
    e |qtd - (qtdc - qtdv)| < 0,5 (o DDE as vezes grava texto ou vazio, e a janela redesenha enquanto e lida).
    -> {brk: (qtd, qtdc, qtdv, volc, volv, ts_ms)}; volc/volv podem ser None na foto valida."""
    fotos = {}
    for ts, brk, qtd, qtdc, qtdv, volc, volv in con.execute(
            "SELECT ts_ms, brk, qtd, qtdc, qtdv, volc, volv FROM tryd_saldo "
            "WHERE ativo=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms, linha",
            (ativo, dia * DIA_MS + ABERTURA_ACOES_MS, (dia + 1) * DIA_MS)):
        q, c, v = _num(qtd), _num(qtdc), _num(qtdv)
        if q is None or c is None or v is None or abs(q - (c - v)) >= 0.5:
            continue
        fotos[brk] = (q, c, v, _num(volc), _num(volv), ts)
    return fotos


def calcular(con, dia):
    """Um item por acao (ativo de tryd_saldo no dia que nao casa com players._FUTURO) com foto valida no dia, so le.
    Do grupo "Estrangeiro": saldo_qtd = soma de qtd; fin_liq = soma (volc - volv), so de corretoras com volc e volv
    numericos; vol_fin_grupo = soma (volc + volv). De todas as corretoras: vol_fin_total = soma (volc + volv) / 2
    (compra + venda conta o negocio duas vezes). foto_ms = maior ts_ms usado. E a mesma conta do script de
    conferencia; os numeros de aceite da ESPEC_DASHBOARD.md (6.6) saem dela."""
    itens = []
    for ativo in players.ativos_do_dia(con, dia):
        if players._FUTURO.match(ativo):
            continue
        fotos = ultima_foto_por_corretora(con, ativo, dia)
        if not fotos:
            continue
        saldo_qtd, fins, vols_grupo, vol_total, foto_ms = 0.0, [], [], 0.0, 0
        for brk, (q, _c, _v, volc, volv, ts) in fotos.items():
            foto_ms = max(foto_ms, ts)
            if players.grupo(brk) == GRUPO:
                saldo_qtd += q
            if volc is None or volv is None:
                continue
            vol_total += volc + volv
            if players.grupo(brk) == GRUPO:
                fins.append(volc - volv)
                vols_grupo.append(volc + volv)
        itens.append({"data": _data(dia), "ativo": ativo, "saldo_qtd": saldo_qtd,
                      "fin_liq": sum(fins) if fins else None, "vol_fin_grupo": sum(vols_grupo),
                      "vol_fin_total": vol_total / 2, "foto_ms": foto_ms})
    return itens


def atualizar(db_path, hoje_dia=None):
    """Grava tryd_estrangeiro_dia (INSERT OR REPLACE de calcular) de cada dia de tryd_saldo anterior a hoje que ainda
    nao tenha linhas; o dia mais recente anterior a hoje e sempre refeito (a ultima foto pode chegar depois da 1a
    passada). Dias sem acao com foto (so contratos) nao gravam nada. -> numero de dias gravados."""
    con = banco.abrir(db_path, SCHEMA)
    try:
        hoje_dia = _hoje_dia() if hoje_dia is None else hoje_dia
        passados = sorted(d for (d,) in con.execute("SELECT DISTINCT ts_ms / 86400000 FROM tryd_saldo") if d < hoje_dia)
        if not passados:
            return 0
        feitos = {r[0] for r in con.execute("SELECT DISTINCT data FROM tryd_estrangeiro_dia")}
        gravar = sorted({d for d in passados if _data(d) not in feitos} | {passados[-1]})
        n = 0
        for dia in gravar:
            itens = calcular(con, dia)
            if not itens:
                continue
            con.executemany("INSERT OR REPLACE INTO tryd_estrangeiro_dia VALUES (?,?,?,?,?,?,?)",
                            [(i["data"], i["ativo"], i["saldo_qtd"], i["fin_liq"], i["vol_fin_grupo"],
                              i["vol_fin_total"], i["foto_ms"]) for i in itens])
            n += 1
        con.commit()
        return n
    finally:
        con.close()


def _resumo(pares):
    """Resumo de uma estimativa contra o oficial: n, acertos de sinal, postos (correlacao de Pearson dos rank(); None
    se n < 4), erro absoluto medio e vies (media de estimativa - oficial)."""
    n = len(pares)
    r = {"n": n, "sinal_certo": sum(_sinal(a) == _sinal(b) for a, b in pares), "postos": None,
         "erro_abs_medio": None, "vies": None}
    if not n:
        return r
    est, ofi = pd.Series([a for a, _ in pares]), pd.Series([b for _, b in pares])
    if n >= 4:
        p = est.rank().corr(ofi.rank())
        r["postos"] = float(p) if p == p else None
    r["erro_abs_medio"] = float((est - ofi).abs().mean())
    r["vies"] = float((est - ofi).mean())
    return r


def _tryd_do_dia(con, dia, tem_tabela):
    """(soma de fin_liq em R$ mi, n de ativos) do dia: da tabela tryd_estrangeiro_dia ou, se a tabela nao existir ou
    o dia nao estiver la, calculado na hora. (None, 0) se o dia nao tem acao com foto (so contratos)."""
    if tem_tabela:
        linhas = con.execute("SELECT ativo, fin_liq FROM tryd_estrangeiro_dia WHERE data=?", (_data(dia),)).fetchall()
        if linhas:
            fins = [f for _, f in linhas if f is not None]
            return (sum(fins) / 1e6 if fins else None), len(linhas)
    itens = calcular(con, dia)
    fins = [i["fin_liq"] for i in itens if i["fin_liq"] is not None]
    return (sum(fins) / 1e6 if fins else None), len(itens)


def placar(con, hoje_dia=None):
    """Placar do estrangeiro (so le): uma linha por data, da mais nova para a mais velha, com oficial
    (fluxo_diario, saldo_mil / 1000, em R$ mi), tryd (soma de fin_liq / 1e6) e cesta (features_fluxo_dia.
    cesta_fin_liq_mi). Datas = uniao dos dias de tryd_saldo e de features_fluxo_dia anteriores a hoje, mais as do
    oficial nesse periodo; data sem oficial e ja encerrada recebe oficial_previsto = data + 2 pregoes uteis.
    Resumo por estimativa so com datas que tem oficial e a estimativa (a cesta so com captura_vol >= CAPTURA_MIN), mais
    a regua "sinal do dia anterior do oficial".
    -> {"linhas": [...], "resumo": {"tryd": {...}, "cesta": {...}, "dia_anterior": {...}}}."""
    hoje_dia = _hoje_dia() if hoje_dia is None else hoje_dia
    tem_tabela = bool(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tryd_estrangeiro_dia'"
                                  ).fetchone())
    tryd, n_ativos = {}, {}
    for dia in sorted(d for (d,) in con.execute("SELECT DISTINCT ts_ms / 86400000 FROM tryd_saldo") if d < hoje_dia):
        tryd[_data(dia)], n_ativos[_data(dia)] = _tryd_do_dia(con, dia, tem_tabela)
    cesta, captura, oficial, pub = {}, {}, {}, {}
    try:
        for d, v, cap in con.execute("SELECT data, cesta_fin_liq_mi, captura_vol FROM features_fluxo_dia"):
            if _dia(d) < hoje_dia:
                cesta[d], captura[d] = v, cap
    except sqlite3.OperationalError:
        pass
    try:
        for d, s, p in con.execute("SELECT data_ref, saldo_mil, data_pub FROM fluxo_diario WHERE tipo=?", (TIPO_B3,)):
            oficial[d], pub[d] = s / 1000, p
    except sqlite3.OperationalError:
        pass
    base = sorted(set(tryd) | set(cesta))
    datas = sorted(set(base) | {d for d in oficial if base and base[0] <= d <= base[-1]}, reverse=True)
    linhas = []
    for d in datas:
        sem_oficial = oficial.get(d) is None
        linhas.append({"data": d, "oficial": oficial.get(d), "data_pub": pub.get(d),
                       "tryd": tryd.get(d), "n_ativos": n_ativos.get(d), "cesta": cesta.get(d),
                       "cesta_captura": captura.get(d),
                       "cesta_no_resumo": cesta.get(d) is not None and (captura.get(d) or 0) >= CAPTURA_MIN,
                       "oficial_previsto": _data_prevista(d) if sem_oficial and _dia(d) < hoje_dia else None})
    resumo = {"tryd": _resumo([(l["tryd"], l["oficial"]) for l in linhas
                               if l["tryd"] is not None and l["oficial"] is not None]),
              "cesta": _resumo([(l["cesta"], l["oficial"]) for l in linhas
                                if l["cesta_no_resumo"] and l["oficial"] is not None])}
    resumo["cesta"]["captura_min"] = CAPTURA_MIN
    ofs, n, certos = sorted(oficial), 0, 0
    for l in linhas:
        if l["oficial"] is None:
            continue
        i = bisect.bisect_left(ofs, l["data"])
        if i == 0:
            continue  # sem oficial anterior na tabela
        n += 1
        certos += _sinal(oficial[ofs[i - 1]]) == _sinal(l["oficial"])
    resumo["dia_anterior"] = {"n": n, "sinal_certo": certos}
    return {"linhas": linhas, "resumo": resumo}


def hoje(con, dia):
    """Estimativa parcial do dia em curso (so le): calcular(con, dia) por ativo (fin_liq em R$ mi) e a soma; e a cesta
    parcial = soma de (fin_compra - fin_venda) de agressao_1min sem o WDO$, das 10:00 as 16:55 (o leilao de fechamento
    e o after-market nao tem agressor), em R$ mi, com a serie acumulada por minuto para o grafico."""
    itens = calcular(con, dia)
    por_ativo = [{"ativo": i["ativo"], "fin_liq_mi": i["fin_liq"] / 1e6 if i["fin_liq"] is not None else None}
                 for i in itens]
    lo = dia * DIA_MS
    por_min = {}
    for ts, sym, n, fc, fv in con.execute(
            "SELECT ts_ms, symbol, n, fin_compra, fin_venda FROM agressao_1min "
            "WHERE ts_ms>=? AND ts_ms<? AND symbol<>'WDO$' ORDER BY ts_ms",
            (lo + ABERTURA_ACOES_MS, lo + 16 * 3_600_000 + 55 * 60_000)):
        k = (sym, ts // 60000)
        if k not in por_min or (n or 0) > (por_min[k][0] or 0):  # mais de uma sessao no minuto: fica a de mais negocios
            por_min[k] = (n, fc, fv)
    liq = {}
    for (_sym, m), (_n, fc, fv) in por_min.items():
        liq[m] = liq.get(m, 0.0) + (fc or 0) - (fv or 0)
    minutos = sorted(liq)
    acumulado = (np.cumsum([liq[m] for m in minutos]) / 1e6).tolist() if minutos else []
    return {"por_ativo": por_ativo, "tryd_mi": float(sum(a["fin_liq_mi"] for a in por_ativo if a["fin_liq_mi"] is not None)),
            "cesta": {"mi": float(acumulado[-1]) if acumulado else 0.0,
                      "t": [m * 60 for m in minutos], "acumulado_mi": acumulado}}


def _preco_futuro(con, dia, ativo):
    """Ultimo mid do dia do contrato-continuo: WIN$ vem de snapshots e WDO$ de macro_cotacoes (barras_1m so e
    atualizada na partida do coletor de regime)."""
    if ativo.startswith("WIN"):
        q = "SELECT mid FROM snapshots WHERE symbol='WIN$' AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms DESC LIMIT 1"
    elif ativo.startswith("WDO"):
        q = "SELECT mid FROM macro_cotacoes WHERE symbol='WDO$' AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms DESC LIMIT 1"
    else:
        return None
    r = con.execute(q, (dia * DIA_MS, (dia + 1) * DIA_MS)).fetchone()
    return r[0] if r and r[0] is not None else None


def mapa_grupos(con, dia):
    """Saldo por grupo (players.GRUPOS) x ativo do dia, em R$ mi (so le). Acoes: soma (volc - volv) das ultimas fotos
    validas por corretora a partir das 10:00 (mesma regra de calcular). Futuros: saldo final de players.saldo_do_dia em
    contratos (a tabela de contratos ja abre zerada as 09:00, sem o corte das 10:00) x ultimo preco do dia x
    multiplicador do ponto (WIN 0,20; WDO 10). -> {"ativos", "grupos", "valores": {grupo: {ativo: valor ou None}}}."""
    ativos = players.ativos_do_dia(con, dia)
    valores = {g: {} for g in players.GRUPOS}
    for ativo in ativos:
        if players._FUTURO.match(ativo):
            m = players.saldo_do_dia(con, dia, ativo) or {}
            mult = next((v for p, v in MULT_FUTURO.items() if ativo.startswith(p)), None)
            preco = _preco_futuro(con, dia, ativo)
            for g in players.GRUPOS:
                saldo = m.get("series", {}).get(g, {}).get("saldo")
                valores[g][ativo] = None if saldo is None or preco is None or mult is None \
                    else saldo[-1] * preco * mult / 1e6
        else:
            fins = {}
            for brk, (_q, _c, _v, volc, volv, _ts) in ultima_foto_por_corretora(con, ativo, dia).items():
                if volc is not None and volv is not None:
                    g = players.grupo(brk)
                    fins[g] = fins.get(g, 0.0) + (volc - volv)
            for g in players.GRUPOS:
                valores[g][ativo] = fins[g] / 1e6 if g in fins else None
    return {"ativos": ativos, "grupos": list(players.GRUPOS), "valores": valores}


def _preco_por_minuto(con, dia, ativo):
    """Preco do ativo por minuto do dia (ultimo mid do minuto). WIN: snapshots WIN$; WDO: macro_cotacoes WDO$;
    acao/ETF: macro_cotacoes do proprio simbolo (BOVA11 so existe em snapshots, ver ESPEC_DASHBOARD.md secao 2)."""
    fontes = [("snapshots", "WIN$")] if ativo.startswith("WIN") else \
        [("macro_cotacoes", "WDO$")] if ativo.startswith("WDO") else [("macro_cotacoes", ativo), ("snapshots", ativo)]
    for tabela, simbolo in fontes:
        por_min = {}
        for ts, mid in con.execute(f"SELECT ts_ms, mid FROM {tabela} WHERE symbol=? AND ts_ms>=? AND ts_ms<? "
                                   "ORDER BY ts_ms", (simbolo, dia * DIA_MS, (dia + 1) * DIA_MS)):
            if mid is not None:
                por_min[ts // 60000] = mid
        if por_min:
            return por_min
    return {}


def _grade(por_min, div_min):
    """{minuto: valor} -> {balde: ultimo valor do balde}, balde = minuto // div_min."""
    return {m // div_min: v for m, v in sorted(por_min.items())}


def _ffill(baldes, b0, b1):
    """Valores dos baldes b0..b1 repetindo o ultimo observado; a semente e o ultimo balde em b0 ou antes (b0 e o
    maximo dos primeiros baldes das duas series, entao sempre existe)."""
    atual = baldes[max(b for b in baldes if b <= b0)]
    out = []
    for b in range(b0, b1 + 1):
        if b in baldes:
            atual = baldes[b]
        out.append(atual)
    return out


def _corr_grade(saldo_min, preco_min, div_min, minimo, diferenca):
    """Correlacao na grade de `div_min` (ultimo valor do balde, ffill, so o trecho comum as duas series): do saldo com
    o preco, ou (diferenca=True) da diferenca do saldo com o log-retorno do preco. None se houver menos de `minimo`
    baldes ou uma serie for constante."""
    s, p = _grade(saldo_min, div_min), _grade(preco_min, div_min)
    if not s or not p:
        return None
    b0, b1 = max(min(s), min(p)), min(max(s), max(p))
    if b1 - b0 + 1 < minimo:
        return None
    sv, pv = np.array(_ffill(s, b0, b1)), np.array(_ffill(p, b0, b1))
    if diferenca:
        sv, pv = np.diff(sv), np.log(pv[1:] / pv[:-1])
    return _corr(sv, pv)


def acompanha_preco(con, dia, ativo):
    """Por grupo de players.saldo_do_dia(con, dia, ativo) (o saldo do montar, sem a fita), so le: corr_5m = correlacao entre a diferenca do saldo e o
    log-retorno do preco em baldes de 5 min (ultimo valor do balde, ffill; menos de MIN_BALDES_5M baldes -> None);
    nivel = correlacao entre o saldo e o preco na grade de 1 min (series acumuladas: duas tendencias se correlacionam
    por acaso, e so contexto, ESPEC_DASHBOARD.md R8). Rotulo por corr_5m: >= 0,25 "a favor do preco"; <= -0,25
    "contra o preco"; senao "sem relacao"; None -> "amostra curta". -> [{"grupo", "corr_5m", "nivel", "rotulo",
    "saldo_final"}] (saldo_final na unidade do ativo: contratos ou cotas)."""
    m, preco = players.saldo_do_dia(con, dia, ativo), _preco_por_minuto(con, dia, ativo)
    if m is None:
        return []
    out = []
    for g in players.GRUPOS:
        saldo = m.get("series", {}).get(g, {}).get("saldo")
        if saldo is None:
            continue
        por_min = {t // 60: s for t, s in zip(m["t"], saldo)}  # ultima foto do minuto
        corr_5m = _corr_grade(por_min, preco, 5, MIN_BALDES_5M, True) if preco else None
        nivel = _corr_grade(por_min, preco, 1, 2, False) if preco else None
        if corr_5m is None:
            rotulo = "amostra curta"
        elif corr_5m >= LIMIAR_ROTULO:
            rotulo = "a favor do preço"
        elif corr_5m <= -LIMIAR_ROTULO:
            rotulo = "contra o preço"
        else:
            rotulo = "sem relação"
        out.append({"grupo": g, "corr_5m": corr_5m, "nivel": nivel, "rotulo": rotulo, "saldo_final": saldo[-1]})
    return out


def _txt(v, nd=2, sinal=False):
    return "-" if v is None else (f"{v:+.{nd}f}" if sinal else f"{v:.{nd}f}")


def _imprimir_placar(r):
    """Tabela do placar (R$ mi) e resumo, para o CLI."""
    if not r["linhas"]:
        print("sem dados")
        return
    df = pd.DataFrame(r["linhas"])[["data", "oficial", "tryd", "cesta", "n_ativos", "oficial_previsto"]]
    for c in ("oficial", "tryd", "cesta"):
        df[c] = df[c].astype(float).round(1)
    df["n_ativos"] = df["n_ativos"].map(lambda v: "-" if v is None or v != v else str(int(v)))
    print(df.to_string(index=False, na_rep="-"))
    print("\nresumo (R$ mi; vies = media de estimativa - oficial):")
    for nome in ("tryd", "cesta"):
        s = r["resumo"][nome]
        print(f"{nome:5s}: n={s['n']}  sinal_certo={s['sinal_certo']}/{s['n']}  postos={_txt(s['postos'])}  "
              f"erro_abs_medio={_txt(s['erro_abs_medio'], 1)}  vies={_txt(s['vies'], 1, True)}")
    d = r["resumo"]["dia_anterior"]
    print(f"regua do sinal do dia anterior do oficial: n={d['n']}  sinal_certo={d['sinal_certo']}/{d['n']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("comando", choices=("atualizar", "placar"))
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    a = ap.parse_args()
    if a.comando == "atualizar":
        print(f"{atualizar(a.db)} dia(s) gravado(s) em tryd_estrangeiro_dia")
    else:
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        try:
            _imprimir_placar(placar(con))
        finally:
            con.close()
