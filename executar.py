"""Roda um script do projeto fora do modo de eficiencia do Windows (ver desempenho.py).

Uso (pelo rodar_coleta.py): python executar.py C:\\...\\coletor_tryd.py TRYD 34000 --db dados/book.db
O caminho do script continua na linha de comando, entao os filtros de processo ('*\\coletor_tryd.py TRYD*') valem.
"""
import runpy
import sys

import desempenho

if __name__ == "__main__":
    desempenho.sem_modo_eficiencia()
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name="__main__")
