"""Testes do players.py com dados sinteticos.

  python -m unittest test_players -v
"""
import sqlite3
import unittest

import coletor_tryd as ct
import players

DIA = 20756
T0 = DIA * 86_400_000 + 12 * 3_600_000


def banco(saldo, fita=(), ativo="WINZ26"):
    db = sqlite3.connect(":memory:")
    db.executescript(ct.SCHEMA)
    db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd, sagr, qtdc, qtdv) VALUES (?,?,?,?,?,?,?,?)",
                   [(ts, ativo, i, b, q, s, c, v) for i, (ts, b, q, s, c, v) in enumerate(saldo)])
    db.executemany("INSERT INTO tryd_fita VALUES (?,?,?,?,?,?,?,?,?,?,?)", fita)
    return db


class TestSaldo(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()

    def test_grupos_somam_zero_e_corretora_nova_comeca_em_zero(self):
        saldo = [(T0, "Goldman", 10, 4, 10, 0), (T0, "XP", -10, -4, 0, 10),
                 (T0 + 10_000, "Goldman", 30, 9, 30, 0), (T0 + 10_000, "XP", -25, -9, 5, 30),
                 (T0 + 10_000, "Zzz", -5, 0, 0, 5)]
        r = players.montar(banco(saldo), DIA)
        self.assertEqual(r["t"], [T0 // 1000, T0 // 1000 + 10])
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [10, 30])
        self.assertEqual(r["series"]["XP"]["saldo"], [-10, -25])
        self.assertEqual(r["series"]["Outros"]["saldo"], [0, -5])  # Zzz so aparece na 2a foto
        for i in range(2):
            self.assertEqual(sum(s["saldo"][i] for s in r["series"].values()), 0)
        self.assertEqual(r["corretoras"][0]["corretora"], "XP")  # maior volume

    def test_xp_btg_genial_tem_grupo_proprio(self):
        self.assertEqual([players.grupo(b) for b in ("XP", "BTG", "Genial", "Santander")], ["XP", "BTG", "Genial", "Misto"])

    def test_celula_vazia_ou_incoerente_mantem_o_valor_anterior(self):
        # 3/11/2026: UBS vinha com qtd vazia numa foto e o Estrangeiro "saltava" para zero e voltava
        saldo = [(T0, "UBS", -30, None, 10, 40), (T0, "XP", 30, None, 40, 10),
                 (T0 + 10_000, "UBS", None, None, None, None), (T0 + 10_000, "XP", 31, None, 41, 10),
                 (T0 + 20_000, "UBS", -5, None, 10, 40), (T0 + 20_000, "XP", 32, None, 42, 10),   # -5 != 10 - 40
                 (T0 + 30_000, "XP", 33, None, 43, 10),                                           # UBS fora da foto
                 (T0 + 40_000, "UBS", -34, None, 10, 44), (T0 + 40_000, "XP", 34, None, 44, 10)]
        r = players.montar(banco(saldo), DIA)
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [-30, -30, -30, -30, -34])
        ubs = {c["corretora"]: c for c in r["corretoras"]}["UBS"]
        self.assertEqual((ubs["saldo"], ubs["volume"]), (-34, 54))

    def test_agressao_vazia_nao_vira_serie_de_zeros(self):
        saldo = [(T0, "XP", 5, "", 5, 0), (T0 + 10_000, "XP", 7, None, 7, 0)]
        r = players.montar(banco(saldo), DIA)
        self.assertFalse(r["agr_disponivel"])
        self.assertNotIn("agr", r["series"]["XP"])
        r = players.montar(banco([(T0, "XP", 5, -3, 5, 0)]), DIA)
        self.assertTrue(r["agr_disponivel"])
        self.assertEqual(r["series"]["XP"]["agr"], [-3])

    def test_agressao_estimada_pela_fita_quando_a_janela_nao_manda(self):
        fita = [(DIA, "WINZ26", 10, T0, "12:00:00.000", 5, 170000, "Goldman", "XP", "Comprador", "+"),
                (DIA, "WINZ26", 20, T0 + 1000, "12:00:01.000", 3, 169995, "XP", "Goldman", "Vendedor", "-"),
                (DIA, "WINZ26", 30, T0 + 2000, "12:00:02.000", 9, 170000, "XP", "XP", "RLP", "="),
                (DIA, "WINZ26", 50, T0 + 3000, "12:00:03.000", 2, 170000, "BTG", "Ideal", "Comprador", "+")]
        r = players.montar(banco([(T0, "XP", 1, "", 1, 0), (T0, "Goldman", -1, "", 0, 1)], fita), DIA)
        self.assertFalse(r["agr_disponivel"])
        est = r["agr_fita"]["series"]
        self.assertEqual(est["Estrangeiro"], [2])   # Goldman comprou 5 a mercado e vendeu 3 a mercado
        self.assertEqual(est["BTG"], [2])           # BTG comprou 2 a mercado; RLP nao entra
        self.assertAlmostEqual(r["agr_cobertura_fita"], 4 / 5)  # num 10..50: 5 esperados, 4 capturados
        gold = {c["corretora"]: c for c in r["corretoras"]}["Goldman"]
        self.assertEqual(gold["agr"], 2)

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
        self.assertEqual(r["series"]["XP"]["saldo"][-1], 1999)  # inclui a ultima foto


DIA_MS = players.DIA_MS


class TestAtivos(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco([(T0, "XP", 1, None, 1, 0), (T0, "UBS", -1, None, 0, 1)], ativo="PETR4")
        self.db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd, sagr, qtdc, qtdv) VALUES (?,?,?,?,?,?,?,?)",
                            [(T0, "WDOZ26", 0, "XP", 2, 2, 2, 0), (T0, "WINZ26", 0, "XP", 3, 3, 3, 0),
                             (T0, "WINZ26", 1, "Goldman", -3, -3, 0, 3), (T0 - DIA_MS, "VALE3", 0, "XP", 1, 1, 1, 0)])

    def test_sem_ativo_e_o_win_e_lista_os_ativos_do_dia_na_ordem(self):
        r = players.montar(self.db, None)
        self.assertEqual(r["ativo"], "WINZ26")
        self.assertEqual(r["ativos"], ["WINZ26", "WDOZ26", "PETR4"])  # VALE3 so no dia anterior
        self.assertEqual(r["unidade"], "contratos")

    def test_acao_sem_fita_nem_agressao(self):
        r = players.montar(self.db, None, "PETR4")
        self.assertEqual((r["ativo"], r["dia"], r["unidade"]), ("PETR4", DIA, "ações"))
        self.assertEqual(r["series"]["XP"]["saldo"], [1])
        self.assertIsNone(r["agr_fita"])  # sem fita nao ha estimativa (antes vinha uma serie vazia)
        self.assertEqual({c["agr"] for c in r["corretoras"]}, {None})  # coluna vazia, nao zero
        self.assertEqual(r["negocios_fita"], 0)

    def test_dia_sem_o_ativo_pedido(self):
        r = players.montar(self.db, DIA - 1, "PETR4")
        self.assertTrue(r["vazio"])
        self.assertEqual(r["ativos"], ["VALE3"])

    def test_tabela_de_ontem_antes_da_abertura_e_ignorada(self):
        # 6/11/2026: antes das 10:00 o Tryd ainda mostra o saldo final de ontem das acoes
        ontem = [(T0 - DIA_MS, "XP", 5, None, 5, 0), (T0 - DIA_MS, "UBS", -5, None, 0, 5)]
        db = banco(ontem + [
            (T0, "XP", 5, None, 5, 0), (T0, "UBS", -5, None, 0, 5),
            (T0 + 60_000, "XP", 2, None, 2, 0), (T0 + 60_000, "UBS", -2, None, 0, 2)], ativo="PETR4")
        r = players.montar(db, DIA, "PETR4")
        self.assertEqual(r["fotos"], 1)
        self.assertEqual(r["series"]["XP"]["saldo"], [2])
        db = banco(ontem + [(T0, "XP", 5, None, 5, 0), (T0, "UBS", -5, None, 0, 5)], ativo="PETR4")
        r = players.montar(db, DIA, "PETR4")
        self.assertEqual((r["vazio"], r["motivo"]), (True, "tabela_de_ontem"))

    def test_acoes_antes_das_10h_sao_ignoradas_mesmo_diferentes_de_ontem(self):
        # 9/11/2026: a tabela de sexta das 08:55 tinha 9 de 36 corretoras diferentes da ultima foto gravada de sexta
        h = DIA * DIA_MS
        db = banco([(T0 - DIA_MS, "XP", 5, None, 5, 0),
                    (h + 9 * 3_600_000, "XP", 6, None, 6, 0), (h + 9 * 3_600_000, "UBS", -6, None, 0, 6),
                    (h + 10 * 3_600_000 + 60_000, "XP", 1, None, 1, 0)], ativo="PETR4")
        r = players.montar(db, DIA, "PETR4")
        self.assertEqual(r["series"]["XP"]["saldo"], [1])
        self.assertNotIn("UBS", r["series"])

    def test_zeramento_da_tabela_corta_o_saldo_de_ontem(self):
        # 6/11/2026: WINZ26 das 08:55 as 09:00 com a tabela de ontem (nao identica a ultima foto gravada); o Goldman, que
        # ainda nao tinha operado hoje, ficava com o saldo de ontem o dia todo
        h9 = DIA * DIA_MS + 9 * 3_600_000
        saldo = [(h9 - 60_000, "Goldman", 500, None, 900, 400), (h9 - 60_000, "XP", -500, None, 400, 900),
                 (h9, "XP", 3, None, 3, 0), (h9 + 10_000, "XP", 4, None, 4, 0)]
        r = players.montar(banco(saldo), DIA)
        self.assertEqual(r["series"]["XP"]["saldo"], [3, 4])
        self.assertNotIn("Estrangeiro", r["series"])
        # tabela quase vazia na ultima foto da tarde nao e zeramento
        tarde = DIA * DIA_MS + 18 * 3_600_000
        saldo = [(tarde, "XP", 50, None, 90, 40), (tarde + 10_000, "XP", 1, None, 1, 0)]
        self.assertEqual(players.montar(banco(saldo), DIA)["fotos"], 2)

    def test_unidade(self):
        self.assertEqual([players.unidade(a) for a in ("WINZ26", "WDOF27", "VALE3", "BOVA11")],
                         ["contratos", "contratos", "ações", "cotas"])


class TestRlp(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()

    def test_agregacao_incremental_igual_a_completa(self):
        # 40 minutos de negocios; a 2a chamada ja traz minutos fechados em cache e a 3a recebe negocios novos
        fita = [(DIA, "WINZ26", 10 * i, T0 + i * 30_000, "h", 2, 170000, "XP", "Ideal" if i % 3 else "XP",
                 "Comprador" if i % 2 else "RLP", "=") for i in range(80)]
        db = banco([(T0, "XP", 1, 1, 1, 0)], fita[:60])
        r1 = players.montar(db, DIA)
        r2 = players.montar(db, DIA)
        self.assertEqual(r1["rlp"], r2["rlp"])
        db.executemany("INSERT INTO tryd_fita VALUES (?,?,?,?,?,?,?,?,?,?,?)", fita[60:])
        r3 = players.montar(db, DIA)
        players._CACHE_FITA.clear()
        self.assertEqual(r3["rlp"], players.montar(db, DIA)["rlp"])  # igual a agregar tudo de uma vez
        self.assertEqual(r3["negocios_fita"], 80)
        self.assertNotEqual(r1["rlp"], r3["rlp"])

    def test_rlp_por_grupo_e_percentual(self):
        fita = [(DIA, "WINZ26", 10, T0, "12:00:00.000", 5, 170000, "Goldman", "XP", "RLP", "="),
                (DIA, "WINZ26", 20, T0 + 1000, "12:00:01.000", 5, 170005, "XP", "Ideal", "Comprador", "+"),
                (DIA, "WINZ26", 30, T0 + 2000, "12:00:02.000", 10, 170010, "XP", "Goldman", "RLP", "+")]
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)], fita), DIA)
        rlp = r["rlp"]
        self.assertEqual(rlp["acumulado"]["Estrangeiro"], [5])  # RLP conta uma vez, na corretora compradora
        self.assertEqual(rlp["acumulado"]["XP"], [10])
        self.assertAlmostEqual(rlp["pct_rlp"][0], 15 / 20)
        xp = {c["corretora"]: c for c in r["corretoras"]}["XP"]
        self.assertAlmostEqual(xp["rlp"], 15 / 20)  # XP: 15 dos 20 contratos que viu na fita eram RLP

    def test_fita_de_ontem_com_data_na_hora_e_ignorada(self):
        # 6/11/2026: os 100 ultimos negocios de ontem vieram as 08:55 com a data na hora e `num` maior que os de hoje
        ontem = players._data_br(DIA - 1) + " 18:31:22.221"
        fita = [(DIA, "WINZ26", 9_000_000, T0 - 3 * 3_600_000, ontem, 7, 170000, "XP", "XP", "RLP", "="),
                (DIA, "WINZ26", 10, T0, players._data_br(DIA) + " 12:00:00.000", 5, 170000, "Goldman", "XP", "RLP", "="),
                (DIA, "WINZ26", 20, T0 + 600_000, "12:10:00.000", 3, 170000, "XP", "Ideal", "Comprador", "+")]
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)], fita), DIA)
        self.assertEqual(r["negocios_fita"], 2)
        self.assertGreater(r["rlp"]["t"][-1], (T0 + 300_000) // 1000)  # chega ao negocio das 12:10 (antes parava as 09:02)

    def test_fita_de_ontem_sem_data_na_hora_e_ignorada(self):
        # 9/11/2026: as 08:55 a janela so tinha negocios de sexta e o Tryd nao pos a data na hora
        antes = DIA * 86_400_000 + 8 * 3_600_000 + 55 * 60_000
        fita = [(DIA, "WINZ26", 9_000_000, antes, "18:24:59.958", 7, 170000, "XP", "XP", "RLP", "="),
                (DIA, "WINZ26", 10, T0, "12:00:00.000", 5, 170000, "Goldman", "XP", "RLP", "="),
                (DIA, "WINZ26", 20, T0 + 600_000, "12:10:00.000", 3, 170000, "XP", "Ideal", "Comprador", "+")]
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)], fita), DIA)
        self.assertEqual(r["negocios_fita"], 2)
        self.assertGreater(r["rlp"]["t"][-1], (T0 + 300_000) // 1000)

    def test_sem_fita(self):
        r = players.montar(banco([(T0, "XP", 1, 1, 1, 0)]), DIA)
        self.assertEqual(r["rlp"]["t"], [])


if __name__ == "__main__":
    unittest.main()
