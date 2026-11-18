"""Testes do fallback_tryd_fita.py com fita sintetica.

  python -m unittest test_fallback_tryd_fita -v
"""
import sqlite3
import unittest

import calendario_b3 as cal
import coletor_tryd as ct
import fallback_tryd_fita as fb
import players

D = 86_400_000
DIA = 20768              # quarta 11/11/2026, pregao util
SABADO = 20771           # sabado 14/11/2026
ATIVO = "WINZ26"


def ts(dia, hh, mm=0, ss=0):
    """ts_ms de um horario de Brasilia codificado como epoch UTC."""
    return dia * D + (hh * 3600 + mm * 60 + ss) * 1000


def banco(fita=(), saldo=()):
    db = sqlite3.connect(":memory:")
    db.executescript(ct.SCHEMA)
    db.executemany("INSERT INTO tryd_fita (dia, ativo, num, ts_ms, hora, qtd, preco, comprador, vendedor, "
                   "agressor, var) VALUES (?,?,?,?,?,?,?,?,?,?,?)", fita)
    db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd, sagr, qtdc, qtdv) "
                   "VALUES (?,?,?,?,?,?,?,?)", saldo)
    return db


def neg(dia, hh, mm, num, qtd, comprador, vendedor, agressor="Comprador", segundo=0):
    return (dia, ATIVO, num, ts(dia, hh, mm, segundo), f"{hh:02d}:{mm:02d}:{segundo:02d}.000", qtd, 170000,
            comprador, vendedor, agressor, "+")


class TestCaptura(unittest.TestCase):
    def test_num_anda_de_10_em_10(self):
        self.assertEqual(fb.captura(2, 10, 20), 1.0)       # 2 negocios lidos, 2 esperados
        self.assertEqual(fb.captura(1, 100, 130), 0.25)    # 1 lido, 4 esperados
        self.assertEqual(fb.captura(0, 10, 20), 0.0)

    def test_sem_num(self):
        self.assertEqual(fb.captura(5, None, 20), 0.0)
        self.assertEqual(fb.captura(5, 20, None), 0.0)


class TestSaldoDaFita(unittest.TestCase):
    def test_comprador_e_vendedor_por_grupo(self):
        db = banco([neg(DIA, 12, 0, 10, 5, "Goldman", "XP"),
                    neg(DIA, 12, 0, 20, 3, "XP", "Goldman", "Vendedor", segundo=1)])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(r["fonte_saldo"], "FITA_FALLBACK")
        self.assertEqual(r["t"], [DIA * 86400 + 12 * 3600])
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [2.0])   # +5 (compra) -3 (venda)
        self.assertEqual(r["series"]["XP"]["saldo"], [-2.0])
        self.assertEqual(sum(s["saldo"][0] for s in r["series"].values()), 0)
        self.assertEqual(r["series"]["Estrangeiro"]["agr"], [2.0])     # +5 comprador -3 vendedor
        self.assertEqual(r["series"]["XP"]["agr"], [0.0])
        self.assertAlmostEqual(r["captura_media"], 1.0)
        self.assertEqual(r["series_sem_ajuste"], r["series"])

    def test_rlp_soma_zero_no_saldo_e_nao_entra_na_agressao(self):
        db = banco([neg(DIA, 12, 0, 10, 9, "XP", "XP", "RLP")])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(r["series"], {})                             # XP +9 -9 = 0 e sem agressao

    def test_minuto_com_captura_baixa_e_ajustado(self):
        # primeiro minuto inteiro (2 de 2); no segundo, 2 lidos para 4 esperados (50% do fluxo)
        db = banco([neg(DIA, 12, 0, 10, 1, "Goldman", "XP"),
                    neg(DIA, 12, 0, 20, 1, "Goldman", "XP", segundo=1),
                    neg(DIA, 12, 1, 100, 1, "Goldman", "XP"),
                    neg(DIA, 12, 1, 130, 1, "Goldman", "XP", segundo=1)])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(r["t"], [DIA * 86400 + 12 * 3600, DIA * 86400 + 12 * 3600 + 60])
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [2.0, 6.0])        # 1+1, depois +2/0,5
        self.assertEqual(r["series_sem_ajuste"]["Estrangeiro"]["saldo"], [2.0, 4.0])
        self.assertAlmostEqual(r["captura_media"], (1.0 + 0.5) / 2)

    def test_captura_abaixo_do_piso_usa_o_piso(self):
        # num 10 -> 1000: 100 esperados para 2 lidos (2%); o divisor para no piso de 20%
        db = banco([neg(DIA, 12, 0, 10, 1, "Goldman", "XP"),
                    neg(DIA, 12, 1, 20, 1, "Goldman", "XP"),
                    neg(DIA, 12, 1, 1000, 1, "Goldman", "XP", segundo=1)])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [1.0, 11.0])      # 1 + 2/0,2
        self.assertEqual(r["series_sem_ajuste"]["Estrangeiro"]["saldo"], [1.0, 3.0])

    def test_hora_invalida_usa_o_ts_da_captura(self):
        db = banco([(DIA, ATIVO, 10, ts(DIA, 12, 0), None, 1, 170000, "Goldman", "XP", "Comprador", "+"),
                    (DIA, ATIVO, 20, ts(DIA, 12, 1), "lixo", 1, 170000, "Goldman", "XP", "Comprador", "+")])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(r["t"], [DIA * 86400 + 12 * 3600, DIA * 86400 + 12 * 3600 + 60])
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [1.0, 2.0])

    def test_grade_cobre_do_primeiro_ao_ultimo_minuto(self):
        db = banco([neg(DIA, 10, 0, 10, 1, "Goldman", "XP"),
                    neg(DIA, 10, 3, 20, 1, "Goldman", "XP")])
        r = fb.saldo_da_fita(db, DIA, ATIVO)
        self.assertEqual(len(r["t"]), 4)
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [1.0, 1.0, 1.0, 2.0])
        self.assertAlmostEqual(r["captura_media"], 1.0)   # os minutos sem negocio nao entram na media

    def test_sem_fita_ou_sem_tabela(self):
        self.assertIsNone(fb.saldo_da_fita(banco(), DIA, ATIVO))
        self.assertIsNone(fb.saldo_da_fita(sqlite3.connect(":memory:"), DIA, ATIVO))
        self.assertIsNone(fb.saldo_da_fita(banco([neg(DIA - 1, 12, 0, 10, 1, "Goldman", "XP")]), DIA, ATIVO))

    def test_sem_ajuste_nao_devolve_a_serie_crua(self):
        db = banco([neg(DIA, 12, 0, 10, 1, "Goldman", "XP")])
        r = fb.saldo_da_fita(db, DIA, ATIVO, ajustar=False)
        self.assertNotIn("series_sem_ajuste", r)
        self.assertEqual(r["series"]["Estrangeiro"]["saldo"], [1.0])


class TestSaldoAtrasado(unittest.TestCase):
    def test_dentro_do_pregao_cobra_o_saldo(self):
        self.assertTrue(cal.eh_pregao(fb._data(DIA)))
        db = banco(saldo=[(ts(DIA, 11, 59), ATIVO, 0, "XP", 1, 1, 1, 0)])
        self.assertFalse(fb.saldo_atrasado(db, ATIVO, ts(DIA, 12, 0)))       # 60 s: no limite
        self.assertTrue(fb.saldo_atrasado(db, ATIVO, ts(DIA, 12, 0, 2)))     # 62 s: atrasado
        self.assertTrue(fb.saldo_atrasado(banco(), ATIVO, ts(DIA, 12, 0)))   # sem foto hoje

    def test_fora_do_pregao_nao_cobra(self):
        velho = banco(saldo=[(ts(DIA, 11, 0), ATIVO, 0, "XP", 1, 1, 1, 0)])
        self.assertFalse(fb.saldo_atrasado(velho, ATIVO, ts(DIA, 19, 0)))    # depois das 18:25
        self.assertFalse(fb.saldo_atrasado(velho, ATIVO, ts(DIA, 8, 0)))     # antes das 9:05
        self.assertFalse(fb.saldo_atrasado(velho, ATIVO, ts(SABADO, 12, 0)))  # sabado

    def test_tabela_ausente_conta_como_atrasada_no_pregao(self):
        self.assertTrue(fb.saldo_atrasado(sqlite3.connect(":memory:"), ATIVO, ts(DIA, 12, 0)))


if __name__ == "__main__":
    unittest.main()
