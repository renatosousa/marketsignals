"""Testes da leitura das janelas de corretoras do coletor_profit_rtd.py com um servidor RTD falso (sem Tryd/Profit).

  python -m unittest test_coletor_profit_rtd -v
"""
import sqlite3
import unittest

import coletor_profit_rtd as cp


class ServidorFalso:
    """So conhece as ferramentas de `tools` ({nome: funcao(partes) -> valor}); as demais devolvem o erro do Tryd."""

    def __init__(self, tools=None):
        self.tools = tools or {}
        self.desconectados, self.fila = [], []

    def ConnectData(self, tid, partes, novo):
        if len(partes) == 3:
            f = self.tools.get(partes[0])
            return [True, f(partes) if f else "Ferramenta Inválida"]
        return [True, 100.0]

    def DisconnectData(self, tid):
        self.desconectados.append(tid)

    def RefreshData(self, n):
        fila, self.fila = self.fila, []
        return len(fila), ([t for t, _ in fila], [v for _, v in fila])


def novo_rtd(srv):
    r = object.__new__(cp.Rtd)  # sem COM
    r.srv, r.topicos, r.ids, r.valores, r._prox = srv, {}, {}, {}, cp.TOPICO_BASE
    return r


def saldo(partes):
    _, campo, i = partes
    if campo == "INFO":
        return "WINZ26"
    if int(i) < 3:  # 3 corretoras preenchidas; as demais linhas vem vazias
        return f"CORR{i}" if campo == "BRK" else float(i) + 1
    return "" if campo == "BRK" else 0.0


class TestFerramentas(unittest.TestCase):
    def test_ferramenta_inativa_nao_assina_nem_grava(self):
        srv = ServidorFalso()
        rtd = novo_rtd(srv)
        f = cp.Ferramentas(rtd, ["WINZ26"])
        f.atualizar()
        self.assertEqual((f.ativas, rtd.topicos, f.linhas(1)), (set(), {}, {}))
        self.assertEqual(srv.desconectados, [cp.TOPICO_BASE, cp.TOPICO_BASE + 1])  # so os topicos de prova (SAL e NEGS)

    def test_ferramenta_ativa_grava_so_linhas_com_corretora(self):
        rtd = novo_rtd(ServidorFalso({"WINZ26_SAL": saldo}))
        f = cp.Ferramentas(rtd, ["WINZ26"])
        f.atualizar()
        self.assertEqual(f.ativas, {("WINZ26", "SAL")})  # NEGS segue inativa: o servidor falso so tem SAL
        ls = f.linhas(123)["rtd_saldo_corretoras"]
        self.assertEqual(len(ls), 3)
        self.assertEqual(ls[0][:4], (123, "WINZ26", 0, "CORR0"))
        self.assertEqual(len(ls[0]), 3 + len(cp.FERRAMENTAS["SAL"]["campos"]))
        self.assertTrue(all(t >= cp.TOPICO_BASE for t in rtd.topicos))  # nunca ocupa os ids baixos de outros clientes

    def test_janela_que_fecha_solta_so_os_proprios_topicos(self):
        srv = ServidorFalso({"WINZ26_SAL": saldo})
        rtd = novo_rtd(srv)
        outro = rtd.conectar("PETR4_B_0", "ULT")  # topico do proprio coletor: nao pode ser solto
        f = cp.Ferramentas(rtd, ["WINZ26"])
        f.atualizar()
        n = len(rtd.topicos)
        srv.fila = [(rtd.ids[("WINZ26_SAL", "INFO", "ATV")], "Ferramenta Inválida")]
        rtd.ler()
        f.atualizar()
        self.assertEqual(f.ativas, set())
        self.assertEqual(list(rtd.valores), [("PETR4_B_0", "ULT")])
        self.assertEqual(outro, 100.0)
        # provas: SAL e NEGS na 1a rodada + NEGS de novo na 2a; mais os topicos da SAL, menos o do proprio coletor
        self.assertEqual(len(srv.desconectados), 3 + n - 1)

    def test_ler_ignora_topico_de_outro_cliente(self):
        srv = ServidorFalso()
        rtd = novo_rtd(srv)
        rtd.conectar("A", "ULT")
        srv.fila = [(5, "x"), (cp.TOPICO_BASE, 7.0)]  # 5 pertence a outro cliente do servidor compartilhado
        rtd.ler()
        self.assertEqual(rtd.valores[("A", "ULT")], 7.0)

    def test_gravacao_no_schema(self):
        rtd = novo_rtd(ServidorFalso({"WINZ26_SAL": saldo}))
        f = cp.Ferramentas(rtd, ["WINZ26"])
        f.atualizar()
        db = sqlite3.connect(":memory:")
        db.executescript(cp.SCHEMA)
        for tabela, ls in f.linhas(1).items():
            db.executemany(f"INSERT OR REPLACE INTO {tabela} VALUES ({','.join('?' * len(ls[0]))})", ls)
        self.assertEqual(db.execute("select count(*), min(brk), max(vol) from rtd_saldo_corretoras").fetchone(),
                         (3, "CORR0", 3.0))

    def test_valores_estranhos_viram_texto(self):
        import datetime
        self.assertEqual(cp.cru(datetime.date(2026, 10, 30)), "2026-10-30")
        self.assertEqual((cp.cru(None), cp.cru(1.5), cp.cru("a")), (None, 1.5, "a"))
        self.assertTrue(cp.vazio("") and cp.vazio(None) and cp.vazio("Ferramenta Inválida") and not cp.vazio(0.0))


if __name__ == "__main__":
    unittest.main()
