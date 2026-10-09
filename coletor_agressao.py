"""Agressao por minuto das maiores acoes e do WDO -> tabela agressao_1min (book.db).

Objetivo: variaveis intradiarias para estimar o fluxo de estrangeiros do dia (ver fluxo_estrangeiro.py e
features_fluxo.py). O MT5 nao identifica corretora nem tipo de investidor; o que se mede aqui e a agressao
(quem bateu no preco: comprador ou vendedor) de uma cesta de blue chips, onde o fluxo estrangeiro se concentra.

Como coleta:
  - acoes (CESTA): le o ultimo tick (symbol_info_tick) a ~100 Hz e detecta cada negocio novo. Nao usa
    copy_ticks: na Genial o historico de ticks de acoes trava o terminal por ~100 s. Negocios muito
    proximos com mesmo preco e volume podem ser contados como um (subestima de leve; ver nota abaixo).
  - WDO$ (futuro): copy_ticks_range a cada ~1 s, que no MT5 de futuros e exato e nao trava.
  O lado vem das flags do tick (TICK_FLAG_BUY/SELL). Sem flag, infere pela posicao do preco em relacao ao
  bid/ask anterior (>= ask compra, <= bid venda); fora disso conta como neutro.
  O minuto e o do proprio negocio (time_msc do tick), na mesma base de tempo do banco.

Tabela agressao_1min (uma linha por ativo e minuto, regravada enquanto o minuto esta aberto):
  ts_ms, sessao, symbol, n, vol_compra, vol_venda, vol_neutro, fin_compra, fin_venda, maior_lote, preco
  (vol em acoes; para o WDO, em contratos e sem fin_*; fin em R$)

Nota de calibracao: depois do pregao, comparar com o historico que o proprio terminal guardou
(copy_ticks_range de 1 ativo, ~100 s) para medir quanto do volume o polling captura.

Uso: python coletor_agressao.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo-ms 10]
"""
import argparse
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

import banco

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
BUY, SELL, LAST = mt5.TICK_FLAG_BUY, mt5.TICK_FLAG_SELL, mt5.TICK_FLAG_LAST


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class Detector:
    """Transforma a sequencia de leituras do ultimo tick em negocios (lado, volume, preco, hora)."""

    def __init__(self):
        self.ms = None
        self.chave = None          # (ultimo preco, ultimo volume) da leitura anterior
        self.bid = self.ask = 0.0  # cotacao ANTERIOR ao negocio, para inferir o lado
        self.preco_ant = None

    def ler(self, tk):
        negocio = None
        chave = (tk.last, tk.volume)
        if self.ms is not None and tk.last > 0:
            trade_tick = bool(tk.flags & LAST) and (tk.time_msc != self.ms or chave != self.chave)
            # negocio que escapou: o tick mais recente ja e so de cotacao, mas ultimo/volume mudaram
            escapou = (not tk.flags & LAST) and chave != self.chave
            if trade_tick or escapou:
                lado = self._lado(tk, confiavel=trade_tick)
                negocio = (lado, float(tk.volume_real or tk.volume), float(tk.last), int(tk.time_msc))
                self.preco_ant = tk.last
        self.ms, self.chave = tk.time_msc, chave
        self.bid, self.ask = tk.bid, tk.ask
        if negocio is None and self.preco_ant is None and tk.last > 0:
            self.preco_ant = tk.last
        return negocio

    def _lado(self, tk, confiavel):
        compra, venda = bool(tk.flags & BUY), bool(tk.flags & SELL)
        if confiavel and compra != venda:  # exatamente uma flag. Com as duas (~13% dos negocios de acoes:
            return "C" if compra else "V"  # lados misturados no mesmo tick), cai na inferencia pelo preco
        if self.ask > 0 and tk.last >= self.ask:
            return "C"
        if self.bid > 0 and tk.last <= self.bid:
            return "V"
        return "N"


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
    ap.add_argument("--intervalo-ms", type=float, default=10.0)
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)
    if not mt5.initialize():
        raise SystemExit(f"initialize falhou: {mt5.last_error()}")
    for s in CESTA + [FUTURO]:
        mt5.symbol_select(s, True)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    det = {s: Detector() for s in CESTA}
    mins = Minutos()
    tk0 = mt5.symbol_info_tick(FUTURO)
    ultimo_fut = int(tk0.time_msc) if tk0 else 0
    prox_fut = prox_flush = prox_log = 0.0
    pend, gravadas, negocios = [], 0, 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Agressao de {len(CESTA)} acoes + {FUTURO} -> {args.db} (sessao {sessao}). Ctrl+C para parar.", flush=True)

    try:
        while time.time() < fim:
            for s in CESTA:
                tk = mt5.symbol_info_tick(s)
                if tk is None:
                    continue
                n = det[s].ler(tk)
                if n:
                    mins.somar(s, n[3], n[0], n[1], n[2], com_fin=True)
                    negocios += 1

            agora = time.time()
            if agora >= prox_fut:  # futuro: exato via historico de ticks
                prox_fut = agora + 1.0
                tk = mt5.symbol_info_tick(FUTURO)
                if tk is not None and tk.time_msc > ultimo_fut:
                    ticks = mt5.copy_ticks_range(FUTURO, utc(ultimo_fut), utc(int(tk.time_msc) + 1000), mt5.COPY_TICKS_TRADE)
                    for t in (ticks if ticks is not None else []):
                        ms = int(t["time_msc"])
                        if ms <= ultimo_fut:
                            continue
                        ultimo_fut = ms
                        fl = int(t["flags"])
                        lado = "C" if fl & BUY else "V" if fl & SELL else "N"
                        mins.somar(FUTURO, ms, lado, float(t["volume_real"] or t["volume"]), float(t["last"]), com_fin=False)
                        negocios += 1

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
