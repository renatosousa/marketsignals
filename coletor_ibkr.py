"""Coletor de cotacoes internacionais da IBKR (via IB Gateway/TWS local) -> SQLite.

Grava na mesma tabela macro_cotacoes do coletor_macro (grupo 'internacional'), no mesmo relogio
(hora de Brasilia codificada como epoch "UTC"), entao dashboard e correlacao com o WIN usam
estes ativos sem mudanca. Nomes com prefixo "IB:" (ex.: IB:ES).

SOMENTE LEITURA: conecta com readonly=True e nunca chama nada de ordens. Deixe tambem marcado
"Read-Only API" no Gateway (Configure > Settings > API > Settings).

Dados: pede tipo 3 (atrasado se nao houver assinatura; com assinatura a IBKR entrega tempo
real). Cotacao atrasada (10-15 min) NAO e gravada: levaria a hora de chegada e ficaria fora de
sincronia com o WIN. Com --aceitar-atrasado, vai com grupo 'internacional_atrasado'.

Uso: python coletor_ibkr.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--porta 4001] [--intervalo 1.0]
                            [--aceitar-atrasado]
     portas padrao: IB Gateway 4001 (real) / 4002 (paper); TWS 7496 / 7497
"""
import argparse
import math
import time
from datetime import datetime, timezone

from ib_async import IB, ContFuture, Future, Index, Stock

import banco
from coletor_macro import SCHEMA

CLIENT_ID = 11

# nome no banco -> contrato. Futuros: ContFuture resolve o vencimento da frente.
CONTRATOS = {
    "IB:ES": ContFuture("ES", "CME"),        # S&P 500 e-mini
    "IB:NQ": ContFuture("NQ", "CME"),        # Nasdaq 100 e-mini
    "IB:6L": ContFuture("BRE", "CME"),       # real brasileiro (USD por BRL); na IBKR o 6L e "BRE"
    "IB:ZN": ContFuture("ZN", "CBOT"),       # T-Note 10 anos
    "IB:CL": ContFuture("CL", "NYMEX"),      # petroleo WTI
    "IB:GC": ContFuture("GC", "COMEX"),      # ouro
    "IB:DX": ContFuture("DX", "NYBOT"),      # indice do dolar (ICE; exige assinatura propria)
    "IB:VIX": Index("VIX", "CBOE"),          # volatilidade do S&P
    "IB:TNX": Index("TNX", "CBOE"),          # juro de 10 anos x10
    "IB:EWZ": Stock("EWZ", "SMART", "USD"),  # ETF de Brasil em NY
    "IB:EEM": Stock("EEM", "SMART", "USD"),  # emergentes
    "IB:VALE": Stock("VALE", "SMART", "USD"),  # ADR Vale
    "IB:PBR": Stock("PBR", "SMART", "USD"),  # ADR Petrobras
}


def agora_br_ms():
    """Relogio de parede de Brasilia (o do PC) como epoch 'UTC', convencao do banco."""
    return int(datetime.now().replace(tzinfo=timezone.utc).timestamp() * 1000)


def resolver(ib):
    """Qualifica os contratos; futuros continuos viram o contrato da frente (necessario p/ mercado ao vivo).
    -> {nome: contrato} so dos que existem; imprime os que falharam."""
    ok = {}
    for nome, c in CONTRATOS.items():
        try:
            q = ib.qualifyContracts(c)
            if not q or not q[0] or not q[0].conId:
                raise ValueError("nao encontrado")
            c = q[0]
            if isinstance(c, ContFuture):
                c = ib.qualifyContracts(Future(conId=c.conId, exchange=c.exchange))[0]
            ok[nome] = c
        except Exception as e:
            print(f"aviso: {nome} indisponivel ({e})", flush=True)
    return ok


def num(v):
    return v if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="IBKR")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--porta", type=int, default=4001)
    ap.add_argument("--intervalo", type=float, default=1.0)
    ap.add_argument("--aceitar-atrasado", action="store_true")
    args = ap.parse_args()

    ib = IB()
    ib.connect("127.0.0.1", args.porta, clientId=CLIENT_ID, readonly=True, timeout=20)
    ib.reqMarketDataType(3)  # atrasado so onde nao ha assinatura
    contratos = resolver(ib)
    if not contratos:
        raise SystemExit("nenhum contrato disponivel")
    tickers = {nome: ib.reqMktData(c, "", False, False) for nome, c in contratos.items()}

    db = banco.abrir(args.db, SCHEMA)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    anterior, pendentes, n = {}, [], 0
    avisados = set()
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Coletando {len(tickers)} ativos IBKR -> {args.db} (sessao {sessao}). Ctrl+C para parar.", flush=True)

    try:
        while time.time() < fim:
            ib.sleep(args.intervalo)  # processa as mensagens da IBKR
            if not ib.isConnected():
                raise SystemExit("conexao com o Gateway perdida")  # supervisor reinicia
            agora = agora_br_ms()
            for nome, t in tickers.items():
                tempo_real = t.marketDataType in (1, 2)
                if not tempo_real and not args.aceitar_atrasado:
                    if nome not in avisados:
                        print(f"aviso: {nome} chegando atrasado (sem assinatura ativa); nao sera gravado", flush=True)
                        avisados.add(nome)
                    continue
                if tempo_real and nome in avisados:
                    print(f"{nome} agora em tempo real", flush=True)
                    avisados.discard(nome)
                bid, ask, last = num(t.bid), num(t.ask), num(t.last) or num(t.close)
                cot = (bid, ask, last)
                if cot == (None, None, None) or anterior.get(nome) == cot:
                    continue
                anterior[nome] = cot
                mid = (bid + ask) / 2 if bid and ask else last
                grupo = "internacional" if tempo_real else "internacional_atrasado"
                pendentes.append((agora, sessao, nome, grupo, bid, ask, last, mid))
            if pendentes:
                try:
                    db.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)", pendentes)
                    db.commit()
                    n += len(pendentes)
                    pendentes = []
                except Exception as e:  # banco ocupado: tenta no proximo ciclo
                    db.rollback()
                    print(f"aviso: gravacao adiada ({e})", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ib.disconnect()
        db.close()
    print(f"Linhas gravadas: {n}", flush=True)


if __name__ == "__main__":
    main()
