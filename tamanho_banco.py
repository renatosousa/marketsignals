"""Tamanho do book.db: arquivo, linhas e bytes estimados por tabela, e ritmo de crescimento.

Sem a extensao dbstat, o tamanho por tabela e estimado copiando uma amostra de linhas (com os
indices) para um banco temporario e medindo bytes/linha.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

AMOSTRA = 20_000


def bytes_por_linha(c, tabela):
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        t = sqlite3.connect(tmp)
        ddl = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL", (tabela,))]
        for s in ddl:
            t.execute(s)
        cols = len(c.execute(f"PRAGMA table_info({tabela})").fetchall())
        linhas = c.execute(f"SELECT * FROM {tabela} ORDER BY rowid DESC LIMIT {AMOSTRA}").fetchall()
        t.executemany(f"INSERT OR IGNORE INTO {tabela} VALUES ({','.join('?' * cols)})", linhas)
        t.commit()
        t.execute("VACUUM")
        t.close()
        vazio = 4096 * (1 + len(ddl))  # paginas de schema/raiz
        return max(os.path.getsize(tmp) - vazio, 0) / max(len(linhas), 1)
    finally:
        os.remove(tmp)

db = Path(sys.argv[1] if len(sys.argv) > 1 else "dados/book.db")
arquivos = [db, Path(f"{db}-wal"), Path(f"{db}-shm")]
total = sum(p.stat().st_size for p in arquivos if p.exists())
print(f"{db}: {db.stat().st_size / 2**20:.1f} MB (+ WAL {Path(f'{db}-wal').stat().st_size / 2**20:.1f} MB) "
      f"= {total / 2**20:.1f} MB")

c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tabelas = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print(f"\n{'tabela':<18}{'linhas':>12}{'B/linha':>9}{'MB (estim.)':>13}{'linhas/h':>11}{'MB/pregao':>11}")
soma_dia = 0.0
for t in tabelas:
    n = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    bpl = bytes_por_linha(c, t) if n else 0
    mb = n * bpl / 2**20
    # ritmo pela sessao mais recente de cada (ativo) coletor: linhas / horas cobertas
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    ativo = "symbol" if "symbol" in cols and t in ("snapshots", "niveis", "eventos") else "subjacente"
    # ultima sessao de cada ativo; linhas/h de cada uma, somadas (coletores rodam em paralelo)
    ritmo = sum(r[0] / max(r[1], 0.01) for r in c.execute(f"""
        SELECT count(*), (max(ts_ms) - min(ts_ms)) / 3600000.0 FROM {t}
        WHERE ({ativo}, sessao) IN (SELECT {ativo}, max(sessao) FROM {t} GROUP BY {ativo})
        GROUP BY {ativo}, sessao"""))
    por_dia = ritmo * 8.5 * bpl / 2**20  # coleta ~8,5 h por pregao
    soma_dia += por_dia
    print(f"{t:<18}{n:>12,}{bpl:>9.0f}{mb:>13.1f}{ritmo:>11,.0f}{por_dia:>11.1f}")
print(f"\nCrescimento estimado: ~{soma_dia:.0f} MB por pregao (~{soma_dia * 21 / 1024:.1f} GB/mes)")
