"""Testes da partida do coletor de regime: Focus e Tesouro Direto (sem rede, funcoes mockadas)."""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import coletor_focus
import coletor_regime as cr
import coletor_tesouro


class TestFontesNaPartida(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "teste.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_partida_chama_focus_e_tesouro(self):
        with mock.patch.object(coletor_focus, "coletar", return_value=0) as foco, \
                mock.patch.object(coletor_tesouro, "coletar_intervalo", return_value=0) as tesouro:
            cr.atualizar_fontes_externas(self.db)
        foco.assert_called_once()
        tesouro.assert_called_once()
        # Focus na data do relatorio mais recente (ultima sexta-feira)
        self.assertEqual(foco.call_args.args[1], coletor_focus.ultima_sexta().isoformat())
        # Tesouro completando os ultimos 10 dias
        hj = date.today()
        self.assertEqual(tesouro.call_args.args[1:], ((hj - timedelta(days=10)).isoformat(), hj.isoformat()))
        # o banco temporario recebeu os dois schemas (idempotente, mesmo sem dados)
        db = sqlite3.connect(self.db)
        try:
            tabelas = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        self.assertIn("focus_ipca", tabelas)
        self.assertIn("tesouro_taxas", tabelas)

    def test_erro_no_focus_nao_impede_o_tesouro(self):
        with mock.patch.object(coletor_focus, "coletar", side_effect=RuntimeError("BCB fora")), \
                mock.patch.object(coletor_tesouro, "coletar_intervalo", return_value=0) as tesouro:
            cr.atualizar_fontes_externas(self.db)  # nao levanta
        tesouro.assert_called_once()

    def test_erro_no_tesouro_nao_impede_o_focus_e_nao_derruba_a_partida(self):
        with mock.patch.object(coletor_focus, "coletar", return_value=0) as foco, \
                mock.patch.object(coletor_tesouro, "coletar_intervalo", side_effect=RuntimeError("CSV fora")):
            cr.atualizar_fontes_externas(self.db)  # nao levanta
        foco.assert_called_once()


if __name__ == "__main__":
    unittest.main()
