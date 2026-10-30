"""Testes do players.py com dados sinteticos.

  python -m unittest test_players -v
"""
import sqlite3
import unittest

import coletor_tryd as ct
import players

DIA = 20756
T0 = DIA * 86_400_000 + 12 * 3_600_000


def banco(saldo, fita=()):
    db = sqlite3.connect(":memory:")
    db.executescript(ct.SCHEMA)
    db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd, sagr, qtdc, qtdv) VALUES (?,?,?,?,?,?,?,?)",
                   [(ts, "WINZ26", i, b, q, s, c, v) for i, (ts, b, q, s, c, v) in enumerate(saldo)])
    db.executemany("INSERT INTO tryd_fita VALUES (?,?,?,?,?,?,?,?,?,?,?)", fita)
    return db


class TestSaldo(unittest.TestCase):
    def test_grupos_somam_zero_e_corretora_nova_comeca_em_zero(self):
        saldo = [(T0, "Goldman", 10, 4, 10, 0), (T0, "XP", -10, -4, 0, 10),
                 (T0 + 10_000, "Goldman", 30, 9, 30, 0), (T0 + 10_000, "XP", -25, -9, 5, 30),
                 (T0 + 10_000, "Zzz", -5, 0, 0, 5)]
        r = players.montar(banco(saldo), DIA)
        self.assertEqual(r["t"], [T0 // 1000, T0 // 1000 + 10])
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [10, 30])
        self.assertEqual(r["series"]["Misto"]["saldo"], [-10, -25])
        self.assertEqual(r["series"]["Outros"]["saldo"], [0, -5])  # Zzz so aparece na 2a foto
        for i in range(2):
            self.assertEqual(sum(s["saldo"][i] for s in r["series"].values()), 0)
        self.assertEqual(r["corretoras"][0]["corretora"], "XP")  # maior volume

    def test_sem_dados(self):
        self.assertTrue(players.montar(banco([]), None)["vazio"])
        self.assertTrue(players.montar(banco([(T0, "XP", 1, 1, 1, 0)]), DIA + 5)["vazio"])

    def test_dia_padrao_e_ultimo_com_saldo(self):
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0), (T0 + DIA_MS, "XP", 2, 2, 2, 0)]), None)
        self.assertEqual(r["dia"], DIA + 1)
        self.assertEqual(r["dias"], [DIA, DIA + 1])

    def test_reduz_a_600_pontos(self):
        saldo = [(T0 + i * 10_000, "XP", i, 0, i, 0) for i in range(2000)]
        r = players.montar(banco(saldo), DIA)
        self.assertLessEqual(len(r["t"]), players.PONTOS)
        self.assertEqual(r["series"]["Misto"]["saldo"][-1], 1999)  # inclui a ultima foto


DIA_MS = players.DIA_MS


class TestRlp(unittest.TestCase):
    def test_rlp_por_grupo_e_percentual(self):
        fita = [(DIA, "WINZ26", 10, T0, "12:00:00.000", 5, 170000, "Goldman", "XP", "RLP", "="),
                (DIA, "WINZ26", 20, T0 + 1000, "12:00:01.000", 5, 170005, "XP", "Ideal", "Comprador", "+"),
                (DIA, "WINZ26", 30, T0 + 2000, "12:00:02.000", 10, 170010, "XP", "Goldman", "RLP", "+")]
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)], fita), DIA)
        rlp = r["rlp"]
        self.assertEqual(rlp["acumulado"]["Estrangeiro"], [5])  # RLP conta uma vez, na corretora compradora
        self.assertEqual(rlp["acumulado"]["Misto"], [10])
        self.assertAlmostEqual(rlp["pct_rlp"][0], 15 / 20)
        self.assertEqual(rlp["preco"], [170010])
        xp = {c["corretora"]: c for c in r["corretoras"]}["XP"]
        self.assertAlmostEqual(xp["rlp"], 15 / 20)  # XP: 15 dos 20 contratos que viu na fita eram RLP

    def test_sem_fita(self):
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)]), DIA)
        self.assertEqual(r["rlp"]["t"], [])


if __name__ == "__main__":
    unittest.main()
