"""Agressao por minuto das maiores acoes e do WDO -> tabela agressao_1min (book.db).

Objetivo: variaveis intradiarias para estimar o fluxo de estrangeiros do dia (ver fluxo_estrangeiro.py e
features_fluxo.py). O MT5 nao identifica corretora nem tipo de investidor; o que se mede aqui e a agressao
(quem bateu no preco: comprador ou vendedor) de uma cesta de blue chips, onde o fluxo estrangeiro se concentra.

Como coleta: copy_ticks_range de cada ativo a cada ~1 s, a partir do ultimo tick ja lido (ticks_trade.py: cada
  tick uma vez, mesmo varios no mesmo milissegundo). O lado vem das flags do tick (TICK_FLAG_BUY/SELL); tick com as
  duas flags, ou a rajada de um leilao (depois de 1 min sem negocio), fica como neutro. Conta so o pregao continuo:
  acoes de 10:00 a 16:55, WDO a partir de 09:05. O minuto e o do proprio negocio (time_msc do tick).
  Ate 27/10/2026 as acoes eram lidas so pelo ultimo tick (symbol_info_tick a ~100 Hz): o polling captava ~60% do
  volume e o saldo saia com sinal trocado (27/10: +R$ 59 mi no coletor, -R$ 154 mi nos ticks, 7 acoes). A primeira
  leitura do historico de ticks de uma acao no dia pode levar ~100 s; o coletor sobe as 08:55, antes do pregao.

Tabela agressao_1min (uma linha por ativo e minuto, regravada enquanto o minuto esta aberto):
  ts_ms, sessao, symbol, n, vol_compra, vol_venda, vol_neutro, fin_compra, fin_venda, maior_lote, preco
  (vol em acoes; para o WDO, em contratos e sem fin_*; fin em R$)

Uso: python coletor_agressao.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo-ms 1000]
"""
import argparse
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

import banco
from relogio import local_br_ms
from ticks_trade import INICIO_MS, JANELA_ACOES, LeitorTicks, em_pregao_continuo

CESTA = ["PETR4", "VALE3", "ITUB4", "BBDC4", "BBAS3", "BPAC11", "B3SA3", "PETR3", "PRIO3", "SBSP3", "ABEV3", "WEGE3"]
FUTURO = "WDO$"
SCHEMA = """
CREATE TABLE IF NOT EXISTS agressao_1min (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL,
    n INTEGER, vol_compra REAL, vol_venda REAL, vol_neutro REAL,
    fin_compra REAL, fin_venda REAL, maior_lote REAL, preco REAL,
    PRIMARY KEY (sessao, symbol, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_agr_sym_ts ON agressao_1min (symbol, ts_ms);
"""


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class Minutos:
    """Acumula estatisticas por (ativo, minuto) e sabe quais linhas precisam ser regravadas."""

    def __init__(self):
        self.d = {}
        self.sujo = set()

    def somar(self, sym, ms, lado, vol, preco, com_fin):
        k = (sym, ms // 60000 * 60000)
        s = self.d.setdefault(k, dict(n=0, c=0.0, v=0.0, nt=0.0, fc=0.0, fv=0.0, maior=0.0, preco=preco))
        s["n"] += 1
        s["maior"] = max(s["maior"], vol)
        s["preco"] = preco
        if lado == "C":
            s["c"] += vol
            s["fc"] += vol * preco if com_fin else 0.0
        elif lado == "V":
            s["v"] += vol
            s["fv"] += vol * preco if com_fin else 0.0
        else:
            s["nt"] += vol
        self.sujo.add(k)

    def linhas(self, sessao):
        """Linhas para gravar (regrava as que mudaram) e descarta da memoria minutos com mais de 5 min."""
        out = []
        for k in self.sujo:
            s = self.d[k]
            fin = k[0] != FUTURO
            out.append((k[1], sessao, k[0], s["n"], s["c"], s["v"], s["nt"], s["fc"] if fin else None,
                        s["fv"] if fin else None, s["maior"], s["preco"]))
        self.sujo = set()
        if self.d:
            ref = max(k[1] for k in self.d)
            for k in [k for k in self.d if k[1] < ref - 5 * 60000]:
                del self.d[k]
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="AGRESSAO")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo-ms", type=float, default=1000.0)
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)
    if not mt5.initialize():
        raise SystemExit(f"initialize falhou: {mt5.last_error()}")
    for s in CESTA + [FUTURO]:
        mt5.symbol_select(s, True)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    mins = Minutos()
    # comeca no ultimo tick, mas nunca antes de hoje: pedir o historico de ontem de uma acao trava ~100 s
    hoje = int(local_br_ms()) // 86_400_000 * 86_400_000
    leitores = {}
    for s in CESTA + [FUTURO]:
        tk0 = mt5.symbol_info_tick(s)
        leitores[s] = LeitorTicks(max(tk0.time_msc if tk0 else 0, hoje))
    janela = {s: JANELA_ACOES for s in CESTA} | {FUTURO: (INICIO_MS, None)}
    prox_flush = prox_log = 0.0
    pend, gravadas, negocios = [], 0, 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Agressao de {len(CESTA)} acoes + {FUTURO} -> {args.db} (sessao {sessao}). Ctrl+C para parar.", flush=True)

    try:
        while time.time() < fim:
            for s in CESTA + [FUTURO]:
                tk = mt5.symbol_info_tick(s)
                lt = leitores[s]
                if tk is None or tk.time_msc < lt.ms:
                    continue
                t0 = time.time()
                ticks = mt5.copy_ticks_range(s, utc(lt.ms), utc(int(tk.time_msc) + 1000), mt5.COPY_TICKS_TRADE)
                if time.time() - t0 > 5:
                    print(f"[{datetime.now():%H:%M:%S}] aviso: historico de ticks de {s} levou {time.time() - t0:.0f} s",
                          flush=True)
                for ms, lado, vol, preco in lt.novos(ticks):
                    if em_pregao_continuo(ms, janela[s]):
                        mins.somar(s, ms, lado, vol, preco, com_fin=s != FUTURO)
                        negocios += 1

            agora = time.time()
            if agora >= prox_flush:
                prox_flush = agora + 5.0
                pend += mins.linhas(sessao)
                if pend:
                    try:
                        db.executemany("INSERT OR REPLACE INTO agressao_1min VALUES (?,?,?,?,?,?,?,?,?,?,?)", pend)
                        db.commit()
                        gravadas += len(pend)
                        pend = []
                    except Exception as e:  # banco ocupado: tenta de novo no proximo ciclo
                        db.rollback()
                        print(f"aviso: gravacao adiada ({e})", flush=True)
            if agora >= prox_log:
                prox_log = agora + 60.0
                print(f"[{datetime.now():%H:%M:%S}] negocios {negocios} | linhas gravadas {gravadas}", flush=True)
            time.sleep(args.intervalo_ms / 1000)
    except KeyboardInterrupt:
        pass
    finally:
        pend += mins.linhas(sessao)
        if pend:
            db.executemany("INSERT OR REPLACE INTO agressao_1min VALUES (?,?,?,?,?,?,?,?,?,?,?)", pend)
            db.commit()
        mt5.shutdown()
        db.close()
    print(f"negocios {negocios} | linhas gravadas {gravadas}", flush=True)


if __name__ == "__main__":
    main()
