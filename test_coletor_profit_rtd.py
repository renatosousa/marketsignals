"""Testes do coletor_profit_rtd.py sem Tryd/Profit: servidor RTD falso e DDE falso.

  python -m unittest test_coletor_profit_rtd -v
"""
import sqlite3
import unittest

import coletor_profit_rtd as cp
import dde_tryd


class ServidorFalso:
    """Servidor RTD que devolve sempre 100.0; guarda a fila de atualizacoes que o RefreshData entrega."""

    def __init__(self):
        self.fila = []

    def ConnectData(self, tid, partes, novo):
        return [True, 100.0]

    def RefreshData(self, n):
        fila, self.fila = self.fila, []
        return len(fila), ([t for t, _ in fila], [v for _, v in fila])


def novo_rtd(srv):
    r = object.__new__(cp.Rtd)  # sem COM
    r.srv, r.topicos, r.ids, r.valores, r._prox = srv, {}, {}, {}, cp.TOPICO_BASE
    return r


class DdeFalso:
    """Servidor DDE com `itens` ({item: texto}); item ausente = None (como o timeout do servidor real)."""

    def __init__(self, itens):
        self.itens, self.lidos, self.aberto = itens, [], True

    def ler(self, item):
        self.lidos.append(item)
        return self.itens.get(item)

    def fechar(self):
        self.aberto = False


def janela_saldo(corretoras, ativo="WINZ26"):
    """Itens de uma janela de saldo com `corretoras` linhas preenchidas (texto no formato do Excel pt-BR)."""
    d = {f"{ativo}_SAL.INFO.ATV": ativo, f"{ativo}_SAL.INFO.TAB": "Saldo"}
    for i, nome in enumerate(corretoras):
        for c in cp.FERRAMENTAS["SAL"]["campos"]:
            d[f"{ativo}_SAL.{c}.{i}"] = nome if c == "BRK" else f"{i + 1},5" if c == "PREMED" else str(i + 1)
    return d


class TestRtd(unittest.TestCase):
    def test_ler_ignora_topico_de_outro_cliente(self):
        srv = ServidorFalso()
        rtd = novo_rtd(srv)
        rtd.conectar("A", "ULT")
        srv.fila = [(5, "x"), (cp.TOPICO_BASE, 7.0)]  # 5 pertence a outro cliente do servidor compartilhado
        rtd.ler()
        self.assertEqual(rtd.valores[("A", "ULT")], 7.0)
        self.assertEqual(list(rtd.valores), [("A", "ULT")])


class TestFerramentas(unittest.TestCase):
    def test_janela_fechada_nao_grava(self):
        f = cp.Ferramentas(["WINZ26"], lambda: DdeFalso({}))
        self.assertEqual(f.coletar(1), {})
        self.assertEqual(f.ativas, set())

    def test_janela_aberta_grava_so_linhas_com_corretora(self):
        dde = DdeFalso(janela_saldo(["UBS", "XP", "BTG"]))
        f = cp.Ferramentas(["WINZ26"], lambda: dde)
        ls = f.coletar(123)["rtd_saldo_corretoras"]
        self.assertEqual(f.ativas, {("WINZ26", "SAL")})  # NEGS segue fechada: o DDE falso so tem SAL
        self.assertEqual(len(ls), 3)  # para na primeira corretora vazia, sem ler as 250 linhas
        self.assertEqual(ls[0][:5], (123, "WINZ26", 0, "UBS", 1))
        self.assertEqual(ls[2][3], "BTG")
        self.assertEqual(len(ls[0]), 3 + len(cp.FERRAMENTAS["SAL"]["campos"]))
        self.assertLess(len(dde.lidos), 100)

    def test_virgula_decimal_vira_float(self):
        ls = cp.Ferramentas(["WINZ26"], lambda: DdeFalso(janela_saldo(["UBS"]))).coletar(1)["rtd_saldo_corretoras"]
        self.assertEqual(ls[0][6], 1.5)  # premed "1,5"
        self.assertIsInstance(ls[0][4], int)

    def test_janela_que_fecha_para_de_gravar(self):
        itens = janela_saldo(["UBS"])
        f = cp.Ferramentas(["WINZ26"], lambda: DdeFalso(itens))
        self.assertTrue(f.coletar(1))
        itens["WINZ26_SAL.INFO.ATV"] = ""
        self.assertEqual(f.coletar(2), {})
        self.assertEqual(f.ativas, set())

    def test_servidor_dde_ausente_nao_derruba(self):
        def abrir():
            raise OSError("DDE indisponivel")
        f = cp.Ferramentas(["WINZ26"], abrir)
        self.assertEqual(f.coletar(1), {})
        self.assertEqual(f.coletar(2), {})  # tenta de novo a cada leitura

    def test_fechar_reabre_na_proxima(self):
        conexoes = []

        def abrir():
            conexoes.append(DdeFalso(janela_saldo(["UBS"])))
            return conexoes[-1]
        f = cp.Ferramentas(["WINZ26"], abrir)
        f.coletar(1)
        f.fechar()
        self.assertFalse(conexoes[0].aberto)
        f.coletar(2)
        self.assertEqual(len(conexoes), 2)

    def test_gravacao_no_schema(self):
        f = cp.Ferramentas(["WINZ26"], lambda: DdeFalso(janela_saldo(["A", "B", "C"])))
        db = sqlite3.connect(":memory:")
        db.executescript(cp.SCHEMA)
        for tabela, ls in f.coletar(1).items():
            db.executemany(f"INSERT OR REPLACE INTO {tabela} VALUES ({','.join('?' * len(ls[0]))})", ls)
        self.assertEqual(db.execute("select count(*), min(brk), max(vol) from rtd_saldo_corretoras").fetchone(),
                         (3, "A", 3))


class TestValor(unittest.TestCase):
    def test_conversoes(self):
        self.assertEqual(dde_tryd.valor("33996,85074946"), 33996.85074946)
        self.assertEqual(dde_tryd.valor("-1190772,0000013"), -1190772.0000013)
        self.assertEqual(dde_tryd.valor("796104254"), 796104254)
        self.assertEqual(dde_tryd.valor("-9120"), -9120)
        self.assertEqual(dde_tryd.valor(" XP "), "XP")
        self.assertEqual(dde_tryd.valor("15:33:24.512"), "15:33:24.512")
        self.assertEqual(dde_tryd.valor("="), "=")
        self.assertIsNone(dde_tryd.valor(None))
        self.assertEqual(dde_tryd.valor(""), "")


if __name__ == "__main__":
    unittest.main()
