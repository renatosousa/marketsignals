"""Resumo de qualidade da coleta no book.db (ultima sessao por padrao)."""
import datetime as d
import sqlite3
import sys

c = sqlite3.connect("dados/book.db")
f = lambda ms: d.datetime.utcfromtimestamp(ms / 1000).strftime("%d/%m %H:%M:%S")  # hora do servidor (Brasilia)

sessoes = [r[0] for r in c.execute("SELECT DISTINCT sessao FROM snapshots ORDER BY sessao")]
alvo = sys.argv[1] if len(sys.argv) > 1 else sessoes[-1]
print("sessoes:", sessoes, "| analisando:", alvo)

ts = [r[0] for r in c.execute("SELECT ts_ms FROM snapshots WHERE sessao=? ORDER BY ts_ms", (alvo,))]
print(f"snapshots: {len(ts)} | {f(ts[0])} -> {f(ts[-1])} ({(ts[-1] - ts[0]) / 60000:.0f} min)")
gaps = [(f(a), round((b - a) / 1000)) for a, b in zip(ts, ts[1:]) if b - a > 5000]
print(f"gaps > 5s: {len(gaps)} | maiores: {sorted(gaps, key=lambda g: -g[1])[:6]}")
print("spread <= 0:", c.execute("SELECT count(*) FROM snapshots WHERE sessao=? AND spread<=0", (alvo,)).fetchone()[0])
print("eventos:", c.execute("SELECT tipo,count(*) FROM eventos WHERE sessao=? GROUP BY tipo", (alvo,)).fetchall())
print("primeiro:", c.execute("SELECT mid,saldo_acum FROM snapshots WHERE sessao=? ORDER BY ts_ms LIMIT 1", (alvo,)).fetchone())
print("ultimo  :", c.execute("SELECT mid,saldo_acum FROM snapshots WHERE sessao=? ORDER BY ts_ms DESC LIMIT 1", (alvo,)).fetchone())
