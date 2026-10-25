"""Coletor de expectativas de mercado do Boletim Focus (Banco Central) via API publica.

Fonte publica (verificada em 25/10/2026):
  GET https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais
      ?$filter=Indicador eq 'IPCA' and baseCalculo eq 0
      &$orderby=Data desc
      &$format=json
  API OData do BCB, sem autenticacao, limite de ~500 registros por requisicao.
  Dados: Top5, Mediana, Media, DesvioPadrao, Minimo, Maximo por Data/DataReferencia (ano).

Tabelas (data_ref = ano da projecao AAAA; data = data da captura AAAA-MM-DD; ts_ms = data 00:00 UTC):
  focus_ipca         data, ts_ms, data_ref, mediana, media, desvio_padrao, minimo, maximo, top5,
                     capturado_em
Idempotente: regravar uma data apaga e reinsere as linhas dela numa transacao curta.

Uso: python coletor_focus.py                       coleta o relatorio mais recente (ultima sexta-feira)
     python coletor_focus.py 2026-10-16            uma data especifica
     [--db dados/book.db] [--log dados/coletor_focus.log]
Saida: 0 ok | 1 erro | 2 nenhum dado disponivel na data.
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
URL_BASE = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
UA = {"User-Agent": "Mozilla/5.0 (coletor_focus; uso pessoal)"}
TIMEOUT = (20, 60)
TENTATIVAS = 4
ESPERA = (5, 20, 60)
log = logging.getLogger("coletor_focus")

SCHEMA = """
CREATE TABLE IF NOT EXISTS focus_ipca (
    data TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    data_ref INTEGER NOT NULL,
    mediana REAL,
    media REAL,
    desvio_padrao REAL,
    minimo REAL,
    maximo REAL,
    top5 REAL,
    capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, data_ref)
);
CREATE INDEX IF NOT EXISTS ix_focus_ipca_ref ON focus_ipca (data_ref, data);
"""


def agora_br():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)


def ms_br(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def ultima_sexta():
    """Retorna a ultima sexta-feira (inclusive hoje se for sexta)."""
    hj = date.today()
    dias = (hj.weekday() - 4) % 7  # 0=sexta de hoje, 1-6=dias desde a ultima sexta
    return hj - timedelta(days=dias)


def buscar_expectativas(data_ref_str):
    """Busca expectativas do IPCA para uma data de referencia (AAAA-MM-DD) via API do BCB.
    
    Retorna: [{"data": "2026-10-16", "data_ref": 2026, "mediana": 5.22, "media": 5.25, ...}, ...]
    """
    # API usa formato MM/DD/AAAA
    d = date.fromisoformat(data_ref_str)
    data_fmt = f"{d.month:02d}/{d.day:02d}/{d.year}"
    
    # Buscar expectativas para anos futuros (ate +5 anos)
    ano_min = d.year
    ano_max = d.year + 5
    
    filtro = f"Indicador eq 'IPCA' and baseCalculo eq 0 and Data eq '{data_fmt}'"
    url = f"{URL_BASE}?$filter={filtro}&$orderby=DataReferencia&$format=json&$top=500"
    
    for tent in range(TENTATIVAS):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            dados = r.json()
            break
        except Exception as e:
            if tent == TENTATIVAS - 1:
                raise
            log.warning(f"Tentativa {tent + 1}/{TENTATIVAS} falhou: {e}")
            time.sleep(ESPERA[min(tent, len(ESPERA) - 1)])
    
    if "value" not in dados:
        return []
    
    linhas = []
    for item in dados["value"]:
        try:
            data_ref = int(item.get("DataReferencia", 0))
            if data_ref < ano_min or data_ref > ano_max:
                continue
            
            linhas.append({
                "data": data_ref_str,
                "data_ref": data_ref,
                "mediana": item.get("Mediana"),
                "media": item.get("Media"),
                "desvio_padrao": item.get("DesvioPadrao"),
                "minimo": item.get("Minimo"),
                "maximo": item.get("Maximo"),
                "top5": item.get("Top5"),
            })
        except (ValueError, KeyError) as e:
            log.debug(f"Item ignorado: {e}")
            continue
    
    return linhas


def coletar(db, data_ref):
    """Coleta expectativas do Focus para uma data de referencia e grava no banco (idempotente)."""
    log.info(f"Buscando expectativas do Focus para {data_ref}...")
    linhas = buscar_expectativas(data_ref)
    
    if not linhas:
        log.warning(f"Nenhum dado disponivel para {data_ref}")
        return 2
    
    cap = agora_br().isoformat()
    
    with db:
        db.execute("DELETE FROM focus_ipca WHERE data=?", (data_ref,))
        db.executemany(
            "INSERT INTO focus_ipca VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(l["data"], ms_br(date.fromisoformat(l["data"])), l["data_ref"],
              l["mediana"], l["media"], l["desvio_padrao"],
              l["minimo"], l["maximo"], l["top5"], cap)
             for l in linhas]
        )
    
    log.info(f"{data_ref}: {len(linhas)} projecoes (anos {min(l['data_ref'] for l in linhas)}-{max(l['data_ref'] for l in linhas)})")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?", help="Data da captura AAAA-MM-DD (padrao: ultima sexta-feira)")
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--log")
    args = ap.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(args.log, encoding="utf-8")] if args.log else [logging.StreamHandler()]
    )
    
    db = banco.abrir(args.db, SCHEMA)
    try:
        data_ref = args.data or ultima_sexta().isoformat()
        ret = coletar(db, data_ref)
        sys.exit(ret)
    except Exception as e:
        log.exception(f"Erro: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
