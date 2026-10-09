"""Taxas indicativas de titulos publicos e curvas de juros (ETTJ) de fechamento da ANBIMA -> SQLite.

Fontes publicas, sem login (verificadas em 09/10/2026):
  Mercado secundario de titulos publicos (LTN, NTN-F, NTN-B, LFT...):
    GET https://www.anbima.com.br/informacoes/merc-sec/arqs/ms{AAMMDD}.txt
    texto latin-1, campos separados por '@', virgula decimal; 404 = ainda nao publicado / sem pregao.
  ETTJ de fechamento (pre, real/IPCA e inflacao implicita por vertice, parametros Svensson):
    POST https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp  Idioma=PT&Dt_Ref=DD/MM/AAAA&saida=csv
    resposta vazia = nao publicado.
Historico publico curto: em 09/10/2026 havia ETTJ desde 14/09 e arquivos ms desde 11/09 (~20 dias
uteis). Para ter historico longo, rode o coletor todo dia (a tarefa agendada abaixo completa os
ultimos 10 dias que faltarem).
A ANBIMA publica por volta de 18h45-19h05 (Last-Modified dos arquivos ms de 01-08/10/2026) e as vezes
republica no dia seguinte.

Tabelas (data = dia de referencia AAAA-MM-DD; ts_ms = esse dia 00:00 em hora de Brasilia codificada como
epoch "UTC", convencao do banco; taxas em % a.a.):
  anbima_titulos     data, ts_ms, titulo, codigo_selic, data_base, vencimento, tx_compra, tx_venda,
                     tx_indicativa, pu, desvio_padrao, int_inf_d0, int_sup_d0, int_inf_d1, int_sup_d1, criterio
  anbima_ettj        data, ts_ms, vertice_du, ettj_ipca, ettj_pre, inflacao_implicita
  anbima_ettj_param  data, ts_ms, curva (PREFIXADOS|IPCA), beta1..beta4, lambda1, lambda2
Idempotente: regravar um dia apaga e reinsere as linhas dele numa transacao curta.

Uso: python coletor_anbima.py                       completa os ultimos 10 dias e regrava os 2 mais recentes
     python coletor_anbima.py 2026-10-08            um dia (regrava)
     python coletor_anbima.py 2026-10-01 2026-10-08 intervalo (regrava; dias sem arquivo sao pulados)
     [--db dados/book.db] [--log dados/coletor_anbima.log]
Saida: 0 ok | 1 erro | 2 o dia esperado (ou pedido) ainda nao foi publicado.
"""
import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import banco

AQUI = Path(__file__).parent
URL_MS = "https://www.anbima.com.br/informacoes/merc-sec/arqs/ms{:%y%m%d}.txt"
URL_ETTJ = "https://www.anbima.com.br/informacoes/est-termo/CZ-down.asp"
UA = {"User-Agent": "Mozilla/5.0 (coletor_anbima; uso pessoal)"}
TIMEOUT = (20, 60)
TENTATIVAS = 4
ESPERA = (5, 20, 60)
PUBLICA_H = 19.5  # depois das 19h30 o dia corrente ja deve estar publicado
log = logging.getLogger("coletor_anbima")

SCHEMA = """
CREATE TABLE IF NOT EXISTS anbima_titulos (
    data TEXT NOT NULL, ts_ms INTEGER NOT NULL, titulo TEXT NOT NULL, codigo_selic TEXT,
    data_base TEXT NOT NULL, vencimento TEXT NOT NULL,
    tx_compra REAL, tx_venda REAL, tx_indicativa REAL, pu REAL, desvio_padrao REAL,
    int_inf_d0 REAL, int_sup_d0 REAL, int_inf_d1 REAL, int_sup_d1 REAL, criterio TEXT,
    capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, titulo, vencimento, data_base)
);
CREATE INDEX IF NOT EXISTS ix_anbima_titulos_venc ON anbima_titulos (titulo, vencimento, ts_ms);
CREATE TABLE IF NOT EXISTS anbima_ettj (
    data TEXT NOT NULL, ts_ms INTEGER NOT NULL, vertice_du INTEGER NOT NULL,
    ettj_ipca REAL, ettj_pre REAL, inflacao_implicita REAL, capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, vertice_du)
);
CREATE TABLE IF NOT EXISTS anbima_ettj_param (
    data TEXT NOT NULL, ts_ms INTEGER NOT NULL, curva TEXT NOT NULL,
    beta1 REAL, beta2 REAL, beta3 REAL, beta4 REAL, lambda1 REAL, lambda2 REAL, capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, curva)
);
"""


# ------------------------------------------------------------------ utilitarios
def agora_br():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)


def ms_br(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def num(v):
    """'13,1049' -> 13.1049; '1.008' -> 1008; '1,6E-02' -> 0.016; '', '--' -> None."""
    s = (v or "").strip()
    if s in ("", "-", "--", "N/D"):
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s else s.replace(".", "") if s.count(".") and \
        all(len(p) == 3 for p in s.split(".")[1:]) else s
    try:
        return float(s)
    except ValueError:
        return None


def iso(aaaammdd):
    s = (aaaammdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 10 and s[2] == "/":
        return f"{s[6:]}-{s[3:5]}-{s[:2]}"
    return s or None


def _requisicao(metodo, url, **kw):
    ultimo = None
    for i in range(TENTATIVAS):
        try:
            r = requests.request(metodo, url, headers=UA, timeout=TIMEOUT, **kw)
            if r.status_code == 404:
                return None
            if r.status_code in (408, 429) or r.status_code >= 500:
                r.raise_for_status()
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            cod = e.response.status_code if e.response is not None else 0
            if 400 <= cod < 500 and cod not in (408, 429):
                raise
            ultimo = e
        except (requests.ConnectionError, requests.Timeout) as e:
            ultimo = e
        if i < TENTATIVAS - 1:
            espera = ESPERA[min(i, len(ESPERA) - 1)]
            log.warning("falha em %s (%s: %s); nova tentativa em %ss", url, ultimo.__class__.__name__, ultimo, espera)
            time.sleep(espera)
    raise ultimo


# ------------------------------------------------------------------ download e parse
def baixar_titulos(d):
    r = _requisicao("GET", URL_MS.format(d))
    if r is None or "Titulo@" not in r.content[:2000].decode("latin-1"):
        return None
    linhas = r.content.decode("latin-1").replace("\r", "").split("\n")
    i = next(k for k, l in enumerate(linhas) if l.startswith("Titulo@"))
    regs = []
    for l in linhas[i + 1:]:
        c = l.split("@")
        if len(c) < 15:
            continue
        if iso(c[1]) != d.isoformat():
            raise ValueError(f"arquivo ms de {d} traz data de referencia {c[1]}")
        regs.append(dict(titulo=c[0].strip(), codigo_selic=c[2].strip(), data_base=iso(c[3]), vencimento=iso(c[4]),
                         tx_compra=num(c[5]), tx_venda=num(c[6]), tx_indicativa=num(c[7]), pu=num(c[8]),
                         desvio_padrao=num(c[9]), int_inf_d0=num(c[10]), int_sup_d0=num(c[11]),
                         int_inf_d1=num(c[12]), int_sup_d1=num(c[13]), criterio=c[14].strip()))
    return regs or None


def baixar_ettj(d):
    r = _requisicao("POST", URL_ETTJ, data={"Idioma": "PT", "Dt_Ref": f"{d:%d/%m/%Y}", "saida": "csv"})
    texto = r.content.decode("latin-1").replace("\r", "") if r is not None else ""
    if not texto.strip():
        return None
    blocos = [b.strip().split("\n") for b in texto.split("\n\n") if b.strip()]
    cab = blocos[0]
    if iso(cab[0].split(";")[0]) != d.isoformat():
        raise ValueError(f"ETTJ de {d} veio com data {cab[0].split(';')[0]}")
    params = []
    for l in cab[1:]:
        c = l.split(";")
        if len(c) >= 7:
            params.append(dict(curva=c[0].strip(), beta1=num(c[1]), beta2=num(c[2]), beta3=num(c[3]),
                               beta4=num(c[4]), lambda1=num(c[5]), lambda2=num(c[6])))
    bloco = next((b for b in blocos if b[0].lower().startswith("ettj infla")), None)
    if bloco is None:
        raise ValueError("bloco 'ETTJ Inflacao Implicita' nao encontrado no CSV da ANBIMA")
    nomes = [x.strip().lower() for x in bloco[1].split(";")]
    if not (nomes[0].startswith("vertice") and "ipca" in nomes[1] and "pref" in nomes[2] and "impl" in nomes[3]):
        raise ValueError(f"colunas inesperadas na ETTJ: {bloco[1]}")
    vert = []
    for l in bloco[2:]:
        c = l.split(";") + [""] * 4
        du = num(c[0])
        if du:
            vert.append(dict(vertice_du=int(du), ettj_ipca=num(c[1]), ettj_pre=num(c[2]), inflacao_implicita=num(c[3])))
    return dict(params=params, vertices=vert) if vert else None


# ------------------------------------------------------------------ banco
def gravar(db_path, d, titulos, ettj):
    cap = agora_br().isoformat(timespec="seconds")
    ts = ms_br(d)
    db = banco.abrir(str(db_path), SCHEMA)
    try:
        db.execute("PRAGMA busy_timeout=30000")
        for i in range(10):
            try:
                db.execute("BEGIN IMMEDIATE")
                if titulos:
                    db.execute("DELETE FROM anbima_titulos WHERE data=?", (d.isoformat(),))
                    db.executemany(
                        "INSERT INTO anbima_titulos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(d.isoformat(), ts, t["titulo"], t["codigo_selic"], t["data_base"], t["vencimento"],
                          t["tx_compra"], t["tx_venda"], t["tx_indicativa"], t["pu"], t["desvio_padrao"],
                          t["int_inf_d0"], t["int_sup_d0"], t["int_inf_d1"], t["int_sup_d1"], t["criterio"], cap)
                         for t in titulos])
                if ettj:
                    db.execute("DELETE FROM anbima_ettj WHERE data=?", (d.isoformat(),))
                    db.execute("DELETE FROM anbima_ettj_param WHERE data=?", (d.isoformat(),))
                    db.executemany("INSERT INTO anbima_ettj VALUES (?,?,?,?,?,?,?)",
                                   [(d.isoformat(), ts, v["vertice_du"], v["ettj_ipca"], v["ettj_pre"],
                                     v["inflacao_implicita"], cap) for v in ettj["vertices"]])
                    db.executemany("INSERT INTO anbima_ettj_param VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   [(d.isoformat(), ts, p["curva"], p["beta1"], p["beta2"], p["beta3"], p["beta4"],
                                     p["lambda1"], p["lambda2"], cap) for p in ettj["params"]])
                db.commit()
                return
            except sqlite3.OperationalError as e:
                db.rollback()
                if ("locked" not in str(e) and "busy" not in str(e)) or i == 9:
                    raise
                log.warning("banco ocupado (%s); tentando de novo", e)
                time.sleep(2 + i)
    finally:
        db.close()


def ja_no_banco(db_path):
    if not Path(db_path).exists():
        return set()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        t = {r[0] for r in con.execute("SELECT DISTINCT data FROM anbima_titulos")}
        e = {r[0] for r in con.execute("SELECT DISTINCT data FROM anbima_ettj")}
        return t & e
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def coletar_dia(db_path, d):
    """-> True se gravou algo; False se a ANBIMA nao tem nada para o dia."""
    titulos, ettj = baixar_titulos(d), baixar_ettj(d)
    if not titulos and not ettj:
        log.info("%s: nada publicado (feriado, fim de semana ou ainda nao saiu)", d)
        return False
    gravar(db_path, d, titulos, ettj)
    resumo = []
    if ettj:
        ii = {v["vertice_du"]: v["inflacao_implicita"] for v in ettj["vertices"]}
        resumo.append("ETTJ %d vertices, implicita 1a/2a/5a %s/%s/%s" % (
            len(ettj["vertices"]), ii.get(252), ii.get(504), ii.get(1260)))
    else:
        resumo.append("sem ETTJ (fora do historico publico ou ainda nao publicada)")
    if titulos:
        ntnb = {t["vencimento"]: t["tx_indicativa"] for t in titulos if t["titulo"] == "NTN-B"}
        resumo.append("%d titulos, NTN-B 2029/2035 %s/%s" % (len(titulos), ntnb.get("2029-05-15"), ntnb.get("2035-05-15")))
    else:
        resumo.append("sem arquivo de titulos")
    log.info("%s: %s", d, " | ".join(resumo))
    return True


def configurar_log(caminho):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    hs = [logging.FileHandler(caminho, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe nao tem stdout
        hs.append(logging.StreamHandler(sys.stdout))
    for h in hs:
        h.setFormatter(fmt)
        log.addHandler(h)


def dias_uteis(de, ate):
    d = de
    while d <= ate:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Titulos publicos e ETTJ da ANBIMA -> book.db")
    ap.add_argument("de", nargs="?", help="AAAA-MM-DD (sem data: completa os ultimos 10 dias)")
    ap.add_argument("ate", nargs="?", help="AAAA-MM-DD (fim do intervalo, inclusive)")
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--log", default=str(AQUI / "dados" / "coletor_anbima.log"))
    args = ap.parse_args(argv)
    configurar_log(args.log)
    try:
        agora = agora_br()
        if args.de:
            de = date.fromisoformat(args.de)
            ate = date.fromisoformat(args.ate) if args.ate else de
            dias = list(dias_uteis(de, ate))
            log.info("inicio: %s a %s (%d dias uteis), db %s", de, ate, len(dias), args.db)
            ok = [d for d in dias if coletar_dia(args.db, d)]
            if not ok:
                return 2
            log.info("fim: %d de %d dias gravados", len(ok), len(dias))
            return 0
        hoje = agora.date()
        esperado = hoje if hoje.weekday() < 5 and agora.hour + agora.minute / 60 >= PUBLICA_H else \
            max(dias_uteis(hoje - timedelta(days=7), hoje - timedelta(days=1)))
        tem = ja_no_banco(args.db)
        # dias que faltam + os 2 ultimos dias uteis sempre (a ANBIMA as vezes republica no dia seguinte)
        recentes = list(dias_uteis(hoje - timedelta(days=10), esperado))
        faltam = [d for d in recentes if d.isoformat() not in tem or d in recentes[-2:]]
        log.info("inicio: dia esperado %s; baixando %s; db %s", esperado, [str(d) for d in faltam], args.db)
        gravados = {d for d in faltam if coletar_dia(args.db, d)}
        if esperado.isoformat() not in tem and esperado not in gravados:
            log.error("dia esperado %s ainda nao publicado pela ANBIMA", esperado)
            return 2
        return 0
    except Exception as e:
        log.exception("falhou: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
