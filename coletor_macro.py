"""Coletor de cotacoes macro (cambio, juros, exterior, commodities) do MT5 -> SQLite.

Le bid/ask/ultimo de uma lista de ativos a cada --intervalo (1 s) e grava a linha de um ativo
so quando a cotacao muda. Sem livro e sem copy_ticks (so symbol_info_tick, que nao trava o MT5).

Tabela (ts_ms = hora do servidor MT5 em ms, mesma base dos outros coletores):
  macro_cotacoes  ts_ms, sessao, symbol, grupo, bid, ask, last, mid

DI1: as cotacoes sao TAXAS (% a.a.), nao precos.
IVVB11 e o S&P 500 em reais; S&P em dolar ~ IVVB11 / WDO$.

Uso: python coletor_macro.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo 1.0]
     (GRUPO e so um rotulo, para o supervisor; SEGUNDOS=0 roda ate Ctrl+C)
"""
import argparse
import sqlite3
import time
from datetime import datetime

import MetaTrader5 as mt5

import banco

# grupo -> ativos. ISP$/WSP$/T10$ da B3 ficam parados (apontam p/ contrato vencido); VIX/US500 nao existem.
ATIVOS = {
    "cambio": ["WDO$"],
    "juros": ["DI1F27", "DI1F28", "DI1F29", "DI1F31", "DI1F33", "DI1F35"],
    "exterior": ["IVVB11", "NASD11", "XINA11", "BEWZ39", "GOLD11", "BIT$"],
    "commodities": ["VALE3", "PETR4", "PRIO3", "SUZB3"],
    "domestico": ["ITUB4", "BBAS3", "SMAL11", "IND$"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_cotacoes (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, grupo TEXT,
    bid REAL, ask REAL, last REAL, mid REAL
);
CREATE INDEX IF NOT EXISTS ix_macro ON macro_cotacoes (symbol, ts_ms);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="MACRO")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=1.0)
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)

    if not mt5.initialize():
        raise SystemExit(f"initialize falhou: {mt5.last_error()}")
    grupo_de = {s: g for g, lista in ATIVOS.items() for s in lista}
    for s in list(grupo_de):
        if not mt5.symbol_select(s, True):
            print(f"aviso: {s} indisponivel, ignorado", flush=True)
            del grupo_de[s]

    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    offset = float("-inf")
    anterior = {}
    pendentes = []
    n = 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Coletando {len(grupo_de)} ativos macro -> {args.db} (sessao {sessao}). Ctrl+C para parar.", flush=True)

    try:
        while time.time() < fim:
            t_loop = time.time()
            ticks = {s: mt5.symbol_info_tick(s) for s in grupo_de}
            ticks = {s: tk for s, tk in ticks.items() if tk is not None and tk.time_msc}
            if not ticks:
                time.sleep(1)
                continue
            local = time.time() * 1000
            # relogio = local + maior offset visto (hora de tick nunca passa da hora do servidor)
            offset = max(offset, max(tk.time_msc for tk in ticks.values()) - local)
            agora = int(local + offset)
            for s, tk in ticks.items():
                cot = (tk.bid, tk.ask, tk.last)
                if anterior.get(s) == cot:
                    continue
                anterior[s] = cot
                mid = (tk.bid + tk.ask) / 2 if tk.bid > 0 and tk.ask > 0 else (tk.last or None)
                pendentes.append((agora, sessao, s, grupo_de[s], tk.bid, tk.ask, tk.last, mid))
            # transacao curta: segurar o lock de escrita entre ciclos travava os outros coletores
            if pendentes:
                try:
                    db.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)", pendentes)
                    db.commit()
                    n += len(pendentes)
                    pendentes = []
                except sqlite3.OperationalError as e:  # banco ocupado: tenta de novo no proximo ciclo
                    db.rollback()
                    print(f"aviso: gravacao adiada ({e}); {len(pendentes)} linhas pendentes", flush=True)
            time.sleep(max(0.0, args.intervalo - (time.time() - t_loop)))
    except KeyboardInterrupt:
        pass
    finally:
        mt5.shutdown()
        if pendentes:
            db.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)", pendentes)
            n += len(pendentes)
        db.commit()
        db.close()
    print(f"Linhas gravadas: {n}", flush=True)


if __name__ == "__main__":
    main()
