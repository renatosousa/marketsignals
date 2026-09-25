"""Supervisor: mantem um coletor (default coletor_book.py) rodando durante o pregao, todos os dias uteis.

Liga em INICIO (default 08:55) e encerra em FIM (default 18:30), horario local (Brasilia).
Se o coletor cair no meio do pregao, reinicia em 10 s (nova 'sessao' no banco, o saldo
acumulado recomeca). Fora do horario, ou em fim de semana, apenas espera.

Uso: python rodar_coleta.py [SIMBOLO] [--db dados/book.db] [--inicio 08:55] [--fim 18:30]
                            [--script coletor_book.py]
     (repassa os demais argumentos ao coletor, ex.: --niveis 5)
"""
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

AQUI = Path(__file__).parent
PY = sys.executable


def hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def arg(nome, padrao, argv):
    if nome in argv:
        i = argv.index(nome)
        valor = argv[i + 1]
        del argv[i:i + 2]
        return valor
    return padrao


def main():
    argv = sys.argv[1:]
    inicio = hhmm(arg("--inicio", "08:55", argv))
    fim = hhmm(arg("--fim", "18:30", argv))
    script = arg("--script", "coletor_book.py", argv)
    symbol = argv.pop(0) if argv and not argv[0].startswith("--") else "WIN$"
    extra = argv
    print(f"Supervisor: {script} {symbol} seg-sex {inicio[0]:02d}:{inicio[1]:02d}-{fim[0]:02d}:{fim[1]:02d}. Ctrl+C para sair.",
          flush=True)

    while True:
        agora = datetime.now()
        t_ini = agora.replace(hour=inicio[0], minute=inicio[1], second=0, microsecond=0)
        t_fim = agora.replace(hour=fim[0], minute=fim[1], second=0, microsecond=0)
        if agora.weekday() < 5 and t_ini <= agora < t_fim:
            restante = int((t_fim - agora).total_seconds())
            print(f"[{agora:%d/%m %H:%M:%S}] iniciando coletor ({restante}s ate o fim)", flush=True)
            r = subprocess.run([PY, str(AQUI / script), symbol, str(restante), *extra], cwd=AQUI)
            print(f"[{datetime.now():%d/%m %H:%M:%S}] coletor saiu (codigo {r.returncode})", flush=True)
            time.sleep(10)
        else:
            time.sleep(30)


if __name__ == "__main__":
    main()
