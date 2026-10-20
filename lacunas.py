"""Lacunas de dados: janelas em que os coletores ao vivo gravaram dado parado (preco congelado).

Causa tipica: queda de internet. O terminal do MT5 para de receber cotacoes, mas symbol_info_tick() continua
devolvendo o ultimo tick; os coletores sem guarda gravavam snapshots, macro e opcoes (com o spot parado) como se
fossem reais. Os coletores atuais nao gravam mais nesse caso (conexao.py); este modulo cobre o que JA esta no banco:
detecta as janelas pelo preco do WIN$ sem mudar por >= 3 min no pregao (09:00-18:15) e as registra em `lacunas_dados`, para que
estudos e o painel as excluam. As barras de 1 min vem do historico do servidor do MT5 e se recuperam por backfill
(backfill_barras.py), entao nao entram aqui.

  lacunas_dados  inicio_ms, fim_ms, fonte ('mercado' = snapshots/macro/agressao das acoes/opcoes), motivo, criado_em
  (ts_ms = relogio de Brasilia como epoch "UTC", convencao do banco)

Uso: python lacunas.py [--db dados/book.db] [--dia AAAA-MM-DD ...] [--desde AAAA-MM-DD] [--gravar]
     sem --gravar so mostra o que encontrou. Padrao: o dia de hoje.
     em codigo: lacunas.carregar(con) -> [(inicio_ms, fim_ms), ...]; lacunas.em_lacuna(ts_ms, lista)
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import banco

SCHEMA = """
CREATE TABLE IF NOT EXISTS lacunas_dados (
    inicio_ms INTEGER NOT NULL, fim_ms INTEGER NOT NULL, fonte TEXT NOT NULL, motivo TEXT, criado_em TEXT,
    PRIMARY KEY (fonte, inicio_ms)
);
"""
MIN_PARADO_S = 180


def _ms(dia, hhmm):
    return int(pd.Timestamp(f"{dia} {hhmm}", tz="UTC").timestamp() * 1000)


def detectar(con, dia, simbolo="WIN$", min_parado_s=MIN_PARADO_S):
    """Janelas (inicio_ms, fim_ms) do pregao de `dia` em que o mid de `simbolo` ficou identico por >= min_parado_s."""
    # sem filtrar spread > 0: no congelamento de 20/10 o livro ficou no estado do pre-abertura (bid > ask), e e
    # justamente esse dado que precisa ser achado
    df = pd.read_sql("SELECT ts_ms, mid FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                     "ORDER BY ts_ms", con, params=(simbolo, _ms(dia, "09:00"), _ms(dia, "18:15")))
    if df.empty:
        return []
    bloco = (df.mid != df.mid.shift()).cumsum()
    out = []
    for _, g in df.groupby(bloco):
        if (g.ts_ms.iloc[-1] - g.ts_ms.iloc[0]) / 1000 >= min_parado_s:
            out.append((int(g.ts_ms.iloc[0]), int(g.ts_ms.iloc[-1])))
    return out


def registrar(db_path, janelas, motivo="preco do WIN$ sem mudar (sem conexao com o servidor)"):
    db = banco.abrir(str(db_path), SCHEMA)
    try:
        agora = datetime.now().isoformat(timespec="seconds")
        db.executemany("INSERT OR REPLACE INTO lacunas_dados VALUES (?,?,?,?,?)",
                       [(i, f, "mercado", motivo, agora) for i, f in janelas])
        db.commit()
    finally:
        db.close()


def carregar(con, fonte="mercado"):
    try:
        return [(r[0], r[1]) for r in con.execute(
            "SELECT inicio_ms, fim_ms FROM lacunas_dados WHERE fonte=? ORDER BY inicio_ms", (fonte,))]
    except sqlite3.OperationalError:  # tabela ainda nao existe
        return []


def em_lacuna(ts_ms, janelas):
    return any(i <= ts_ms <= f for i, f in janelas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--dia", action="append", help="AAAA-MM-DD (repetivel); padrao: hoje")
    ap.add_argument("--desde", help="AAAA-MM-DD: todos os dias uteis desde esta data ate hoje")
    ap.add_argument("--gravar", action="store_true")
    args = ap.parse_args()
    hoje = date.today()
    if args.desde:
        d = date.fromisoformat(args.desde)
        dias = [(d + timedelta(days=k)).isoformat() for k in range((hoje - d).days + 1)
                if (d + timedelta(days=k)).weekday() < 5]
    else:
        dias = args.dia or [hoje.isoformat()]
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    achadas = []
    f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d/%m %H:%M:%S")
    for dia in dias:
        for i, fim in detectar(con, dia):
            achadas.append((i, fim))
            print(f"{dia}: {f(i)} -> {f(fim)} ({(fim - i) / 60000:.0f} min) com o preco do WIN$ parado")
    con.close()
    if not achadas:
        print("nenhuma lacuna encontrada")
    elif args.gravar:
        registrar(args.db, achadas)
        print(f"{len(achadas)} lacuna(s) registrada(s) em lacunas_dados")
    else:
        print("(use --gravar para registrar)")


if __name__ == "__main__":
    main()
