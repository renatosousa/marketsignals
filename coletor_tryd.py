"""Corretoras do Tryd lidas por DDE (profitchart|TT, sem Excel) -> book.db. Substitui a coleta do Profit (RTD).

Duas janelas do Tryd, por contrato (a janela precisa estar aberta e com a exportacao DDE ligada; ver dde_tryd.py):
  <ativo>_SAL   "Saldo por corretora": acumulado do dia por corretora, completo. Foto a cada --saldo s em tryd_saldo.
                qtd = qtdc - qtdv e vol = volc - volv (liquidos, contratos e R$); sagr = agrc - agrv (saldo de agressao);
                spas = qtd - sagr; premed e plb em R$ por contrato. O fluxo de um intervalo sai da diferenca entre fotos.
  <ativo>_NEGS  "Negocios": a janela guarda so os 31 ultimos e o Tryd a redesenha ~4x/s, entao nos picos (WIN > 120
                negocios/s) parte da fita se perde: 73% capturado em trecho normal e 45% num pico (30/10/2026). Cada
                negocio novo entra uma vez em tryd_fita (chave: dia, ativo, num); buracos no `num` (passo de 10 por
                negocio) medem o que faltou. A fita da corretora de cada ponta, o agressor (Comprador/Vendedor/RLP...) e a
                hora do negocio; o saldo acima e a fonte exata dos totais.

Tabelas (ts_ms = relogio de Brasilia como epoch "UTC", convencao do banco; dia = ts_ms // 86400000):
  tryd_saldo  ts_ms, ativo, linha, brk, vol, qtd, premed, agrc, agrv, sagr, spas, plb, qtdc, qtdv, volc, volv
  tryd_fita   dia, ativo, num, ts_ms (captura), hora (do negocio, hh:mm:ss.mmm), qtd, preco, comprador, vendedor, agressor, var

Acoes (--acoes): so o saldo por corretora, a cada --saldo-acoes s (padrao 60): a B3 publica o fluxo por tipo de investidor
(estrangeiro, institucional, pessoa fisica...) so para acoes, com 2 pregoes de atraso, e e com elas que se valida o mapa
corretora -> tipo de investidor. A janela de cada acao precisa estar aberta no Tryd.

Uso: python coletor_tryd.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--ativos WINZ26] [--acoes PETR4,...] [--saldo 10]
                            [--saldo-acoes 60] [--fita 0.125]
"""
import argparse
import time
from datetime import datetime

import banco
from dde_tryd import Dde, valor
from relogio import local_br_ms

ATIVOS = ("WINZ26",)  # saldo + fita; rolagem do WIN: trocar quando o contrato vencer
ACOES = ("PETR4", "VALE3", "ITUB4", "BBDC4", "BOVA11")  # so saldo: valida corretora -> tipo de investidor (D+2 da B3, acoes)
SALDO_CAMPOS = ["BRK", "VOL", "QTD", "PREMED", "AGRC", "AGRV", "SAGR", "SPAS", "PLB", "QTDC", "QTDV", "VOLC", "VOLV"]
FITA_CAMPOS = ["DAT", "QUL", "PRE", "ACP", "AVD", "AGR", "VAR"]  # + NUM, lido a parte
LINHAS_SALDO, LINHAS_FITA = 500, 31
VISTOS_MAX = 50_000  # numeros de negocio lembrados por ativo (o `num` so cresce dentro do dia)
SCHEMA = """
CREATE TABLE IF NOT EXISTS tryd_saldo (
    ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, linha INTEGER NOT NULL,
    brk TEXT, vol REAL, qtd REAL, premed REAL, agrc REAL, agrv REAL, sagr REAL, spas REAL, plb REAL,
    qtdc REAL, qtdv REAL, volc REAL, volv REAL,
    PRIMARY KEY (ts_ms, ativo, linha)
);
CREATE TABLE IF NOT EXISTS tryd_fita (
    dia INTEGER NOT NULL, ativo TEXT NOT NULL, num INTEGER NOT NULL, ts_ms INTEGER, hora TEXT, qtd REAL, preco REAL,
    comprador TEXT, vendedor TEXT, agressor TEXT, var TEXT,
    PRIMARY KEY (dia, ativo, num)
);
"""


def vazio(v):
    return v is None or v in ("", "-")


class Tryd:
    """Leitor das janelas de corretoras. `abrir` cria a conexao DDE (injetavel nos testes)."""

    def __init__(self, abrir=Dde):
        self._abrir, self.dde, self.vistos = abrir, None, {}

    def conectar(self):
        if self.dde is None:
            self.dde = self._abrir()  # OSError se o Tryd estiver fechado

    def _ler(self, item):
        return valor(self.dde.ler(item))

    def ativa(self, ativo, ferr):
        """True se a janela <ativo>_<ferr> esta aberta e exportando."""
        return not vazio(self._ler(f"{ativo}_{ferr}.INFO.ATV"))

    def saldo(self, ts, ativo):
        """[(ts, ativo, linha, *campos)] das corretoras da janela; a tabela termina na primeira corretora vazia."""
        linhas = []
        for i in range(LINHAS_SALDO):
            vals = [self._ler(f"{ativo}_SAL.{c}.{i}") for c in SALDO_CAMPOS]
            if vazio(vals[0]):
                break
            linhas.append((ts, ativo, i, *vals))
        return linhas

    def fita(self, ts, ativo):
        """Negocios que apareceram desde a ultima chamada, do mais antigo ao mais novo: (dia, ativo, num, ts, hora, qtd,
        preco, comprador, vendedor, agressor, var). A linha so vale se o numero continua o mesmo depois de ler os
        detalhes (a janela e redesenhada a qualquer momento e empurra as linhas); senao fica para a proxima leitura."""
        nome, vistos, novos = f"{ativo}_NEGS", self.vistos.setdefault(ativo, set()), []
        for i in range(LINHAS_FITA):
            n = self._ler(f"{nome}.NUM.{i}")
            if not isinstance(n, int) or n in vistos:
                continue
            det = [self._ler(f"{nome}.{c}.{i}") for c in FITA_CAMPOS]
            if self._ler(f"{nome}.NUM.{i}") != n:
                continue
            vistos.add(n)
            hora, qtd, preco, comprador, vendedor, agressor, var = det
            novos.append((ts // 86_400_000, ativo, n, ts, hora, qtd, preco, comprador, vendedor, agressor, var))
        if len(vistos) > VISTOS_MAX:
            corte = max(vistos) - VISTOS_MAX * 5  # `num` anda de 10 em 10: guarda a metade mais recente
            vistos.intersection_update({x for x in vistos if x >= corte})
        return sorted(novos, key=lambda r: r[2])

    def fechar(self):
        if self.dde is not None:
            try:
                self.dde.fechar()
            finally:
                self.dde = None


def gravar(db, tabela, linhas, ignorar=False):
    if linhas:
        verbo = "INSERT OR IGNORE" if ignorar else "INSERT OR REPLACE"
        db.executemany(f"{verbo} INTO {tabela} VALUES ({','.join('?' * len(linhas[0]))})", linhas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="TRYD")
    ap.add_argument("segundos", nargs="?", type=float, default=0.0, help="0 = sem limite")
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--ativos", default=",".join(ATIVOS), help="contratos com janela aberta no Tryd; padrao %(default)s")
    ap.add_argument("--acoes", default=",".join(ACOES), help="acoes so com saldo (vazio desliga); padrao %(default)s")
    ap.add_argument("--saldo", type=float, default=10.0, help="s entre fotos do saldo por corretora dos contratos")
    ap.add_argument("--saldo-acoes", type=float, default=60.0, help="s entre fotos do saldo por corretora das acoes")
    ap.add_argument("--fita", type=float, default=0.125, help="s entre leituras da fita (a janela redesenha ~4x/s)")
    args = ap.parse_args()
    ativos = [a for a in args.ativos.split(",") if a]
    acoes = [a for a in args.acoes.split(",") if a]

    db = banco.abrir(args.db, SCHEMA)
    tryd, fim = Tryd(), time.time() + args.segundos if args.segundos > 0 else float("inf")
    ativas, logado = {}, {}
    prox_saldo = prox_acoes = prox_flush = 0.0
    pendente, n_saldo, n_fita = [], 0, 0

    def log(chave, texto):
        if logado.get(chave) != texto:
            logado[chave] = texto
            print(f"[{datetime.now():%H:%M:%S}] {chave}: {texto}", flush=True)

    print(f"Tryd (DDE profitchart|TT): {','.join(ativos)} -> {args.db}; saldo a cada {args.saldo:.0f} s, fita a cada "
          f"{args.fita * 1000:.0f} ms; acoes ({','.join(acoes) or '-'}): saldo a cada {args.saldo_acoes:.0f} s. "
          f"Ctrl+C para parar.", flush=True)

    def foto(ts, lista, ferrs):
        nonlocal n_saldo
        for a in lista:
            for ferr in ferrs:
                ativas[(a, ferr)] = tryd.ativa(a, ferr)
                log(f"{a}_{ferr}", "ativa" if ativas[(a, ferr)] else "inativa: abra a janela no Tryd com a "
                                                                     "exportacao DDE ligada")
            if ativas[(a, "SAL")]:
                ls = tryd.saldo(ts, a)
                pendente.append(("tryd_saldo", ls, False))
                n_saldo += len(ls)
    try:
        while time.time() < fim:
            t = time.time()
            try:
                tryd.conectar()
                ts = int(local_br_ms())
                if t >= prox_saldo:  # foto do saldo e reteste das janelas
                    prox_saldo = t + args.saldo
                    foto(ts, ativos, ("SAL", "NEGS"))
                if t >= prox_acoes and acoes:
                    prox_acoes = t + args.saldo_acoes
                    foto(ts, acoes, ("SAL",))
                for a in ativos:
                    if ativas.get((a, "NEGS")):
                        novos = tryd.fita(ts, a)
                        pendente.append(("tryd_fita", novos, True))
                        n_fita += len(novos)
            except OSError as e:  # Tryd fechado: tenta de novo em 30 s
                log("DDE", str(e))
                tryd.fechar()
                ativas.clear()
                prox_saldo = prox_acoes = time.time() + 30
                time.sleep(30)
                continue
            except Exception as e:  # leitura quebrou no meio: reabre a conexao na proxima volta
                log("DDE", f"leitura falhou ({e.__class__.__name__}: {e}); reconectando")
                tryd.fechar()
                ativas.clear()
                prox_saldo = prox_acoes = 0.0
                time.sleep(2)
                continue
            if t >= prox_flush and pendente:
                prox_flush = t + 2.0
                try:
                    for tabela, ls, ign in pendente:
                        gravar(db, tabela, ls, ign)
                    db.commit()
                    pendente.clear()
                except Exception as e:  # banco ocupado: mantem a fila e tenta no proximo ciclo
                    db.rollback()
                    log("banco", f"gravacao adiada ({e})")
            time.sleep(max(0.0, args.fita - (time.time() - t)))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            for tabela, ls, ign in pendente:
                gravar(db, tabela, ls, ign)
            db.commit()
        except Exception:
            db.rollback()
        tryd.fechar()
        db.close()
    print(f"linhas de saldo {n_saldo}, negocios da fita {n_fita}", flush=True)


if __name__ == "__main__":
    main()
