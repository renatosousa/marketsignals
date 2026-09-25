"""Resumo de qualidade da coleta no book.db: ultima sessao de cada ativo (ou a sessao informada)."""
import datetime as d
import sqlite3
import sys

c = sqlite3.connect("dados/book.db")
f = lambda ms: d.datetime.utcfromtimestamp(ms / 1000).strftime("%d/%m %H:%M:%S")  # hora do servidor (Brasilia)

pares = c.execute("SELECT symbol, sessao FROM snapshots GROUP BY symbol, sessao ORDER BY symbol, sessao").fetchall()
if len(sys.argv) > 1:
    alvos = [p for p in pares if p[1] == sys.argv[1]]
else:
    alvos = list({s: (s, ses) for s, ses in pares}.values())  # ultima sessao de cada ativo
print("sessoes:", pares)

for sym, ses in alvos:
    q = (sym, ses)
    ts = [r[0] for r in c.execute("SELECT ts_ms FROM snapshots WHERE symbol=? AND sessao=? ORDER BY ts_ms", q)]
    print(f"\n== {sym} | sessao {ses}")
    print(f"snapshots: {len(ts)} | {f(ts[0])} -> {f(ts[-1])} ({(ts[-1] - ts[0]) / 60000:.0f} min)")
    gaps = [(f(a), round((b - a) / 1000)) for a, b in zip(ts, ts[1:]) if b - a > 5000]
    print(f"gaps > 5s: {len(gaps)} | maiores: {sorted(gaps, key=lambda g: -g[1])[:6]}")
    print("spread <= 0:", c.execute("SELECT count(*) FROM snapshots WHERE symbol=? AND sessao=? AND spread<=0", q).fetchone()[0])
    print("eventos:", c.execute("SELECT tipo,count(*) FROM eventos WHERE symbol=? AND sessao=? GROUP BY tipo", q).fetchall())
    print("primeiro (mid, saldo):", c.execute("SELECT mid,saldo_acum FROM snapshots WHERE symbol=? AND sessao=? ORDER BY ts_ms LIMIT 1", q).fetchone())
    print("ultimo   (mid, saldo):", c.execute("SELECT mid,saldo_acum FROM snapshots WHERE symbol=? AND sessao=? ORDER BY ts_ms DESC LIMIT 1", q).fetchone())
