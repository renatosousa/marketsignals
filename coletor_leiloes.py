"""Resultados dos leiloes do Tesouro Nacional (LTN, NTN-F, NTN-B, LFT...) -> SQLite.

Fonte publica, sem login (conferida em 04/11/2026): dataset ds013 do Tesouro Transparente, planilhas do SISWEB
  historico completo (xls antigo, 27 abas "Ano 2026" ... "Ano 2000", ~5 MB):
    https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/todos/desc/historico-leiloes-todos.xls
  um ano (xlsx; 2011-2018 em xls; o do ano novo pode dar 404 em janeiro):
    https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/unico/desc/historico-leiloes-{ANO}.xlsx
  O cabecalho nao fica numa linha fixa: o parser procura "Data do leilao" e mapeia as colunas pelo nome.
  Datas em serial do Excel, taxas em fracao decimal (0,078280 = 7,8280% a.a.; na LFT e agio/desagio).

O que o arquivo NAO traz: volume de propostas (nao ha bid-to-cover; ver leiloes.py: absorcao = venda / oferta).
"Venda para Bacen" e quantidade ADICIONAL a "Venda" (ao mesmo PU), nao uma parte dela: em 793 das 1.035 linhas com
Bacen a quantidade supera a venda ao mercado.

Tabelas (data = AAAA-MM-DD do leilao; quantidades em titulos; financeiro em R$; taxas em fracao):
  leilao_resultados  uma linha por (data, titulo, tipo, volta, vencimento, liquidacao, seq); hash detecta revisao
                     do Tesouro; visto_em = quando esta copia viu a linha pela primeira vez (mede a latencia de
                     publicacao); seq so existe para linhas que colidem em tudo menos nos valores (4 pares em 2009)
  leilao_revisoes    antes/depois (JSON) toda vez que uma linha ja gravada muda
  leilao_metricas / leilao_alertas: ver leiloes.py

Uso: python coletor_leiloes.py backfill [--arquivo CAMINHO]   carrega o historico completo (baixa o todos.xls)
     python coletor_leiloes.py update [--ano 2026]            baixa o ano, grava o que mudou, recalcula metricas/alertas
     python coletor_leiloes.py status                         resumo do que ha no banco
     python coletor_leiloes.py LEILOES SEGUNDOS               supervisor (rodar_coleta.py): ver `supervisor`
     [--db dados/book.db]
Saida: 0 ok | 1 erro | 2 o ano pedido ainda nao tem arquivo (404).
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import requests

import banco

AQUI = Path(__file__).parent
BRUTO = AQUI / "dados" / "raw" / "leiloes"
URL_TODOS = "https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/todos/desc/historico-leiloes-todos.xls"
URL_ANO = "https://sisweb.tesouro.gov.br/apex/cosis/rleiloes/arquivos/unico/desc/historico-leiloes-{ano}.{ext}"
UA = {"User-Agent": "Mozilla/5.0 (coletor_leiloes; uso pessoal)"}
TIMEOUT = (20, 120)
TENTATIVAS = 4
ESPERA = (5, 20, 60)

# cabecalho em portugues (sem acento, minusculo) -> campo
CABECALHO = {
    "data do leilao": "data", "titulo": "titulo", "tipo de leilao": "tipo", "volta": "volta",
    "data de liquidacao": "liquidacao", "data de vencimento": "vencimento", "oferta": "oferta",
    "taxa media": "tx_media", "taxa de corte": "tx_corte", "venda": "venda", "financeiro (r$)": "financeiro",
    "venda para bacen": "venda_bacen", "financeiro para bacen (r$)": "financeiro_bacen",
}
TIPOS = {"venda": "VENDA", "compra": "COMPRA", "troca": "TROCA", "extra compra": "EXTRA_COMPRA", "extra venda": "EXTRA_VENDA"}
CAMPOS_VALOR = ("oferta", "tx_media", "tx_corte", "venda", "financeiro", "venda_bacen", "financeiro_bacen")

# horario de Brasilia sem depender de tzdata
BRT = timezone(timedelta(hours=-3))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leilao_resultados (
    id INTEGER PRIMARY KEY,
    data TEXT NOT NULL, titulo TEXT NOT NULL, tipo TEXT NOT NULL, volta INTEGER NOT NULL,
    liquidacao TEXT NOT NULL DEFAULT '', vencimento TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 1,
    oferta REAL, tx_media REAL, tx_corte REAL, venda REAL, financeiro REAL, venda_bacen REAL, financeiro_bacen REAL,
    arquivo TEXT, aba TEXT, linha INTEGER, visto_em TEXT NOT NULL, atualizado_em TEXT NOT NULL, hash TEXT NOT NULL,
    UNIQUE (data, titulo, tipo, volta, vencimento, liquidacao, seq)
);
CREATE INDEX IF NOT EXISTS ix_leilao_res_data ON leilao_resultados (data);
CREATE INDEX IF NOT EXISTS ix_leilao_res_titulo ON leilao_resultados (titulo, tipo, volta, vencimento, data);
CREATE TABLE IF NOT EXISTS leilao_revisoes (
    id INTEGER PRIMARY KEY, resultado_id INTEGER NOT NULL, em TEXT NOT NULL, antes TEXT NOT NULL, depois TEXT NOT NULL
);
"""


def agora_br():
    return datetime.now(timezone.utc).astimezone(BRT).replace(tzinfo=None)


# ------------------------------------------------------------------ download
def baixar(url):
    """bytes do arquivo; None em 404 (ainda nao publicado). Tenta de novo em erro de rede/5xx/429."""
    ultimo = None
    for i in range(TENTATIVAS):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code == 404:
                return None
            if r.status_code in (408, 429) or r.status_code >= 500:
                r.raise_for_status()
            r.raise_for_status()
            if "html" in (r.headers.get("content-type") or "").lower() or r.content[:15].lower().startswith(b"<!doctype"):
                raise RuntimeError(f"o servidor devolveu uma pagina HTML em vez da planilha ({url})")
            return r.content
        except requests.HTTPError as e:
            cod = e.response.status_code if e.response is not None else 0
            if 400 <= cod < 500 and cod not in (408, 429):
                raise
            ultimo = e
        except (requests.ConnectionError, requests.Timeout, RuntimeError) as e:
            ultimo = e
        if i < TENTATIVAS - 1:
            time.sleep(ESPERA[min(i, len(ESPERA) - 1)])
    raise ultimo


def baixar_ano(ano):
    """(nome, bytes) do arquivo do ano: .xlsx e, se nao existir, .xls (2011-2018 so existem em xls). None = 404 nos dois."""
    for ext in ("xlsx", "xls"):
        conteudo = baixar(URL_ANO.format(ano=ano, ext=ext))
        if conteudo is not None:
            return f"historico-leiloes-{ano}.{ext}", conteudo
    return None


def guardar_bruto(nome, conteudo):
    """Copia da planilha em dados/raw/leiloes (a ultima de cada nome; o historico de revisoes fica no banco)."""
    BRUTO.mkdir(parents=True, exist_ok=True)
    (BRUTO / nome).write_bytes(conteudo)


# ------------------------------------------------------------------ parser
def sem_acento(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def como_data(v):
    """datetime/date, serial do Excel (float) ou 'DD/MM/AAAA' -> 'AAAA-MM-DD'; vazio ou ilegivel -> None."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1:
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date().isoformat()
    if isinstance(v, str) and re.fullmatch(r"\d\d/\d\d/\d{4}", v.strip()):
        d, m, a = v.strip().split("/")
        return f"{a}-{m}-{d}"
    return None


def como_num(v):
    """float; vazio, 'null', '-' e texto -> None (NULL, nunca 0)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    s = str(v).strip().replace(".", "").replace(",", ".") if "," in str(v) else str(v).strip()
    try:
        return float(s)
    except ValueError:
        return None


def como_volta(v):
    """'1.ª volta' / '2.ª volta' -> 1 / 2."""
    m = re.match(r"\s*(\d)", str(v or ""))
    return int(m.group(1)) if m else None


def _abas(conteudo, nome):
    """Gera (aba, [linhas]) com os valores crus das celulas, para .xls (xlrd) e .xlsx (openpyxl)."""
    if nome.lower().endswith(".xls"):
        import xlrd  # xlrd>=2.0.1 le .xls (nao le .xlsx)
        wb = xlrd.open_workbook(file_contents=conteudo)
        for aba in wb.sheet_names():
            sh = wb.sheet_by_name(aba)
            yield aba, (sh.row_values(r) for r in range(sh.nrows))
    else:
        import warnings

        import openpyxl
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # "Workbook contains no default style"
            # nao usar read_only: o xlsx do Tesouro nao traz <dimension> e o modo de leitura enxerga uma unica linha vazia
            wb = openpyxl.load_workbook(BytesIO(conteudo), data_only=True)
        for ws in wb.worksheets:
            yield ws.title, (list(r) for r in ws.iter_rows(values_only=True))


def parsear(conteudo, nome):
    """-> (registros, descartadas): lista de dicts normalizados + quantas linhas com dados quebrados foram ignoradas.

    O cabecalho e achado pelo texto 'Data do leilao' (qualquer coluna, qualquer linha) e as colunas pelo nome;
    cabecalho em ingles, legenda e rodape nao tem data valida e sao pulados. Levanta ValueError se uma aba
    com 'Data do leilao' nao tiver todas as 13 colunas (o layout mudou: melhor falhar do que gravar errado)."""
    regs, descartadas = [], 0
    for aba, linhas in _abas(conteudo, nome):
        col = None
        for n, linha in enumerate(linhas, start=1):
            if col is None:
                nomes = {sem_acento(c): i for i, c in enumerate(linha) if c not in ("", None)}
                if "data do leilao" in nomes:
                    faltam = [h for h in CABECALHO if h not in nomes]
                    if faltam:
                        raise ValueError(f"aba {aba!r} sem as colunas {faltam}: layout do Tesouro mudou?")
                    col = {CABECALHO[h]: nomes[h] for h in CABECALHO}
                continue
            v = {campo: (linha[i] if i < len(linha) else None) for campo, i in col.items()}
            data, venc, volta = como_data(v["data"]), como_data(v["vencimento"]), como_volta(v["volta"])
            if data is None:
                continue  # cabecalho em ingles, linha vazia ou rodape
            tipo, titulo = TIPOS.get(sem_acento(v["tipo"])), str(v["titulo"] or "").strip().upper()
            if not (venc and volta and tipo and titulo):
                descartadas += 1
                continue
            reg = dict(data=data, titulo=titulo, tipo=tipo, volta=volta, vencimento=venc,
                       liquidacao=como_data(v["liquidacao"]) or "", aba=aba, linha=n)
            reg.update({c: como_num(v[c]) for c in CAMPOS_VALOR})
            regs.append(reg)
    if not regs:
        raise ValueError(f"nenhuma linha de leilao em {nome}: arquivo sem a linha 'Data do leilao'?")
    return regs, descartadas


def chave(r):
    return (r["data"], r["titulo"], r["tipo"], r["volta"], r["vencimento"], r["liquidacao"])


def valores(r):
    return tuple(r[c] for c in CAMPOS_VALOR)


def hash_linha(r):
    return hashlib.sha1(repr(valores(r)).encode()).hexdigest()


def numerar(regs):
    """Copias exatas (mesma chave e mesmos valores) sao uma so linha; chave igual com valores diferentes ganha seq 2, 3...
    Devolve (registros com seq, copias descartadas)."""
    vistos, por_chave, saida, copias = set(), {}, [], 0
    for r in regs:
        k = (chave(r), valores(r))
        if k in vistos:
            copias += 1
            continue
        vistos.add(k)
        por_chave[chave(r)] = por_chave.get(chave(r), 0) + 1
        saida.append(dict(r, seq=por_chave[chave(r)]))
    return saida, copias


# ------------------------------------------------------------------ banco
def gravar(db, regs, arquivo, agora=None):
    """Upsert pela chave natural. Revisao (hash diferente) atualiza a linha e deixa o antes/depois em leilao_revisoes.
    -> dict(novos=[ids], revisados=[ids], iguais=n, copias=n)."""
    agora = agora or agora_br().isoformat(timespec="seconds")
    regs, copias = numerar(regs)
    novos, revisados, iguais = [], [], 0
    existentes = {(*chave_(r),): r for r in db.execute(
        "SELECT id,data,titulo,tipo,volta,vencimento,liquidacao,seq,hash," + ",".join(CAMPOS_VALOR) +
        " FROM leilao_resultados WHERE data>=? AND data<=?", (min(r["data"] for r in regs), max(r["data"] for r in regs)))}
    with db:
        for r in regs:
            h = hash_linha(r)
            ex = existentes.get((*chave(r), r["seq"]))
            if ex is None:
                cur = db.execute(
                    "INSERT INTO leilao_resultados (data,titulo,tipo,volta,liquidacao,vencimento,seq," + ",".join(CAMPOS_VALOR) +
                    ",arquivo,aba,linha,visto_em,atualizado_em,hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["data"], r["titulo"], r["tipo"], r["volta"], r["liquidacao"], r["vencimento"], r["seq"],
                     *valores(r), arquivo, r["aba"], r["linha"], agora, agora, h))
                novos.append(cur.lastrowid)
            elif ex[8] != h:
                antes = dict(zip(CAMPOS_VALOR, ex[9:]))
                depois = {c: r[c] for c in CAMPOS_VALOR}
                db.execute("UPDATE leilao_resultados SET " + ",".join(f"{c}=?" for c in CAMPOS_VALOR) +
                           ",arquivo=?,aba=?,linha=?,atualizado_em=?,hash=? WHERE id=?",
                           (*valores(r), arquivo, r["aba"], r["linha"], agora, h, ex[0]))
                db.execute("INSERT INTO leilao_revisoes (resultado_id,em,antes,depois) VALUES (?,?,?,?)",
                           (ex[0], agora, json.dumps(antes), json.dumps(depois)))
                revisados.append(ex[0])
            else:
                iguais += 1
    return dict(novos=novos, revisados=revisados, iguais=iguais, copias=copias)


def chave_(row):
    """Chave de uma linha lida do banco (id,data,titulo,tipo,volta,vencimento,liquidacao,seq,...)."""
    return (row[1], row[2], row[3], row[4], row[5], row[6], row[7])


# ------------------------------------------------------------------ comandos
def carregar(db, conteudo, nome, recalcular=True):
    regs, descartadas = parsear(conteudo, nome)
    res = gravar(db, regs, nome)
    print(f"{nome}: {len(regs)} linhas lidas ({descartadas} descartadas), {len(res['novos'])} novas, "
          f"{len(res['revisados'])} revisadas, {res['iguais']} iguais, {res['copias']} copias exatas ignoradas", flush=True)
    if res["revisados"]:
        print(f"  ATENCAO: o Tesouro revisou {len(res['revisados'])} linhas ja gravadas (ver leilao_revisoes)", flush=True)
    if recalcular:
        import leiloes
        ids = res["novos"] + res["revisados"]
        if ids:
            leiloes.atualizar(db, ids, alertas_desde="0000-00-00")  # alertas de tudo o que chegou ou foi revisado
    return res


def backfill(db, arquivo=None):
    if arquivo:
        nome, conteudo = Path(arquivo).name, Path(arquivo).read_bytes()
    else:
        nome, conteudo = "historico-leiloes-todos.xls", baixar(URL_TODOS)
        if conteudo is None:
            print("historico-leiloes-todos.xls devolveu 404", file=sys.stderr)
            return 2
        guardar_bruto(nome, conteudo)
    carregar(db, conteudo, nome, recalcular=False)
    import leiloes
    # metricas de todo o historico; alertas so dos ultimos 10 dias (nao inundar a tabela com 20 anos)
    leiloes.atualizar(db, None, alertas_desde=(agora_br().date() - timedelta(days=10)).isoformat())
    return 0


def update(db, ano=None):
    ano = ano or agora_br().year
    achou = baixar_ano(ano)
    if achou is None and ano == agora_br().year and agora_br().month <= 2:
        achou = baixar_ano(ano - 1)  # arquivo do ano novo ainda nao existe em janeiro/fevereiro
    if achou is None:
        print(f"arquivo de {ano} ainda nao publicado (404)", file=sys.stderr)
        return 2
    nome, conteudo = achou
    guardar_bruto(nome, conteudo)
    carregar(db, conteudo, nome)
    return 0


def status(db):
    n, d0, d1 = db.execute("SELECT COUNT(*),MIN(data),MAX(data) FROM leilao_resultados").fetchone()
    print(f"{n} linhas de {d0} a {d1}; {db.execute('SELECT COUNT(*) FROM leilao_revisoes').fetchone()[0]} revisoes")
    for r in db.execute("SELECT data,MIN(visto_em),COUNT(*) FROM leilao_resultados GROUP BY data ORDER BY data DESC LIMIT 5"):
        print("  ", r)
    return 0


# ------------------------------------------------------------------ supervisor
INTERVALO_S = 15 * 60       # dia de leilao esperado (terca: LFT/NTN-B; quinta: LTN/NTN-F): a cada 15 min
INTERVALO_LENTO_S = 60 * 60  # outros dias uteis (um Extra pode sair a qualquer momento)
INICIO_JANELA_H = 12         # propostas ~11h-11h30, resultado ~12h-13h (nao confirmado: ver visto_em)


def dia_de_leilao_esperado(d):
    """Estimativa pelo padrao de 2026 (nao e calendario oficial): terca e quinta."""
    return d.weekday() in (1, 3)


def supervisor(db_path, segundos):
    """Roda ate `segundos` (o supervisor rodar_coleta.py passa o tempo ate o fim da janela). Faz uma busca ao subir
    (pega os Extras do dia anterior); em dia esperado espera ate 12:00 e busca a cada 15 min, parando de buscar quando a
    data de hoje aparece no arquivo; nos demais dias, a cada hora."""
    fim = time.time() + segundos
    primeira = True
    while True:
        agora = agora_br()
        esperado = dia_de_leilao_esperado(agora.date())
        ja_saiu = False
        if primeira or not esperado or agora.hour >= INICIO_JANELA_H:
            try:
                db = banco.abrir(db_path, SCHEMA)
                try:
                    update(db)
                    ja_saiu = db.execute("SELECT 1 FROM leilao_resultados WHERE data=?", (agora.date().isoformat(),)).fetchone() is not None
                finally:
                    db.close()
            except Exception as e:
                print(f"[{agora:%H:%M:%S}] erro: {e}", flush=True)
            primeira = False
        espera = INTERVALO_S if esperado else INTERVALO_LENTO_S
        if ja_saiu or time.time() + espera >= fim:
            time.sleep(max(0.0, fim - time.time()))  # sair antes faria o supervisor relancar a cada 10 s
            return 0
        time.sleep(espera)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "LEILOES":  # forma do supervisor: LEILOES SEGUNDOS [--db ...]
        ap = argparse.ArgumentParser()
        ap.add_argument("grupo")
        ap.add_argument("segundos", type=float)
        ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
        a = ap.parse_args(argv)
        return supervisor(a.db, a.segundos)
    ap = argparse.ArgumentParser(description="Leiloes do Tesouro Nacional -> book.db")
    ap.add_argument("comando", choices=("backfill", "update", "status"))
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--arquivo", help="backfill: planilha local (.xls/.xlsx) em vez de baixar")
    ap.add_argument("--ano", type=int, help="update: ano (padrao: o corrente)")
    a = ap.parse_args(argv)
    db = banco.abrir(a.db, SCHEMA)
    try:
        return {"backfill": lambda: backfill(db, a.arquivo), "update": lambda: update(db, a.ano),
                "status": lambda: status(db)}[a.comando]()
    except Exception as e:
        print(f"erro: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
