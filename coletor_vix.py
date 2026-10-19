"""VIX (CBOE, atrasado ~15 min) -> macro_cotacoes, sem login.

Fonte: cotacao atrasada publica da Cboe (a mesma do site deles), sem chave nem conta:
  https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json   (responde com um redirecionamento 307)
Campos usados: data.current_price (ultimo), data.prev_day_close, data.high, data.low.
Nao e uma API oficial documentada: se o endereco mudar, o coletor avisa e segue tentando.

Grava no mesmo formato e com o mesmo simbolo do coletor da IBKR (IB:VIX, grupo internacional_atrasado), entao
coletor_regime.py e o dashboard nao mudam. Substitui o coletor_ibkr.py, que so gravava o VIX e exigia o IB Gateway
logado todo dia. Grava quando o valor muda e, ao menos, a cada 5 min (o regime mede a idade do dado pela ultima linha).

ts_ms = hora de chegada (relogio de Brasilia como epoch UTC, convencao do banco). O dado ja chega ~15 min velho.

Uso: python coletor_vix.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo 60]
"""
import argparse
import sqlite3
import time
from datetime import datetime

import requests

import banco
from coletor_macro import SCHEMA
from relogio import local_br_ms

URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
UA = {"User-Agent": "Mozilla/5.0 (coletor_vix; uso pessoal)"}
SIMBOLO, GRUPO_MACRO = "IB:VIX", "internacional_atrasado"
BATIMENTO_S = 300.0  # grava mesmo sem mudanca a cada 5 min


def consultar(sessao_http):
    """-> (ultimo, fechamento anterior, maxima, minima). Levanta ValueError se a resposta nao for plausivel."""
    r = sessao_http.get(URL, headers=UA, timeout=(5, 15), allow_redirects=True)
    r.raise_for_status()
    d = r.json()["data"]
    ultimo = float(d["current_price"])
    if not 5 < ultimo < 200:
        raise ValueError(f"VIX implausivel: {ultimo}")
    return ultimo, d.get("prev_day_close"), d.get("high"), d.get("low")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="VIX")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=60.0)
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    http = requests.Session()
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"VIX (Cboe, atrasado) -> {args.db} (sessao {sessao}), a cada {args.intervalo:.0f}s. Ctrl+C para parar.",
          flush=True)
    anterior, ultima_gravacao, falhas, n = None, 0.0, 0, 0
    while time.time() < fim:
        t0 = time.time()
        try:
            ultimo, fech, alta, baixa = consultar(http)
            falhas = 0
            if ultimo != anterior or t0 - ultima_gravacao >= BATIMENTO_S:
                db.execute("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)",
                           (int(local_br_ms()), sessao, SIMBOLO, GRUPO_MACRO, None, None, ultimo, ultimo))
                db.commit()
                if ultimo != anterior:
                    print(f"[{datetime.now():%H:%M:%S}] VIX {ultimo:.2f} (fech. anterior {fech}, dia {baixa}-{alta})",
                          flush=True)
                anterior, ultima_gravacao, n = ultimo, t0, n + 1
        except sqlite3.OperationalError as e:  # banco ocupado: tenta no proximo ciclo
            db.rollback()
            print(f"aviso: gravacao adiada ({e})", flush=True)
        except Exception as e:  # rede fora, endereco mudou, resposta estranha: avisa e segue
            falhas += 1
            if falhas in (1, 5) or falhas % 30 == 0:
                print(f"aviso: VIX nao atualizado ({e.__class__.__name__}: {e}); falhas seguidas: {falhas}", flush=True)
        time.sleep(max(1.0, args.intervalo * min(falhas + 1, 5) - (time.time() - t0)))  # recua se estiver falhando
    db.close()
    print(f"Linhas gravadas: {n}", flush=True)


if __name__ == "__main__":
    main()
