"""Saldo de agressao calculado pelo Profit (Nelogica), lido pelo servidor RTD -> tabela profit_rtd (book.db).

Para comparar com a agressao que medimos pelos ticks do MT5 (ticks_trade.py). O MT5 agrupa varios negocios num
tick e marca o tick inteiro como compra ou venda; o Profit recebe cada negocio da B3 com o tipo (agressao, RLP,
direto, leilao...) e deixa ~27% do volume do WIN sem agressor (28/10/2026, 09:55: 72% do volume com agressor no
Profit contra 98% no MT5). O saldo do Profit fica bem menor que o nosso; as duas series vao para o banco.

Le o RTD sem Excel: implementa o cliente IRtdServer com comtypes (a typelib do servidor do Profit traz as
interfaces IRtdServer e IRTDUpdateEvent). O Profit precisa estar aberto e logado; sem ele o coletor espera, e
nunca cria o servidor COM sozinho (isso abriria o Profit).

Tabela profit_rtd (uma linha por ativo a cada --intervalo s; ts_ms = relogio de Brasilia como epoch "UTC"):
  ts_ms, symbol, hora (do ultimo negocio no Profit), ultimo, negocios, quantidade,
  agr_vol_c, agr_vol_v, agr_saldo (contratos/acoes), agr_neg_c, agr_neg_v, agr_saldo_neg (numero de negocios)

Corretoras (30/10/2026): as janelas "Saldo por corretora" (<ativo>_SAL, ate 500 linhas) e "Negocios" (<ativo>_NEGS,
so os ultimos 31) do Tryd NAO saem pelo RTD (sempre "Ferramenta Invalida"); saem pelo DDE `profitchart|TT`, lido por
dde_tryd.py (so leitura, sem Excel). A cada leitura o coletor testa `<ativo>_<ferr>.INFO.ATV`: vazio = sem janela
exportando, e a ferramenta so e lida quando a janela esta aberta no Tryd. Tabelas (valores ja convertidos: a virgula
decimal do DDE vira float):
  rtd_saldo_corretoras  ts_ms, ativo, linha, brk, vol, qtd, premed, agrc, agrv, sagr, spas, plb, qtdc, qtdv, volc, volv
  rtd_negs_amostra      ts_ms, ativo, linha, dat, qul, pre, num, acp, avd, agr, var
No saldo, acumulado do dia: qtd = qtdc - qtdv e vol = volc - volv (liquidos, em contratos e R$; a janela ordena pelo
saldo liquido), sagr = agrc - agrv, spas = qtd - sagr. O fluxo de cada minuto sai da diferenca entre leituras.

O servidor RTD e UNICO e compartilhado: um segundo cliente (sonda, Excel) que numere topicos a partir de 1 sobrescreve
e desconecta os deste coletor e rouba as atualizacoes do RefreshData (30/10/2026: o coletor ficou 10 min congelado).
Por isso os ids daqui comecam em TOPICO_BASE, so se desconecta o que e proprio, e leituras paradas (a hora do ultimo
negocio do WIN igual por 5 leituras no pregao) forcam a reconexao.

Uso: python coletor_profit_rtd.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo 60]
"""
import argparse
import subprocess
import time
from datetime import datetime

import comtypes
import comtypes.client
from comtypes import COMObject

import banco
from dde_tryd import Dde, valor
from relogio import local_br_ms

TYPELIB = ("{EFCFBDCA-78A5-450B-8228-346C4F44D5B8}", 1, 0)  # RTDTrading.RTDServer (profitchart.exe)
ATIVOS = (["WINFUT_F_0", "WINZ26_F_0", "WDOFUT_F_0", "BOVA11_B_0"] +
          [f"{s}_B_0" for s in ("PETR4", "VALE3", "ITUB4", "BBDC4", "BBAS3", "BPAC11", "B3SA3", "PETR3", "PRIO3",
                                "SBSP3", "ABEV3", "WEGE3")])
CAMPOS = {"HOR": "hora", "ULT": "ultimo", "NEG": "negocios", "QTT": "quantidade", "AGRVOLC": "agr_vol_c",
          "AGRVOLV": "agr_vol_v", "AGRSALDO": "agr_saldo", "AGRNEGC": "agr_neg_c", "AGRNEGV": "agr_neg_v",
          "AGRSALDONEG": "agr_saldo_neg"}
# contratos cujas janelas de corretoras sao lidas (rolagem do WIN: trocar quando o contrato vencer, como ATIVOS)
CORRETORAS_ATIVOS = ("WINZ26",)
FERRAMENTAS = {
    "SAL": dict(tabela="rtd_saldo_corretoras", linhas=250, chave="BRK",
                campos=["BRK", "VOL", "QTD", "PREMED", "AGRC", "AGRV", "SAGR", "SPAS", "PLB", "QTDC", "QTDV", "VOLC",
                        "VOLV"]),
    "NEGS": dict(tabela="rtd_negs_amostra", linhas=31, chave="NUM",
                 campos=["DAT", "QUL", "PRE", "NUM", "ACP", "AVD", "AGR", "VAR"]),
}
TOPICO_BASE = 1_000_000  # outros clientes do servidor compartilhado numeram os topicos a partir de 1
PARADA_MAX = 5           # leituras seguidas com a mesma hora do ultimo negocio do WIN, no pregao, = servidor travado
SCHEMA = """
CREATE TABLE IF NOT EXISTS profit_rtd (
    ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, hora TEXT, ultimo REAL, negocios INTEGER, quantidade REAL,
    agr_vol_c REAL, agr_vol_v REAL, agr_saldo REAL, agr_neg_c REAL, agr_neg_v REAL, agr_saldo_neg REAL,
    PRIMARY KEY (symbol, ts_ms)
);
CREATE TABLE IF NOT EXISTS rtd_saldo_corretoras (
    ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, linha INTEGER NOT NULL,
    brk, vol, qtd, premed, agrc, agrv, sagr, spas, plb, qtdc, qtdv, volc, volv,
    PRIMARY KEY (ts_ms, ativo, linha)
);
CREATE TABLE IF NOT EXISTS rtd_negs_amostra (
    ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, linha INTEGER NOT NULL,
    dat, qul, pre, num, acp, avd, agr, var,
    PRIMARY KEY (ts_ms, ativo, linha)
);
"""


def profit_aberto():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq profitchart.exe", "/NH"], capture_output=True, text=True)
    return "profitchart.exe" in r.stdout.lower()


class Rtd:
    """Cliente IRtdServer minimo: conecta topicos (ativo, atributo) e guarda o ultimo valor de cada um em
    self.valores, indexado pela tupla do topico."""

    def __init__(self, ativos, atributos):
        mod = comtypes.client.GetModule(TYPELIB)

        class Callback(COMObject):
            _com_interfaces_ = [mod.IRTDUpdateEvent]

            def UpdateNotify(self):
                pass

            def Disconnect(self):
                pass

            def _get_HeartbeatInterval(self):
                return 15000

            def _set_HeartbeatInterval(self, v):
                pass

        self.srv = comtypes.client.CreateObject("RTDTrading.RTDServer", interface=mod.IRtdServer)
        self.cb = Callback()  # referencia viva enquanto o servidor estiver em uso
        self.srv.ServerStart(self.cb)
        self.topicos, self.ids, self.valores, self._prox = {}, {}, {}, TOPICO_BASE
        for ativo in ativos:
            for at in atributos:
                self.conectar(ativo, at)

    def conectar(self, *partes):
        """Assina um topico e devolve o valor inicial."""
        tid, self._prox = self._prox, self._prox + 1
        self.topicos[tid], self.ids[partes] = partes, tid
        self.valores[partes] = self.srv.ConnectData(tid, list(partes), True)[1]
        return self.valores[partes]

    def ler(self):
        n, dados = self.srv.RefreshData(0)
        if n:
            for tid, v in zip(*dados):
                partes = self.topicos.get(int(tid))
                if partes is not None:  # id de outro cliente do servidor compartilhado: ignora
                    self.valores[partes] = v
        return self.valores

    def fechar(self):
        try:
            for tid in self.topicos:
                self.srv.DisconnectData(tid)
            self.srv.ServerTerminate()
        except Exception:
            pass


def vazio(v):
    return v is None or (isinstance(v, str) and v in ("", "-"))


class Ferramentas:
    """Janelas de corretoras do Tryd lidas por DDE (ver a docstring do modulo). `abrir` cria a conexao DDE."""

    def __init__(self, ativos, abrir=Dde):
        self.ativos, self._abrir, self.dde = tuple(ativos), abrir, None
        self.ativas = set()  # (ativo, ferramenta) com a janela aberta na ultima leitura
        self.logado = {}

    def _log(self, chave, texto):
        if self.logado.get(chave) != texto:
            self.logado[chave] = texto
            print(f"[{datetime.now():%H:%M:%S}] corretoras {chave}: {texto}", flush=True)

    def _ler(self, item):
        return valor(self.dde.ler(item))

    def coletar(self, ts):
        """{tabela: [(ts, ativo, linha, *campos)]} das janelas abertas; linha sem a chave (corretora/numero) encerra a tabela."""
        saida = {}
        if self.dde is None:
            try:
                self.dde = self._abrir()
            except OSError as e:
                self._log("DDE", str(e))
                return saida
            self._log("DDE", "conectado a profitchart|TT")
        for ativo in self.ativos:
            for ferr, cfg in FERRAMENTAS.items():
                chave, nome = (ativo, ferr), f"{ativo}_{ferr}"
                if vazio(self._ler(f"{nome}.INFO.ATV")):
                    if chave in self.ativas:
                        self.ativas.discard(chave)
                    self._log(nome, "inativa: abra a janela no Tryd com a exportacao DDE ligada")
                    continue
                k, linhas = cfg["campos"].index(cfg["chave"]), []
                for i in range(cfg["linhas"]):
                    vals = [self._ler(f"{nome}.{c}.{i}") for c in cfg["campos"]]
                    if vazio(vals[k]):
                        break
                    linhas.append((ts, ativo, i, *vals))
                self.ativas.add(chave)
                self._log(nome, "ativa")
                saida[cfg["tabela"]] = linhas
        return saida

    def fechar(self):
        if self.dde is not None:
            self.dde.fechar()
            self.dde = None


def em_pregao():
    return "09:10" <= datetime.now().strftime("%H:%M") <= "17:50"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="PROFIT")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=60.0)
    ap.add_argument("--corretoras", default=",".join(CORRETORAS_ATIVOS),
                    help="contratos das janelas de corretoras a ler (vazio desliga); padrao %(default)s")
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)
    comtypes.CoInitialize()
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    rtd, gravadas, avisou = None, 0, False
    ferr = Ferramentas([a for a in args.corretoras.split(",") if a])
    hora_ant, parada = None, 0
    print(f"RTD do Profit: {len(ATIVOS)} ativos -> {args.db} (a cada {args.intervalo:.0f} s). Ctrl+C para parar.",
          flush=True)
    try:
        while time.time() < fim:
            if rtd is None:
                if not profit_aberto():
                    if not avisou:
                        print(f"[{datetime.now():%H:%M:%S}] Profit fechado: aguardando", flush=True)
                        avisou = True
                    time.sleep(30)
                    continue
                try:
                    rtd = Rtd(ATIVOS, list(CAMPOS))
                    hora_ant, parada, avisou = None, 0, False
                    print(f"[{datetime.now():%H:%M:%S}] conectado ao RTD do Profit", flush=True)
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] aviso: RTD indisponivel ({e}); tento em 60 s", flush=True)
                    time.sleep(60)
                    continue
            prox = time.time() + args.intervalo
            try:
                v = rtd.ler()
            except Exception as e:  # Profit fechado ou reiniciado no meio do dia: reconecta
                print(f"[{datetime.now():%H:%M:%S}] aviso: leitura do RTD falhou ({e}); reconectando", flush=True)
                rtd.fechar()
                rtd = None
                continue
            hora = v.get(("WINFUT_F_0", "HOR"))
            parada = parada + 1 if hora is not None and hora == hora_ant else 0
            hora_ant = hora
            if parada >= PARADA_MAX and em_pregao():  # o pregao do WIN negocia todo segundo: leitura parada = travada
                print(f"[{datetime.now():%H:%M:%S}] aviso: RTD parado em {hora} ha {parada} leituras; reconectando",
                      flush=True)
                rtd.fechar()
                rtd = None
                continue
            agora = int(local_br_ms())
            linhas = [(agora, a, *(v.get((a, at)) for at in CAMPOS)) for a in ATIVOS]
            try:
                db.executemany(f"INSERT OR REPLACE INTO profit_rtd VALUES ({','.join('?' * (2 + len(CAMPOS)))})",
                               linhas)
                db.commit()
                gravadas += len(linhas)
            except Exception as e:  # banco ocupado: perde esta leitura, a proxima vem em `intervalo` s
                db.rollback()
                print(f"aviso: gravacao adiada ({e})", flush=True)
            if ferr.ativos:
                try:
                    for tabela, ls in ferr.coletar(agora).items():
                        if not ls:
                            continue
                        db.executemany(f"INSERT OR REPLACE INTO {tabela} VALUES ({','.join('?' * len(ls[0]))})", ls)
                    db.commit()
                except Exception as e:  # as corretoras nunca derrubam a leitura do Profit
                    db.rollback()
                    ferr.fechar()  # reabre a conexao DDE na proxima leitura
                    print(f"[{datetime.now():%H:%M:%S}] aviso: corretoras falharam ({e.__class__.__name__}: {e})",
                          flush=True)
            while time.time() < prox:  # o servidor so atualiza com mensagens do Windows sendo processadas
                comtypes.client.PumpEvents(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if rtd is not None:
            rtd.fechar()
        ferr.fechar()
        db.close()
    print(f"linhas gravadas {gravadas}", flush=True)


if __name__ == "__main__":
    main()
