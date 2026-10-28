"""Leiloes de operacoes compromissadas do Banco Central e Selic (meta e over) -> SQLite.

Fontes publicas, sem login (verificadas em 28/10/2026):
  Leiloes do Selic (dados abertos do BCB, Olinda/OData):
    https://olinda.bcb.gov.br/olinda/servico/leiloes_selic/versao/v1/odata/leiloes_compromissadas(...)
    parametros dataLancamentoInicio/Fim 'AAAA-MM-DD' (os demais vazios); historico desde 2018.
  Selic (SGS): 432 = meta (% a.a., valor diario, inclusive fins de semana); 1178 = over anualizada base 252.
    https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados?formato=json&dataInicial=DD/MM/AAAA&dataFinal=...
    (o atalho /ultimos/N as vezes volta vazio; a consulta por intervalo e mais estavel)

O que sao os leiloes (nomeTipoOferta):
  Tomador             leilao de 1 dia util (3 corridos na sexta) so para dealers, as 09:00, as vezes com extras
                      no mesmo dia. O BC toma recursos: volume = excesso de liquidez do sistema que ele absorve.
                      taxa_corte em % a.a.; com o corredor atual sai a meta - 0,10 (13,65% com meta de 13,75%).
  Compromissada 1047  BC vende titulos com compromisso de recompra, 91 ou 182 dias, propostas das 12:00 as 12:30
                      (oferta tipica de R$ 5 bi). taxa_corte NAO e % a.a.: e o percentual aplicado sobre o fator
                      diario da Selic (99,29 = 99,29% da Selic). Quanto menor, mais as instituicoes aceitam ganhar
                      menos que a Selic para aplicar com o BC.
  percentual_corte: vem 0 na maioria; quando preenchido parece ser o rateio das propostas na taxa de corte (nao
  confirmado na documentacao). volume em R$ mil (cabecalho da pagina do BC; a API nao traz unidade).

Tabelas:
  bcb_compromissadas  id, data, hora, publico, comunicado, tipo, prazo_dc, data_liquidacao, data_retorno,
                      volume_mil, taxa_corte, percentual_corte, coletado_em
  bcb_selic           data, meta, over_aa

Uso: python coletor_bcb.py [--desde AAAA-MM-DD] [--db dados/book.db]   uma rodada (padrao: ultimos 10 dias)
     python coletor_bcb.py BCB SEGUNDOS [--db ...]                      supervisor: repete a cada 20 min
Saida: 0 ok | 1 erro.
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta

import requests

import banco

URL_LEILOES = ("https://olinda.bcb.gov.br/olinda/servico/leiloes_selic/versao/v1/odata/leiloes_compromissadas("
               "dataLancamentoInicio=@a,dataLancamentoFim=@b,horaInicio=@c,dataLiquidacao=@d,dataRetorno=@e,"
               "publicoPermitidoLeilao=@f,nomeTipoOferta=@g)")
URL_SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{}/dados"
SERIES = {"meta": 432, "over_aa": 1178}
UA = {"User-Agent": "Mozilla/5.0 (coletor_bcb; uso pessoal)"}
TIMEOUT = (20, 120)
ESPERA = (5, 20, 60)
INTERVALO_S = 20 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS bcb_compromissadas (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, hora TEXT, publico TEXT, comunicado TEXT, tipo TEXT NOT NULL,
    prazo_dc INTEGER, data_liquidacao TEXT, data_retorno TEXT, volume_mil REAL, taxa_corte REAL,
    percentual_corte REAL, coletado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bcb_comp_data ON bcb_compromissadas (tipo, data);
CREATE TABLE IF NOT EXISTS bcb_selic (data TEXT PRIMARY KEY, meta REAL, over_aa REAL);
"""


def obter(url, params):
    """GET com tentativas (a API do BC falha as vezes). -> JSON"""
    erro = None
    for k in range(len(ESPERA) + 1):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            erro = e
            if k < len(ESPERA):
                time.sleep(ESPERA[k])
    raise RuntimeError(f"{url.split('?')[0][:80]}: {erro}")


def leiloes(d0, d1):
    p = {"@a": f"'{d0}'", "@b": f"'{d1}'", **{f"@{c}": "''" for c in "cdefg"}, "$format": "json", "$top": "100000"}
    return obter(URL_LEILOES, p)["value"]


def sgs(serie, d0, d1):
    br = lambda d: f"{d:%d/%m/%Y}"
    linhas = obter(URL_SGS.format(serie), {"formato": "json", "dataInicial": br(d0), "dataFinal": br(d1)})
    return {datetime.strptime(x["data"], "%d/%m/%Y").date().isoformat(): float(x["valor"]) for x in linhas}


def coletar(db_path, desde):
    hoje = date.today()
    agora = datetime.now().isoformat(timespec="seconds")
    lei = leiloes(desde.isoformat(), hoje.isoformat())
    linhas = [(x["id"], x["dataMovimento"], x["horaInicio"], x["publicoPermitidoLeilao"], x["numeroComunicado"],
               x["nomeTipoOferta"], x["prazoDiasCorridos"], x["dataLiquidacao"], x["dataRetorno"], x["volumeAceito"],
               x["taxaCorte"], x["percentualCorte"], agora) for x in lei]
    selic = {nome: sgs(s, desde, hoje) for nome, s in SERIES.items()}
    dias = sorted(set(selic["meta"]) | set(selic["over_aa"]))
    db = banco.abrir(db_path, SCHEMA)
    try:
        with db:
            db.executemany("INSERT OR REPLACE INTO bcb_compromissadas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", linhas)
            db.executemany("INSERT OR REPLACE INTO bcb_selic VALUES (?,?,?)",
                           [(d, selic["meta"].get(d), selic["over_aa"].get(d)) for d in dias])
    finally:
        db.close()
    return len(linhas), len(dias)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?")
    ap.add_argument("segundos", nargs="?", type=float)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--desde", help="AAAA-MM-DD (padrao: ultimos 10 dias)")
    args = ap.parse_args()
    desde = date.fromisoformat(args.desde) if args.desde else date.today() - timedelta(days=10)
    fim = time.time() + args.segundos if args.segundos else None
    while True:
        try:
            n, d = coletar(args.db, desde)
            print(f"[{datetime.now():%H:%M:%S}] leiloes {n} | dias de Selic {d}", flush=True)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] erro: {e}", flush=True)
            if fim is None:
                return 1
        if fim is None:
            return 0
        if time.time() + INTERVALO_S >= fim:  # espera o fim da janela: sair antes faz o supervisor relancar
            time.sleep(max(0.0, fim - time.time()))  # a cada ~10 s e martelar a API do BC
            return 0
        time.sleep(INTERVALO_S)
        desde = date.today() - timedelta(days=10)


if __name__ == "__main__":
    sys.exit(main())
