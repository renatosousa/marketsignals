"""Manutencao do banco: agregado permanente da fita, arquivo frio em Parquet, expurgo e backup do nucleo.

Tres rotinas, na ordem em que rodam todo dia as 18:40 (ver `diaria`):
  1. tryd_fita_1min  agregado por dia/ativo/minuto/grupo da fita do Tryd (coletor_tryd.py). E a unica tabela que
     sobrevive ao expurgo da fita crua.
  2. historico_parquet  as 6 tabelas brutas (tryd_fita, opcoes_cotacoes, niveis, eventos, snapshots, tryd_saldo) viram
     um Parquet por dia em dados/historico_parquet/<tabela>/ano_mes=AAAA-MM/dia=<dia>.parquet (zstd). O manifesto
     arquivo_frio guarda linhas e checksum de cada arquivo; so entra no manifesto o arquivo cujo checksum confere.
  3. backup_nucleo  copia todas as tabelas MENOS as 6 brutas e macro_cotacoes para dados/backups/book_core_AAAAMMDD.db.gz.

Regras de ouro:
  - O expurgo (DELETE dos dias ja no Parquet) so roda com `--apagar` explicito; o padrao e simular e imprimir. O codigo
    NUNCA roda DROP, VACUUM ou DELETE no banco real sem esse argumento. O VACUUM nao e chamado nem com --apagar:
    auto_vacuum = 0 no book.db, entao `PRAGMA incremental_vacuum` nao devolve espaco e o VACUUM completo trava o banco
    e exige os coletores parados (o expurgo so avisa).
  - Contra o banco real o modulo abre `file:...?mode=ro` para ler; a conexao de escrita so e aberta para gravar as
    tabelas novas (tryd_fita_1min, arquivo_frio) e o expurgo explicito.
  - `diaria`, `expurgar --apagar` e qualquer gravacao NAO devem ser rodados contra dados/book.db por quem so esta
    validando o codigo: use um banco sintetico (ver test_manutencao_banco.py).

Convencoes do banco (ver RESUMO_PROJETO.md): ts_ms = hora de Brasilia como epoch UTC; dia = ts_ms // 86_400_000;
minuto = minuto do dia (0 a 1439). Na fita, `num` anda de 10 em 10 por negocio, entao buracos medem o que a coleta
perdeu e a captura de um minuto e negocios lidos / ((max(num) - min(num)) / 10 + 1).

Uso:
  python manutencao_banco.py diaria [--db dados/book.db]
  python manutencao_banco.py servico MANUT <segundos> [--db ...] [--hora 18:40]
  python manutencao_banco.py expurgar [--dias-manter 10] [--apagar]
  python manutencao_banco.py estado [--db ...]
"""
import argparse
import bisect
import datetime
import gzip
import hashlib
import math
import os
import shutil
import sqlite3
import time
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import players

DIA_MS = 86_400_000
DB_PADRAO = "dados/book.db"
PARQUET_PADRAO = "dados/historico_parquet"
BACKUPS_PADRAO = "dados/backups"
CHUNK = 200_000
DIAS_MANTER = 10
BACKUPS_MANTER = 15

TABELAS_BRUTAS = ("tryd_fita", "opcoes_cotacoes", "niveis", "eventos", "snapshots", "tryd_saldo")
EXCLUIDAS_BACKUP = set(TABELAS_BRUTAS) | {"macro_cotacoes"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tryd_fita_1min (
    dia INTEGER NOT NULL, ativo TEXT NOT NULL, minuto INTEGER NOT NULL, grupo TEXT NOT NULL,
    vol_agr_compra REAL, vol_agr_venda REAL,
    vol_pas_compra REAL, vol_pas_venda REAL,
    vol_rlp REAL, n_negocios INTEGER, n_lotes_grandes INTEGER,
    captura REAL,
    PRIMARY KEY (dia, ativo, minuto, grupo)
);
CREATE TABLE IF NOT EXISTS arquivo_frio (
    tabela TEXT, dia INTEGER, arquivo TEXT, linhas INTEGER, checksum TEXT,
    ts_ms INTEGER, apagado_ms INTEGER, PRIMARY KEY (tabela, dia)
);
"""
COLUNAS_1MIN = ("dia", "ativo", "minuto", "grupo", "vol_agr_compra", "vol_agr_venda", "vol_pas_compra",
                "vol_pas_venda", "vol_rlp", "n_negocios", "n_lotes_grandes", "captura")

# Por tabela bruta: como filtrar o dia, qual soma cobre a chave e qual coluna numerica entra no checksum. As somas sao
# quantizadas para inteiro (ROUND(col * escala)) nos dois lados (SQLite e duckdb): somar REAL em ordens diferentes da
# diferenca de centavos, e um checksum que nao fecha nunca mais.
# A soma de ts_ms estouraria 2^63 nas tabelas grandes (29 mi de linhas x 1,8e12); usa-se ts_ms relativo ao dia, que
# testa a mesma coisa sem estourar.
ESPEC = {
    "tryd_fita": {"chave": "num", "coluna": "qtd", "escala": 1000},
    "opcoes_cotacoes": {"chave": "ts_ms % 86400000", "coluna": "strike", "escala": 1000},
    "niveis": {"chave": "ts_ms % 86400000", "coluna": "volume", "escala": 1},
    "eventos": {"chave": "ts_ms % 86400000", "coluna": "vol_antes", "escala": 1},
    "snapshots": {"chave": "ts_ms % 86400000", "coluna": "mid", "escala": 1000},
    "tryd_saldo": {"chave": "ts_ms % 86400000", "coluna": "qtd", "escala": 1000},
}

# Expressao do minuto dentro do dia a partir de `hora` (hh:mm:ss.mmm, ou "dd/mm/aaaa hh:mm:ss.mmm" quando o Tryd poe a
# data para separar negocios de dois dias).
MINUTO_SQL = ("CASE WHEN hora LIKE '__/__/____%' "
              "THEN CAST(substr(hora, 12, 2) AS INTEGER) * 60 + CAST(substr(hora, 15, 2) AS INTEGER) "
              "ELSE CAST(substr(hora, 1, 2) AS INTEGER) * 60 + CAST(substr(hora, 4, 2) AS INTEGER) END")


def _data_do_dia(dia):
    return datetime.date(1970, 1, 1) + datetime.timedelta(days=int(dia))


def _uri_ro(caminho):
    return f"file:{Path(caminho).resolve().as_posix()}?mode=ro"


def _abrir_ro(caminho):
    if not Path(caminho).exists():
        raise FileNotFoundError(f"banco nao encontrado: {caminho}")
    con = sqlite3.connect(_uri_ro(caminho), uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _abrir_rw(caminho):
    con = sqlite3.connect(str(caminho), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


def _caminho_parquet(tabela, dia, base=PARQUET_PADRAO):
    ano_mes = _data_do_dia(dia).strftime("%Y-%m")
    return Path(base) / tabela / f"ano_mes={ano_mes}" / f"dia={int(dia)}.parquet"


def _arquivo_manifesto(tabela, dia, base=PARQUET_PADRAO):
    """Caminho relativo a `base` (mover a pasta inteira nao quebra o manifesto)."""
    return _caminho_parquet(tabela, dia, base).relative_to(Path(base)).as_posix()


def _resolver_arquivo(arquivo, base=PARQUET_PADRAO):
    p = Path(arquivo)
    return p if p.is_absolute() else Path(base) / p


def _checksum(linhas, soma1, soma2):
    return hashlib.sha256(f"{int(linhas)}|{int(soma1)}|{int(soma2)}".encode("ascii")).hexdigest()


def _params_dia(tabela, dia):
    if tabela == "tryd_fita":
        return (int(dia),)
    return (int(dia) * DIA_MS, (int(dia) + 1) * DIA_MS)


def _where_sqlite(tabela, dia):
    if tabela == "tryd_fita":
        return "dia = ?"
    return "ts_ms >= ? AND ts_ms < ?"


def _sql_agregado(tabela, cast="INTEGER"):
    e = ESPEC[tabela]
    soma1 = f"COALESCE(SUM(CAST(ROUND(({e['chave']}) * 1) AS {cast})), 0)"
    soma2 = f"COALESCE(SUM(CAST(ROUND({e['coluna']} * {e['escala']}) AS {cast})), 0)"
    return f"SELECT COUNT(*), {soma1}, {soma2} FROM {tabela}"


def _soma_sqlite(con, tabela, dia):
    sql = _sql_agregado(tabela) + " WHERE " + _where_sqlite(tabela, dia)
    n, s1, s2 = con.execute(sql, _params_dia(tabela, dia)).fetchone()
    return int(n), int(s1), int(s2)


def _sql_literal(texto):
    return "'" + str(texto).replace("'", "''") + "'"


def _soma_parquet(caminho, tabela):
    e = ESPEC[tabela]
    soma1 = f"COALESCE(SUM(CAST(ROUND(({e['chave']}) * 1) AS BIGINT)), 0)"
    soma2 = f"COALESCE(SUM(CAST(ROUND({e['coluna']} * {e['escala']}) AS BIGINT)), 0)"
    sql = (f"SELECT COUNT(*), {soma1}, {soma2} "
           f"FROM read_parquet({_sql_literal(caminho)})")
    con = duckdb.connect()
    try:
        n, s1, s2 = con.execute(sql).fetchone()
    finally:
        con.close()
    return int(n), int(s1), int(s2)


def _plano_parquet(con, tabela):
    """(schema pyarrow, tipos pandas) das colunas da tabela, do PRAGMA. INTEGER anulavel vira Int64 (nullable) para
    NULL continuar NULL no Parquet, e nao NaN."""
    info = con.execute(f'PRAGMA table_info("{tabela}")').fetchall()
    if not info:
        raise ValueError(f"tabela desconhecida: {tabela}")
    campos, dtypes = [], {}
    for _, nome, tipo, notnull, _, _ in info:
        t = (tipo or "").upper()
        if "INT" in t:
            campos.append(pa.field(nome, pa.int64()))
            dtypes[nome] = "int64" if notnull else "Int64"
        elif any(x in t for x in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
            campos.append(pa.field(nome, pa.float64()))
            dtypes[nome] = "Float64"
        else:
            campos.append(pa.field(nome, pa.string()))
            dtypes[nome] = "string"
    return pa.schema(campos), dtypes


def _tabela_pyarrow(chunk, schema, dtypes):
    chunk = chunk.copy()
    for nome, dtype in dtypes.items():
        chunk[nome] = chunk[nome].astype(dtype)
    return pa.Table.from_pandas(chunk, schema=schema, preserve_index=False)


def _escrever_parquet(con, tabela, dia, caminho):
    """Le o dia em pedacos e escreve o Parquet. Devolve o numero de linhas escritas (0 = nada a fazer)."""
    schema, dtypes = _plano_parquet(con, tabela)
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".parcial")
    sql = f'SELECT * FROM "{tabela}" WHERE ' + _where_sqlite(tabela, dia)
    n = 0
    escritor = pq.ParquetWriter(tmp, schema, compression="zstd")
    try:
        for chunk in pd.read_sql_query(sql, con, params=_params_dia(tabela, dia), chunksize=CHUNK):
            if len(chunk):
                escritor.write_table(_tabela_pyarrow(chunk, schema, dtypes))
                n += len(chunk)
    except Exception:
        escritor.close()
        tmp.unlink(missing_ok=True)
        raise
    escritor.close()
    if n == 0:
        tmp.unlink(missing_ok=True)
        return 0
    os.replace(tmp, caminho)
    return n


def _filtro_hora(dia):
    """So negocios do dia (ver players._SO_DO_DIA): com dois dias na janela o Tryd poe a data e negocio de outro dia
    nao entra; com a janela so com os negocios de ontem a hora vem depois da captura (> 2 min de folga)."""
    d = _data_do_dia(dia).strftime("%d/%m/%Y")
    return ("hora IS NOT NULL AND ("
            f"(hora LIKE '__/__/____%' AND substr(hora, 1, 10) = '{d}') OR "
            "(hora GLOB '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*' "
            "AND substr(hora, 1, 8) <= time(ts_ms / 1000 + 120, 'unixepoch')))")


def _percentil(valores, pesos, q=95):
    """Percentil ponderado com interpolacao linear (o mesmo criterio do numpy.percentile), sem expandir a amostra:
    pos = (n - 1) * q / 100 sobre a amostra ordenada implicitamente pelos pesos."""
    if not valores:
        return None
    pares = sorted(zip(valores, pesos))
    v = [x for x, _ in pares]
    cum, total = [], 0
    for _, w in pares:
        total += w
        cum.append(total)
    if total <= 0:
        return None

    def valor_no_rank(k):
        return float(v[bisect.bisect_right(cum, k)])

    pos = (total - 1) * q / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return valor_no_rank(lo)
    frac = pos - lo
    return valor_no_rank(lo) * (1 - frac) + valor_no_rank(hi) * frac


def _limiar_lote(con, dia, filtro):
    """Percentil 95 da qtd do ativo no dia (ponderado pelo numero de negocios), ou None sem qtd."""
    hist = con.execute(f"SELECT qtd, COUNT(*) FROM tryd_fita WHERE dia=? AND {filtro} AND qtd IS NOT NULL "
                       "GROUP BY qtd", (int(dia),)).fetchall()
    return _percentil([r[0] for r in hist], [r[1] for r in hist], 95)


def agregar_fita_dia(con_ro, dia):
    """Agrega a fita de um dia inteiro em linhas (dia, ativo, minuto, grupo) prontas para tryd_fita_1min.

    Regras por negocio (o minuto vem de `hora`, nao do ts_ms de captura):
      Comprador  grupo do comprador -> vol_agr_compra; grupo do vendedor -> vol_pas_venda.
      Vendedor   grupo do vendedor -> vol_agr_venda; grupo do comprador -> vol_pas_compra.
      RLP        qtd -> vol_rlp do grupo do comprador (as duas pontas sao a mesma corretora).
      Direto/Leilao  so n_negocios.
    n_negocios e n_lotes_grandes contam o negocio nos grupos das DUAS pontas (o grupo participou dele), uma vez cada.
    lote grande = qtd >= percentil 95 da qtd do ativo no dia. captura = negocios lidos / ((max(num)-min(num))/10+1)
    no minuto, NULL com menos de 2 negocios (igual em todas as linhas do minuto).
    """
    dia = int(dia)
    filtro = _filtro_hora(dia)
    ativos = [r[0] for r in con_ro.execute(
        f"SELECT DISTINCT ativo FROM tryd_fita WHERE dia=? AND {filtro} ORDER BY 1", (dia,))]
    acc = {}   # (ativo, minuto, grupo) -> [5 volumes, n_negocios, n_lotes_grandes]
    por_minuto = {}  # (ativo, minuto) -> [n, num_min, num_max]
    for ativo in ativos:
        limiar = _limiar_lote(con_ro, dia, f"ativo = {_sql_literal(ativo)} AND {filtro}")
        grandes = "SUM(CASE WHEN qtd >= ? THEN 1 ELSE 0 END)" if limiar is not None else "0"
        params = ([float(limiar)] if limiar is not None else []) + [dia, ativo]
        sql = (f"SELECT {MINUTO_SQL} AS minuto, comprador, vendedor, agressor, "
               f"COALESCE(SUM(qtd), 0) AS qtd, COUNT(*) AS n, {grandes} AS n_grandes, "
               f"MIN(num) AS num_min, MAX(num) AS num_max "
               f"FROM tryd_fita WHERE dia=? AND ativo=? AND {filtro} "
               "GROUP BY 1, 2, 3, 4")
        for minuto, comp, vend, agr, qtd, n, n_grandes, nmin, nmax in con_ro.execute(sql, params):
            minuto = int(minuto)
            g_comp, g_vend = players.grupo(comp), players.grupo(vend)
            qtd = float(qtd or 0.0)

            def soma(grupo, campo, valor):
                linha = acc.setdefault((ativo, minuto, grupo), [0.0, 0.0, 0.0, 0.0, 0.0, 0, 0])
                linha[campo] += valor

            if agr == "Comprador":
                soma(g_comp, 0, qtd)   # vol_agr_compra
                soma(g_vend, 3, qtd)   # vol_pas_venda
            elif agr == "Vendedor":
                soma(g_vend, 1, qtd)   # vol_agr_venda
                soma(g_comp, 2, qtd)   # vol_pas_compra
            elif agr == "RLP":
                soma(g_comp, 4, qtd)   # vol_rlp
            for g in {g_comp, g_vend}:
                linha = acc.setdefault((ativo, minuto, g), [0.0, 0.0, 0.0, 0.0, 0.0, 0, 0])
                linha[5] += int(n)
                linha[6] += int(n_grandes)
            pm = por_minuto.setdefault((ativo, minuto), [0, nmin, nmax])
            pm[0] += int(n)
            pm[1] = min(pm[1], nmin) if pm[1] is not None else nmin
            pm[2] = max(pm[2], nmax) if pm[2] is not None else nmax

    linhas = []
    for (ativo, minuto, grupo), v in acc.items():
        n, nmin, nmax = por_minuto[(ativo, minuto)]
        captura = (n / ((nmax - nmin) // 10 + 1)) if n >= 2 else None
        linhas.append((dia, ativo, minuto, grupo, *v[:5], v[5], v[6], captura))
    df = pd.DataFrame(linhas, columns=COLUNAS_1MIN)
    if df.empty:
        df = df.astype({"dia": "int64", "minuto": "int64", "n_negocios": "int64", "n_lotes_grandes": "int64"})
        return df
    for c in ("vol_agr_compra", "vol_agr_venda", "vol_pas_compra", "vol_pas_venda", "vol_rlp"):
        df[c] = df[c].astype(float)
    return df.sort_values(["ativo", "minuto", "grupo"]).reset_index(drop=True)


def gravar_fita_1min(con_rw, df):
    """INSERT OR REPLACE das linhas de agregar_fita_dia numa transacao. Devolve quantas linhas gravou."""
    con_rw.executescript(SCHEMA)
    if df is None or df.empty:
        return 0
    linhas = []
    for r in df[list(COLUNAS_1MIN)].itertuples(index=False):
        captura = None if r.captura is None or pd.isna(r.captura) else float(r.captura)
        linhas.append((int(r.dia), str(r.ativo), int(r.minuto), str(r.grupo),
                       float(r.vol_agr_compra), float(r.vol_agr_venda), float(r.vol_pas_compra),
                       float(r.vol_pas_venda), float(r.vol_rlp), int(r.n_negocios),
                       int(r.n_lotes_grandes), captura))
    with con_rw:
        con_rw.executemany(f"INSERT OR REPLACE INTO tryd_fita_1min ({','.join(COLUNAS_1MIN)}) "
                           f"VALUES ({','.join('?' * len(COLUNAS_1MIN))})", linhas)
    return len(linhas)


def exportar_dia(db_path, tabela, dia, base=PARQUET_PADRAO, verificar=True):
    """Exporta um dia de uma tabela bruta para Parquet zstd, confere o checksum e grava o manifesto.

    Idempotente: se o dia ja esta no manifesto e o checksum do Parquet confere, nao faz nada. O arquivo so entra no
    manifesto se o checksum relido pelo duckdb for igual ao calculado no SQLite; se nao bater, o arquivo parcial e
    apagado e nada e gravado.
    Retorna {"tabela", "dia", "acao", "linhas", "arquivo"} com acao em exportado/pulado/vazio/falhou.
    """
    if tabela not in ESPEC:
        raise ValueError(f"tabela nao e bruta: {tabela}")
    if not Path(db_path).exists():
        raise FileNotFoundError(f"banco nao encontrado: {db_path}")
    dia = int(dia)
    caminho = _caminho_parquet(tabela, dia, base)
    relativo = _arquivo_manifesto(tabela, dia, base)

    con_rw = _abrir_rw(db_path)
    try:
        atual = con_rw.execute("SELECT linhas, checksum, apagado_ms FROM arquivo_frio WHERE tabela=? AND dia=?",
                               (tabela, dia)).fetchone()
        if atual and caminho.exists():
            n, s1, s2 = _soma_parquet(caminho, tabela)
            if _checksum(n, s1, s2) == atual[1]:
                return {"tabela": tabela, "dia": dia, "acao": "pulado", "linhas": int(n), "arquivo": str(caminho)}
            if atual[2] is not None:
                print(f"[manutencao] {tabela} dia {dia}: Parquet nao confere e o dia ja foi apagado; mantendo",
                      flush=True)
                return {"tabela": tabela, "dia": dia, "acao": "falhou", "linhas": 0, "arquivo": str(caminho)}
        con_ro = _abrir_ro(db_path)
        try:
            n_sql, s1, s2 = _soma_sqlite(con_ro, tabela, dia)
            if n_sql == 0:
                return {"tabela": tabela, "dia": dia, "acao": "vazio", "linhas": 0, "arquivo": str(caminho)}
            n = _escrever_parquet(con_ro, tabela, dia, caminho)
        finally:
            con_ro.close()
        alvo = _checksum(n_sql, s1, s2)
        if verificar:
            n_pq, p1, p2 = _soma_parquet(caminho, tabela)
            if (n_pq, p1, p2) != (n_sql, s1, s2) or _checksum(n_pq, p1, p2) != alvo:
                caminho.unlink(missing_ok=True)
                print(f"[manutencao] {tabela} dia {dia}: checksum nao confere "
                      f"(sqlite {n_sql}/{s1}/{s2}, parquet {n_pq}/{p1}/{p2}); arquivo apagado", flush=True)
                return {"tabela": tabela, "dia": dia, "acao": "falhou", "linhas": 0, "arquivo": str(caminho)}
        with con_rw:
            con_rw.execute("INSERT OR REPLACE INTO arquivo_frio (tabela, dia, arquivo, linhas, checksum, ts_ms, "
                           "apagado_ms) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                           (tabela, dia, relativo, int(n_sql), alvo, int(time.time() * 1000)))
        return {"tabela": tabela, "dia": dia, "acao": "exportado", "linhas": int(n), "arquivo": str(caminho)}
    finally:
        con_rw.close()


def _tabela_existe(con, tabela):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tabela,)).fetchone())


def _dias_com_dado(con, tabela):
    if not _tabela_existe(con, tabela):
        return []
    if tabela == "tryd_fita":
        return [int(r[0]) for r in con.execute("SELECT DISTINCT dia FROM tryd_fita ORDER BY 1")]
    return [int(r[0]) for r in con.execute(
        f"SELECT DISTINCT ts_ms / {DIA_MS} FROM {tabela} WHERE ts_ms IS NOT NULL ORDER BY 1")]


def exportar_pendentes(db_path, dias_manter=DIAS_MANTER, base=PARQUET_PADRAO, tabelas=TABELAS_BRUTAS):
    """Exporta todo pregao com mais de `dias_manter` pregoes COM DADO de idade (nao dias corridos).

    Para cada tabela, os dias com dado sao ordenados; os `dias_manter` mais novos ficam no SQLite e os anteriores vao
    para o Parquet. Retorna a lista de resultados de exportar_dia.
    """
    con = _abrir_ro(db_path)
    try:
        plano = {t: _dias_com_dado(con, t) for t in tabelas if _tabela_existe(con, t)}
    finally:
        con.close()
    saida = []
    for tabela, dias in plano.items():
        corte = len(dias) - max(0, int(dias_manter))
        for dia in dias[:max(0, corte)]:
            saida.append(exportar_dia(db_path, tabela, dia, base))
    return saida


def ler_historico(tabela, dia_ini, dia_fim, colunas=None, filtro_sql=None, db_path=DB_PADRAO,
                  base=PARQUET_PADRAO):
    """DataFrame com os dias de [dia_ini, dia_fim], juntando Parquet frio e SQLite sem duplicar.

    O manifesto decide a fonte de cada dia: dia com apagado_ms preenchido vem do Parquet; os outros do SQLite (mesmo
    que ja exista Parquet, porque o dado ainda esta no banco). `colunas` = lista de nomes; `filtro_sql` = condicao
    aplicada nos dois lados (ex.: "ativo='WINZ26'").
    """
    if tabela not in ESPEC:
        raise ValueError(f"tabela nao e bruta: {tabela}")
    cols = ", ".join(colunas) if colunas else "*"
    con = _abrir_ro(db_path)
    try:
        manifesto = {(r[0], int(r[1])): r[2] for r in con.execute(
            "SELECT tabela, dia, apagado_ms FROM arquivo_frio WHERE tabela=? AND dia>=? AND dia<=?",
            (tabela, int(dia_ini), int(dia_fim)))}
    except sqlite3.OperationalError:
        manifesto = {}
    finally:
        con.close()
    partes = []
    for dia in range(int(dia_ini), int(dia_fim) + 1):
        if manifesto.get((tabela, dia)) is not None:
            # dia apagado do SQLite: vem do Parquet
            con = _abrir_ro(db_path)
            try:
                arquivo = con.execute("SELECT arquivo FROM arquivo_frio WHERE tabela=? AND dia=?",
                                      (tabela, dia)).fetchone()
            finally:
                con.close()
            caminho = _resolver_arquivo(arquivo[0], base) if arquivo else None
            if caminho is None or not Path(caminho).exists():
                print(f"[manutencao] {tabela} dia {dia}: Parquet do manifesto nao encontrado; pulando", flush=True)
                continue
            sql = f"SELECT {cols} FROM read_parquet({_sql_literal(caminho)})"
            if filtro_sql:
                sql += f" WHERE {filtro_sql}"
            ddb = duckdb.connect()
            try:
                partes.append(ddb.execute(sql).df())
            finally:
                ddb.close()
        else:
            con = _abrir_ro(db_path)
            try:
                sql = f"SELECT {cols} FROM {tabela} WHERE {_where_sqlite(tabela, dia)}"
                if filtro_sql:
                    sql += f" AND ({filtro_sql})"
                partes.append(pd.read_sql_query(sql, con, params=_params_dia(tabela, dia)))
            finally:
                con.close()
    if not partes:
        return pd.DataFrame(columns=colunas)
    return pd.concat(partes, ignore_index=True)


def expurgar(db_path, dias_manter=DIAS_MANTER, apagar=False, base=PARQUET_PADRAO):
    """Apaga do SQLite os dias ja exportados e conferidos, mantendo os `dias_manter` pregoes mais novos.

    Sem `apagar=True` so imprime o que apagaria (linhas e tamanho do Parquet). Com `apagar=True`, reconfere o checksum
    do Parquet e, so se bater, faz o DELETE do dia numa transacao e marca apagado_ms. Nunca chama VACUUM: no book.db
    (auto_vacuum = 0, page_size 4096) o incremental_vacuum nao devolve espaco e o VACUUM completo trava o banco e exige
    os coletores parados; o fim imprime esse aviso. NAO usar contra o banco real sem o dono autorizar.
    Retorna a lista de {"tabela", "dia", "linhas", "bytes", "acao"}.
    """
    con = _abrir_rw(db_path)
    saida = []
    try:
        tabelas = [r[0] for r in con.execute("SELECT DISTINCT tabela FROM arquivo_frio ORDER BY 1")]
        for tabela in tabelas:
            if tabela not in ESPEC:
                continue
            manifesto = {int(r[0]): r[1] for r in con.execute(
                "SELECT dia, apagado_ms FROM arquivo_frio WHERE tabela=?", (tabela,))}
            # idade contada nos pregoes COM DADO: SQLite (ainda quente) + manifest (frio). Sem isso os dias ja
            # exportados seriam sempre os "mais novos" do manifesto e nunca expirariam.
            todos = sorted(set(_dias_com_dado(con, tabela)) | set(manifesto))
            pos = {dia: i for i, dia in enumerate(todos)}
            corte = len(todos) - max(0, int(dias_manter))
            alvos = [dia for dia in todos
                     if dia in manifesto and manifesto[dia] is None and pos[dia] < corte]
            for dia in alvos:
                linha = con.execute("SELECT arquivo, linhas, checksum FROM arquivo_frio WHERE tabela=? AND dia=?",
                                    (tabela, dia)).fetchone()
                if not linha:
                    continue
                arquivo, linhas, esperado = linha
                caminho = _resolver_arquivo(arquivo, base)
                if not caminho.exists():
                    print(f"[manutencao] {tabela} dia {dia}: Parquet sumiu; nao apago", flush=True)
                    continue
                n, s1, s2 = _soma_parquet(caminho, tabela)
                if _checksum(n, s1, s2) != esperado:
                    print(f"[manutencao] {tabela} dia {dia}: checksum do Parquet nao confere; nao apago", flush=True)
                    continue
                bytes_ = caminho.stat().st_size
                if not apagar:
                    print(f"[seco] apagaria {tabela} dia {dia}: {int(linhas)} linhas, "
                          f"{bytes_ / 1e6:.1f} MB no Parquet", flush=True)
                    saida.append({"tabela": tabela, "dia": dia, "linhas": int(linhas), "bytes": bytes_,
                                  "acao": "simulado"})
                    continue
                with con:
                    con.execute(f"DELETE FROM {tabela} WHERE {_where_sqlite(tabela, dia)}", _params_dia(tabela, dia))
                    con.execute("UPDATE arquivo_frio SET apagado_ms=? WHERE tabela=? AND dia=?",
                                (int(time.time() * 1000), tabela, dia))
                print(f"[expurgo] apagado {tabela} dia {dia}: {int(linhas)} linhas "
                      f"({bytes_ / 1e6:.1f} MB no Parquet)", flush=True)
                saida.append({"tabela": tabela, "dia": dia, "linhas": int(linhas), "bytes": bytes_,
                              "acao": "apagado"})
    finally:
        con.close()
    if apagar and saida:
        print("[expurgo] espaco em disco NAO diminuiu ainda: e preciso VACUUM com os coletores parados "
              "(auto_vacuum = 0 no book.db; incremental_vacuum nao devolve espaco).", flush=True)
    return saida


def backup_nucleo(db_path, pasta=BACKUPS_PADRAO, manter=BACKUPS_MANTER):
    """Copia todas as tabelas MENOS as 6 brutas e macro_cotacoes para book_core_AAAAMMDD.db.gz.

    Copia tabela a tabela por ATTACH (somente leitura) + INSERT SELECT, sem as excluidas; roda PRAGMA integrity_check,
    comprime com gzip, apaga o .db intermediario e mantem os `manter` arquivos mais recentes. Imprime tamanho e tempo.
    """
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    inicio = time.time()
    nome = f"book_core_{datetime.datetime.now():%Y%m%d}.db"
    tmp = pasta / nome
    if tmp.exists():
        tmp.unlink()
    # copia so as tabelas do nucleo (ATTACH em modo leitura + INSERT SELECT): a API backup copiaria os 15+ GB das
    # brutas para depois apaga-las
    uri = "file:" + Path(db_path).resolve().as_posix() + "?mode=ro"
    copia = sqlite3.connect("file:" + tmp.resolve().as_posix(), uri=True)  # uri=True para o ATTACH aceitar ?mode=ro
    try:
        copia.execute("ATTACH DATABASE ? AS src", (uri,))
        defs = copia.execute("SELECT name, sql FROM src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                             "AND sql IS NOT NULL").fetchall()
        tabelas = [n for n, _ in defs]
        for nome_t, sql in defs:
            if nome_t in EXCLUIDAS_BACKUP:
                continue
            copia.execute(sql)
            copia.execute(f'INSERT INTO main."{nome_t}" SELECT * FROM src."{nome_t}"')
            copia.commit()
        copia.execute("DETACH DATABASE src")
        ok = copia.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copia.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"integrity_check da copia falhou: {ok}")
    destino = pasta / (nome + ".gz")
    with open(tmp, "rb") as f, gzip.open(destino, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g)
    tmp.unlink(missing_ok=True)

    antigos = sorted(pasta.glob("book_core_*.db.gz"), key=lambda p: p.name)
    for velho in antigos[:-max(0, int(manter))] if int(manter) > 0 else antigos:
        velho.unlink(missing_ok=True)
    segundos = time.time() - inicio
    tamanho = destino.stat().st_size
    print(f"[backup] {destino.name}: {tamanho / 1e6:.1f} MB em {segundos:.1f} s "
          f"({len(tabelas) - len(set(tabelas) & EXCLUIDAS_BACKUP)} tabelas)", flush=True)
    return {"arquivo": str(destino), "bytes": tamanho, "segundos": segundos}


def diaria(db_path=DB_PADRAO, base=PARQUET_PADRAO, dias_manter=DIAS_MANTER, pasta_backups=BACKUPS_PADRAO,
           manter=BACKUPS_MANTER):
    """Rotina das 18:40: agrega a fita dos dias faltantes, arquiva os pregoes velhos e faz o backup do nucleo.

    NAO expurga (o expurgo e manual, com --apagar). Grava no banco real: nao rodar contra dados/book.db sem autorizacao.
    """
    con = _abrir_rw(db_path)
    try:
        dias = [int(r[0]) for r in con.execute(
            "SELECT DISTINCT dia FROM tryd_fita WHERE dia NOT IN (SELECT dia FROM tryd_fita_1min) ORDER BY 1")]
        agregados = 0
        for dia in dias:
            df = agregar_fita_dia(con, dia)
            n = gravar_fita_1min(con, df)
            agregados += n
            print(f"[diaria] tryd_fita_1min dia {dia}: {n} linhas", flush=True)
    finally:
        con.close()
    exportados = exportar_pendentes(db_path, dias_manter, base)
    novos = [r for r in exportados if r["acao"] == "exportado"]
    print(f"[diaria] arquivo frio: {len(novos)} dias exportados, "
          f"{len([r for r in exportados if r['acao'] == 'pulado'])} ja estavam", flush=True)
    backup = backup_nucleo(db_path, pasta_backups, manter)
    return {"dias_agregados": dias, "linhas_1min": agregados, "exportados": novos, "backup": backup}


def servico(db_path=DB_PADRAO, segundos=0, hora="18:40", base=PARQUET_PADRAO, dias_manter=DIAS_MANTER,
            pasta_backups=BACKUPS_PADRAO, manter=BACKUPS_MANTER):
    """Loop para o rodar_coleta: espera `hora`, roda `diaria` uma vez por dia e dorme ate o fim (nao sai antes, para o
    supervisor nao reiniciar em loop)."""
    fim = time.time() + max(0.0, float(segundos))
    alvo = datetime.time.fromisoformat(hora)
    ultimo = None
    print(f"[servico] manutencao diaria as {hora}; fim em {max(0.0, float(segundos)):.0f} s", flush=True)
    while time.time() < fim:
        agora = datetime.datetime.now()
        if agora.weekday() < 5 and agora.time() >= alvo and ultimo != agora.date():
            try:
                diaria(db_path, base, dias_manter, pasta_backups, manter)
            except Exception as e:  # erro de um dia nao derruba o servico
                print(f"[servico] diaria falhou: {e.__class__.__name__}: {e}", flush=True)
            ultimo = agora.date()
        time.sleep(min(30.0, max(0.0, fim - time.time())))
    print("[servico] fim do horario; saindo", flush=True)


def _tamanho_estimado(con, tabela, n_linhas):
    """Bytes estimados por amostra de 20 mil linhas (nao ha dbstat compilado neste SQLite)."""
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{tabela}")')]
    expr = " + ".join(f"length(quote({c})) + 1" for c in cols)
    n, total = con.execute(f"SELECT COUNT(*), COALESCE(SUM(tam), 0) FROM "
                           f"(SELECT {expr} AS tam FROM {tabela} LIMIT 20000)").fetchone()
    if not n:
        return 0
    return int(n_linhas * (total / n + 12))


def estado(db_path=DB_PADRAO, base=PARQUET_PADRAO):
    """Imprime tamanho estimado por tabela bruta e dias no SQLite x no Parquet (somente leitura)."""
    con = _abrir_ro(db_path)
    print(f"banco {db_path}: {Path(db_path).stat().st_size / 1e9:.1f} GB", flush=True)
    print(f"{'tabela':<18}{'linhas sql':>14}{'dias sql':>10}{'~GB sql':>10}{'dias parq':>11}"
          f"{'linhas parq':>15}{'GB parq':>9}", flush=True)
    stats = {}
    try:
        tem_manifesto = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arquivo_frio'").fetchone())
        for tabela in TABELAS_BRUTAS:
            if not _tabela_existe(con, tabela):
                print(f"{tabela:<18}{'-':>14}{'-':>10}{'-':>10}{'-':>11}{'-':>15}{'-':>9}", flush=True)
                continue
            if tabela == "tryd_fita":
                n, dmin, dmax, dias = con.execute(
                    "SELECT COUNT(*), MIN(dia), MAX(dia), COUNT(DISTINCT dia) FROM tryd_fita").fetchone()
            else:
                n, dmin, dmax, dias = con.execute(
                    f"SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms), COUNT(DISTINCT ts_ms / {DIA_MS}) "
                    f"FROM {tabela}").fetchone()
            if tem_manifesto:
                linhas_pq, dias_pq = con.execute(
                    "SELECT COALESCE(SUM(linhas), 0), COUNT(*) FROM arquivo_frio WHERE tabela=?",
                    (tabela,)).fetchone()
            else:
                linhas_pq, dias_pq = 0, 0
            pasta = Path(base) / tabela
            bytes_pq = sum(p.stat().st_size for p in pasta.rglob("*.parquet")) if pasta.exists() else 0
            bytes_sql = _tamanho_estimado(con, tabela, int(n))
            print(f"{tabela:<18}{int(n):>14}{int(dias):>10}{bytes_sql / 1e9:>10.2f}{int(dias_pq):>11}"
                  f"{int(linhas_pq):>15}{bytes_pq / 1e9:>9.2f}", flush=True)
            stats[tabela] = {"linhas_sql": int(n), "dias_sql": int(dias), "dias_parquet": int(dias_pq),
                             "linhas_parquet": int(linhas_pq), "bytes_sql_estimado": bytes_sql,
                             "bytes_parquet": bytes_pq}
    finally:
        con.close()
    return stats


def main():
    import sys
    # chamado pelo supervisor (rodar_coleta.py): "manutencao_banco.py MANUT <segundos> --db ..." = servico
    if len(sys.argv) > 1 and sys.argv[1] not in ("diaria", "estado", "servico", "expurgar", "-h", "--help"):
        sys.argv.insert(1, "servico")
    ap = argparse.ArgumentParser(description="Manutencao do book.db: agregado da fita, Parquet frio, expurgo e backup.")
    sub = ap.add_subparsers(dest="comando", required=True)

    for nome in ("diaria", "estado"):
        p = sub.add_parser(nome)
        p.add_argument("--db", default=DB_PADRAO)
        p.add_argument("--parquet", default=PARQUET_PADRAO)

    p = sub.add_parser("servico")
    p.add_argument("grupo", nargs="?", default="MANUT")
    p.add_argument("segundos", nargs="?", type=float, default=0.0)
    p.add_argument("--db", default=DB_PADRAO)
    p.add_argument("--parquet", default=PARQUET_PADRAO)
    p.add_argument("--hora", default="18:40")
    p.add_argument("--dias-manter", type=int, default=DIAS_MANTER)
    p.add_argument("--backups", default=BACKUPS_PADRAO)
    p.add_argument("--manter", type=int, default=BACKUPS_MANTER)

    p = sub.add_parser("expurgar")
    p.add_argument("--db", default=DB_PADRAO)
    p.add_argument("--parquet", default=PARQUET_PADRAO)
    p.add_argument("--dias-manter", type=int, default=DIAS_MANTER)
    p.add_argument("--apagar", action="store_true")

    args = ap.parse_args()
    if args.comando == "diaria":
        diaria(args.db, args.parquet)
    elif args.comando == "servico":
        servico(args.db, args.segundos, args.hora, args.parquet, args.dias_manter, args.backups, args.manter)
    elif args.comando == "expurgar":
        expurgar(args.db, args.dias_manter, args.apagar, args.parquet)
    elif args.comando == "estado":
        estado(args.db, args.parquet)


if __name__ == "__main__":
    main()
