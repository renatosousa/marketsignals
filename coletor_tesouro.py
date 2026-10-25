"""Coletor de taxas do Tesouro Direto (Prefixado e IPCA+) via CSV publico do Tesouro Transparente.

Fonte publica (verificada em 25/10/2026):
  GET https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv
  CSV separado por ponto e virgula, codificacao latin-1, virgula decimal.
  Historico completo desde 2002, atualizado diariamente por volta de 18h-19h com as taxas do dia.

Tabelas (data = dia de referencia AAAA-MM-DD; ts_ms = esse dia 00:00 em hora de Brasilia codificada como
epoch "UTC", convencao do banco; taxas em % a.a.):
  tesouro_taxas      data, ts_ms, tipo_titulo, vencimento, taxa_compra, taxa_venda, pu_compra, pu_venda,
                     capturado_em
Idempotente: regravar um dia apaga e reinsere as linhas dele numa transacao curta.

Uso: python coletor_tesouro.py                       completa os ultimos 10 dias
     python coletor_tesouro.py 2026-10-20            um dia (regrava)
     python coletor_tesouro.py 2026-10-01 2026-10-20 intervalo (regrava)
     [--db dados/book.db] [--log dados/coletor_tesouro.log]
Saida: 0 ok | 1 erro | 2 o dia esperado ainda nao foi publicado.
"""
import argparse
import csv
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import requests

import banco

AQUI = Path(__file__).parent
URL_CSV = "https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv"
UA = {"User-Agent": "Mozilla/5.0 (coletor_tesouro; uso pessoal)"}
TIMEOUT = (20, 90)
TENTATIVAS = 4
ESPERA = (5, 20, 60)
log = logging.getLogger("coletor_tesouro")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tesouro_taxas (
    data TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    tipo_titulo TEXT NOT NULL,
    vencimento TEXT NOT NULL,
    taxa_compra REAL,
    taxa_venda REAL,
    pu_compra REAL,
    pu_venda REAL,
    capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, tipo_titulo, vencimento)
);
CREATE INDEX IF NOT EXISTS ix_tesouro_taxas_venc ON tesouro_taxas (tipo_titulo, vencimento, data);
"""


def agora_br():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)


def ms_br(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def num(v):
    """'13,50' -> 13.5; '1.234,56' -> 1234.56; '' -> None."""
    s = (v or "").strip()
    if not s or s in ("-", "--", "N/D"):
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parsear_csv(texto):
    """Parseia o CSV do Tesouro Direto e retorna lista de dicts com os dados.
    
    Colunas esperadas:
    Data Vencimento, Tipo Titulo, Taxa Compra Manha, Taxa Venda Manha, PU Compra Manha, PU Venda Manha,
    Data Base, Taxa Compra, Taxa Venda, PU Compra, PU Venda
    
    Retorna: [{"data": "2026-10-20", "tipo_titulo": "Tesouro Prefixado 2029", "vencimento": "2029-01-01", 
               "taxa_compra": 12.64, "taxa_venda": 12.74, "pu_compra": 800.12, "pu_venda": 799.00}, ...]
    """
    linhas = []
    reader = csv.DictReader(StringIO(texto), delimiter=";")
    
    for row in reader:
        try:
            # Data Base e Data Vencimento no formato DD/MM/YYYY
            data_base = row.get("Data Base", "").strip()
            if not data_base:
                continue
            
            d, m, a = data_base.split("/")
            data_iso = f"{a}-{m.zfill(2)}-{d.zfill(2)}"
            
            venc = row.get("Data Vencimento", "").strip()
            if not venc:
                continue
            dv, mv, av = venc.split("/")
            venc_iso = f"{av}-{mv.zfill(2)}-{dv.zfill(2)}"
            
            tipo = row.get("Tipo Titulo", "").strip()
            if not tipo:
                continue
            
            # Pegar taxas do dia (nao da manha)
            taxa_compra = num(row.get("Taxa Compra", ""))
            taxa_venda = num(row.get("Taxa Venda", ""))
            pu_compra = num(row.get("PU Compra", ""))
            pu_venda = num(row.get("PU Venda", ""))
            
            # Filtrar apenas Tesouro Prefixado e Tesouro IPCA+
            if not (tipo.startswith("Tesouro Prefixado") or tipo.startswith("Tesouro IPCA+")):
                continue
            
            linhas.append({
                "data": data_iso,
                "tipo_titulo": tipo,
                "vencimento": venc_iso,
                "taxa_compra": taxa_compra,
                "taxa_venda": taxa_venda,
                "pu_compra": pu_compra,
                "pu_venda": pu_venda,
            })
        except (ValueError, KeyError, AttributeError) as e:
            log.debug(f"Linha ignorada no parse: {e}")
            continue
    
    return linhas


def baixar_csv():
    """Baixa o CSV completo do Tesouro Direto com retries."""
    for tent in range(TENTATIVAS):
        try:
            r = requests.get(URL_CSV, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            # CSV em latin-1
            return r.content.decode("latin-1")
        except Exception as e:
            if tent == TENTATIVAS - 1:
                raise
            log.warning(f"Tentativa {tent + 1}/{TENTATIVAS} falhou: {e}")
            time.sleep(ESPERA[min(tent, len(ESPERA) - 1)])


def coletar_intervalo(db, d0, d1):
    """Coleta dados do intervalo [d0, d1] e grava no banco (idempotente)."""
    log.info(f"Baixando CSV do Tesouro Direto...")
    texto = baixar_csv()
    
    log.info(f"Parseando CSV...")
    linhas = parsear_csv(texto)
    
    if not linhas:
        log.warning("Nenhuma linha valida no CSV")
        return 0
    
    # Filtrar apenas as datas do intervalo
    linhas_filt = [l for l in linhas if d0 <= l["data"] <= d1]
    
    if not linhas_filt:
        log.warning(f"Nenhum dado no intervalo {d0} a {d1}")
        return 2
    
    # Agrupar por data para gravacao idempotente
    por_data = {}
    for l in linhas_filt:
        por_data.setdefault(l["data"], []).append(l)
    
    cap = agora_br().isoformat()
    
    for data, items in sorted(por_data.items()):
        with db:
            db.execute("DELETE FROM tesouro_taxas WHERE data=?", (data,))
            db.executemany(
                "INSERT INTO tesouro_taxas VALUES (?,?,?,?,?,?,?,?,?)",
                [(item["data"], ms_br(date.fromisoformat(item["data"])),
                  item["tipo_titulo"], item["vencimento"],
                  item["taxa_compra"], item["taxa_venda"],
                  item["pu_compra"], item["pu_venda"], cap)
                 for item in items]
            )
        log.info(f"{data}: {len(items)} titulos")
    
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inicio", nargs="?")
    ap.add_argument("fim", nargs="?")
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
        if args.inicio and args.fim:
            d0 = args.inicio
            d1 = args.fim
        elif args.inicio:
            d0 = d1 = args.inicio
        else:
            # Completar ultimos 10 dias
            hj = date.today()
            d0 = (hj - timedelta(days=10)).isoformat()
            d1 = hj.isoformat()
        
        ret = coletar_intervalo(db, d0, d1)
        sys.exit(ret)
    except Exception as e:
        log.exception(f"Erro: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
