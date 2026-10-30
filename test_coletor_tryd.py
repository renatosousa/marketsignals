"""Testes do coletor_tryd.py sem Tryd: DDE falso com as janelas de saldo e de negocios.

  python -m unittest test_coletor_tryd -v
"""
import sqlite3
import unittest

import coletor_tryd as ct
import dde_tryd

DIA = 20756
TS = DIA * 86_400_000 + 15 * 3_600_000


class DdeFalso:
    """`itens` = {item: texto}; item ausente = None (o servidor real nao responde). `ao_ler` roda a cada leitura."""

    def __init__(self, itens=None, ao_ler=None):
        self.itens, self.ao_ler, self.lidos, self.aberto = itens if itens is not None else {}, ao_ler, [], True

    def ler(self, item):
        self.lidos.append(item)
        if self.ao_ler:
            self.ao_ler(self, item)
        return self.itens.get(item)

    def fechar(self):
        self.aberto = False


def saldo(corretoras, ativo="WINZ26"):
    d = {f"{ativo}_SAL.INFO.ATV": ativo}
    for i, nome in enumerate(corretoras):
        for c in ct.SALDO_CAMPOS:
            d[f"{ativo}_SAL.{c}.{i}"] = nome if c == "BRK" else "1,5" if c == "PREMED" else str(i + 1)
    return d


def fita(nums, ativo="WINZ26", agressor="Comprador"):
    """Janela de negocios com `nums` do mais novo (linha 0) ao mais antigo."""
    d = {f"{ativo}_NEGS.INFO.ATV": ativo}
    for i, n in enumerate(nums):
        d.update({f"{ativo}_NEGS.NUM.{i}": str(n), f"{ativo}_NEGS.DAT.{i}": f"15:00:00.{i:03d}",
                  f"{ativo}_NEGS.QUL.{i}": "3", f"{ativo}_NEGS.PRE.{i}": "169755", f"{ativo}_NEGS.ACP.{i}": "XP",
                  f"{ativo}_NEGS.AVD.{i}": "Ideal", f"{ativo}_NEGS.AGR.{i}": agressor, f"{ativo}_NEGS.VAR.{i}": "="})
    return d


class TestSaldo(unittest.TestCase):
    def test_janela_fechada(self):
        t = ct.Tryd(lambda: DdeFalso())
        t.conectar()
        self.assertFalse(t.ativa("WINZ26", "SAL"))

    def test_para_na_primeira_corretora_vazia(self):
        dde = DdeFalso(saldo(["UBS", "XP", "BTG"]))
        t = ct.Tryd(lambda: dde)
        t.conectar()
        self.assertTrue(t.ativa("WINZ26", "SAL"))
        ls = t.saldo(TS, "WINZ26")
        self.assertEqual([r[3] for r in ls], ["UBS", "XP", "BTG"])
        self.assertEqual(ls[0][:3], (TS, "WINZ26", 0))
        self.assertEqual(len(ls[0]), 3 + len(ct.SALDO_CAMPOS))
        self.assertEqual(ls[0][6], 1.5)  # premed "1,5" -> float
        self.assertLess(len(dde.lidos), 100)  # nao le as 250 linhas


class TestFita(unittest.TestCase):
    def setUp(self):
        self.dde = DdeFalso(fita([50, 40, 30]))
        self.t = ct.Tryd(lambda: self.dde)
        self.t.conectar()

    def test_primeira_leitura_traz_tudo_do_mais_antigo_ao_mais_novo(self):
        novos = self.t.fita(TS, "WINZ26")
        self.assertEqual([r[2] for r in novos], [30, 40, 50])
        self.assertEqual(novos[0], (DIA, "WINZ26", 30, TS, "15:00:00.002", 3, 169755, "XP", "Ideal", "Comprador", "="))

    def test_so_negocios_novos(self):
        self.t.fita(TS, "WINZ26")
        self.dde.itens.update(fita([70, 60, 50, 40]))
        self.assertEqual([r[2] for r in self.t.fita(TS, "WINZ26")], [60, 70])
        self.assertEqual(self.t.fita(TS, "WINZ26"), [])

    def test_linha_que_mudou_durante_a_leitura_fica_para_a_proxima(self):
        def ao_ler(dde, item):  # a janela e redesenhada (entra o 60) entre a leitura do NUM.0 e a conferencia dele
            if item == "WINZ26_NEGS.NUM.0" and dde.lidos.count(item) == 2:
                dde.itens.update(fita([60, 50, 40]))
        self.dde.ao_ler = ao_ler
        primeiro = {r[2]: r for r in self.t.fita(TS, "WINZ26")}
        # o 50 foi lido na linha 0 (descartado: o numero mudou) e depois na linha 1 com os detalhes certos
        self.assertEqual(sorted(primeiro), [40, 50])
        self.assertEqual(primeiro[50][4], "15:00:00.001")
        self.assertEqual(primeiro[40][4], "15:00:00.002")
        self.dde.ao_ler = None
        self.assertEqual([r[2] for r in self.t.fita(TS, "WINZ26")], [60])

    def test_nao_regrava_quando_o_tryd_reabre(self):
        db = sqlite3.connect(":memory:")
        db.executescript(ct.SCHEMA)
        for _ in range(2):  # 2 leituras com a memoria zerada: a chave (dia, ativo, num) evita duplicata
            t = ct.Tryd(lambda: self.dde)
            t.conectar()
            ct.gravar(db, "tryd_fita", t.fita(TS, "WINZ26"), True)
        self.assertEqual(db.execute("select count(*) from tryd_fita").fetchone(), (3,))

    def test_memoria_de_vistos_nao_cresce_sem_limite(self):
        t = ct.Tryd(lambda: self.dde)
        t.vistos["WINZ26"] = set(range(0, 10 * (ct.VISTOS_MAX + 100), 10))
        self.dde.itens.update(fita([10 * (ct.VISTOS_MAX + 100) + 10]))
        t.conectar()
        t.fita(TS, "WINZ26")
        self.assertLess(len(t.vistos["WINZ26"]), ct.VISTOS_MAX)


class TestBanco(unittest.TestCase):
    def test_gravacao_no_schema(self):
        db = sqlite3.connect(":memory:")
        db.executescript(ct.SCHEMA)
        t = ct.Tryd(lambda: DdeFalso({**saldo(["A", "B", "C"]), **fita([20, 10])}))
        t.conectar()
        ct.gravar(db, "tryd_saldo", t.saldo(TS, "WINZ26"))
        ct.gravar(db, "tryd_fita", t.fita(TS, "WINZ26"), True)
        self.assertEqual(db.execute("select count(*), min(brk), max(vol) from tryd_saldo").fetchone(), (3, "A", 3))
        self.assertEqual(db.execute("select count(*), sum(qtd), min(comprador) from tryd_fita").fetchone(), (2, 6, "XP"))

    def test_dde_ausente_levanta_oserror(self):
        def abrir():
            raise OSError("Tryd fechado")
        with self.assertRaises(OSError):
            ct.Tryd(abrir).conectar()

    def test_valor(self):
        self.assertEqual(dde_tryd.valor("33996,85"), 33996.85)


if __name__ == "__main__":
    unittest.main()
