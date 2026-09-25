"""Abertura do SQLite compartilhado pelos coletores (varios processos gravando no mesmo arquivo)."""
import sqlite3
import time
from pathlib import Path


def abrir(caminho, schema, tentativas=30):
    """Conecta em modo WAL e cria o schema. Coletores sobem juntos (tarefa no logon) e o
    PRAGMA journal_mode nao respeita o busy timeout: tenta de novo enquanto o banco estiver ocupado."""
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    for i in range(tentativas):
        db = sqlite3.connect(caminho, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(schema)
            return db
        except sqlite3.OperationalError as e:
            db.close()
            if "locked" not in str(e) or i == tentativas - 1:
                raise
            time.sleep(0.5 + 0.1 * i)
