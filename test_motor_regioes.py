"""Testes do motor_regioes.py com banco SQLite temporario (dados sinteticos).

  python -m unittest test_motor_regioes -v

Cobre: fontes de nivel (D-1, composto, extremos, VWAP, quantis de fallback e premed do Tryd), o
agrupamento single linkage e a forca por fontes distintas, as regras de classificacao pelo vies, o plano
(stop/alvos/R:R liquido), o limite de 3 zonas por lado, a causalidade (nada depois de agora entra) e a
gravacao em regioes_operacionais.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import calendario_b3 as cal
import motor_regioes as mr

D = 86_400_000
DIA = 20774              # terca 17/11/2026
AGORA = DIA * D + 15 * 3600000       # 15:00


def ts(dia, hh, mm=0, ss=0):
    return dia * D + (hh * 3600 + mm * 60 + ss) * 1000


def iso(dia):
    return (date(1970, 1, 1) + timedelta(days=dia)).isoformat()


SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, mid REAL, spread REAL,
                        last REAL, vol_compra_agr REAL, vol_venda_agr REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, last REAL);
CREATE TABLE barras_1m (symbol TEXT, ts_ms INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL);
CREATE TABLE fechamento_diario (dia_idx INTEGER, ativo TEXT, maxima REAL, minima REAL, ultimo_liquido REAL,
                                poc_dia REAL, vah_dia REAL, val_dia REAL, PRIMARY KEY (dia_idx, ativo));
CREATE TABLE regime_live (ts_ms INTEGER, sessao TEXT, preco REAL, rv_dia_prev REAL, faixa_1h_pts REAL,
                          wdo_rv_rel REAL);
CREATE TABLE tryd_saldo (ts_ms INTEGER, ativo TEXT, linha INTEGER, brk TEXT, qtd REAL, premed REAL);
CREATE TABLE cenario_scores (ts_ms INTEGER PRIMARY KEY, score_estrutural REAL);
"""


def pregoes_antes(dia, n):
    d = date(1970, 1, 1) + timedelta(days=dia)
    out = []
    for _ in range(n):
        d = cal.pregao_anterior(d)
        out.append((d - date(1970, 1, 1)).days)
    return out


def banco(com_hoje=True, caminho=":memory:"):
    db = sqlite3.connect(str(caminho))
    db.executescript(SCHEMA)
    antes = pregoes_antes(DIA, 16)
    for i, d in enumerate(antes):
        db.execute("INSERT INTO fechamento_diario (dia_idx, ativo, maxima, minima, ultimo_liquido) VALUES (?,?,?,?,?)",
                   (d, "WIN$", 100400.0, 99900.0, 100150.0))
        db.executemany("INSERT INTO barras_1m (symbol, ts_ms, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                       [("WIN$", ts(d, 10, 0), 100000.0, 100200.0, 99950.0, 100100.0, 100.0),
                        ("WIN$", ts(d, 11, 0), 100100.0, 100800.0, 99500.0, 100000.0, 50.0),
                        ("WIN$", ts(d, 12, 0), 100000.0, 100150.0, 99800.0, 100050.0, 10.0)])
    # D-1 fica com POC/VAH/VAL NULL: o perfil do teste sai das barras acima
    if com_hoje:
        linhas = []
        for i in range(301):                      # 10:00 a 15:00, 1 leitura por minuto
            preco = 100000.0 if i == 300 else (99900.0 if i % 2 == 0 else 100100.0)
            t = ts(DIA, 10, 0) + i * 60_000
            linhas.append((t, "S1", "WIN$", preco - 2.5, preco + 2.5, preco, 5.0, preco, 5.0, 5.0))
        db.executemany("INSERT INTO snapshots (ts_ms, sessao, symbol, bid, ask, mid, spread, last, vol_compra_agr, "
                       "vol_venda_agr) VALUES (?,?,?,?,?,?,?,?,?,?)", linhas)
        db.execute("INSERT INTO regime_live VALUES (?,?,?,?,?,?)",
                   (AGORA - 60_000, "S1", 100000.0, 1.0, 200.0, 1.0))
        # Tryd: fotos do dia e de D-1 (premed em R$ por contrato; WIN = R$ 0,20 por ponto)
        for d, prem_e, prem_i in ((DIA, 20000.0, 20020.0), (antes[0], 20010.0, 20000.0)):
            for linha, (brk, qtd, prem) in enumerate((("Goldman", 10.0, prem_e), ("Itau", 5.0, prem_i))):
                db.execute("INSERT INTO tryd_saldo VALUES (?,?,?,?,?,?)",
                           (ts(d, 14, 0), "WINZ26", linha, brk, qtd, prem))
    return db


class TestFontes(unittest.TestCase):
    def test_niveis_das_fontes_e_diagnostico(self):
        db = banco()
        niveis = mr.niveis_candidatos(db, "WIN$", AGORA)
        fontes = {n["fonte"] for n in niveis}
        self.assertTrue({"d1", "composto", "extremos", "vwap", "quantis", "quantis_extremos", "premed"} <= fontes)
        rotulos = {n["rotulo"] for n in niveis}
        self.assertIn("POC D-1", rotulos)
        self.assertIn("VAH D-1", rotulos)
        self.assertIn("VWAP dia", rotulos)
        self.assertIn("q05 do dia", rotulos)
        self.assertIn("Preço médio Estrangeiro dia", rotulos)
        self.assertIn("Preço médio Institucional D-1", rotulos)
        diag = mr.diagnostico(db, "WIN$", AGORA)
        self.assertEqual(diag["preco"], 100000.0)
        self.assertIsInstance(diag["origem_quantis"], str)
        self.assertTrue(any(f.startswith("walls/gamma_flip") for f in diag["faltando"]))   # sem tabela de opcoes

    def test_quantis_de_fallback_sem_modelo(self):
        db = banco()
        with mock.patch.object(mr, "_modelo_v3", return_value=None):
            niveis, origem = mr._niveis_quantis(db, "WIN$", AGORA, 100000.0, {})
        self.assertEqual(origem, "gaussiana_regime_live (rv_dia_prev)")
        por_tag = {n["tag"]: n["preco"] for n in niveis}
        self.assertAlmostEqual(por_tag["q05"], 100000.0 - 1.6449 * 1000.0)
        self.assertAlmostEqual(por_tag["q15"], 100000.0 - 1.0364 * 1000.0)
        self.assertAlmostEqual(por_tag["q95"], 100000.0 + 1.6449 * 1000.0)

    def test_premed_ponderado_por_qtd_e_convertido_do_r_por_contrato(self):
        db = banco()
        niveis = [n for n in mr.niveis_candidatos(db, "WIN$", AGORA) if n["fonte"] == "premed"]
        por_rotulo = {n["rotulo"]: n["preco"] for n in niveis}
        self.assertAlmostEqual(por_rotulo["Preço médio Estrangeiro dia"], 20000.0 / 0.20)
        self.assertAlmostEqual(por_rotulo["Preço médio Estrangeiro D-1"], 20010.0 / 0.20)

    def test_perfil_de_d_1_sai_das_barras_quando_o_fechamento_nao_tem_poc(self):
        db = banco()
        perfil = mr._perfil_dia(db, "WIN$", pregoes_antes(DIA, 1)[0])
        self.assertIsNotNone(perfil)
        self.assertAlmostEqual(perfil["poc"], 100100.0)

    def test_causalidade_vwap_do_dia_para_em_agora(self):
        db = banco()
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ts(DIA, 16, 0), "S1", "WIN$", 109998.0, 110002.0, 110000.0, 4.0, 110000.0, 1e9, 1e9))
        self.assertEqual(mr.preco_atual(db, "WIN$", AGORA), 100000.0)
        df = mr._dados_dia(db, "WIN$", DIA, AGORA)
        self.assertLess(df.last.max(), 101000.0)
        _, vwap = mr._niveis_vwap(db, "WIN$", AGORA, {})
        self.assertLess(vwap, 101000.0)


class TestClusterizar(unittest.TestCase):
    def test_single_linkage_e_forca_por_fonte_distinta(self):
        niveis = [dict(preco=100.0, fonte="d1", peso=20.0, rotulo="POC D-1", tag=None),
                  dict(preco=105.0, fonte="d1", peso=20.0, rotulo="VAH D-1", tag="vah"),   # mesma fonte
                  dict(preco=115.0, fonte="vwap", peso=20.0, rotulo="VWAP dia", tag=None),
                  dict(preco=300.0, fonte="composto", peso=25.0, rotulo="POC 20d", tag=None)]
        zonas = mr.clusterizar(niveis, delta=20.0)
        self.assertEqual(len(zonas), 2)
        z = zonas[0]
        self.assertEqual(z["forca"], 40.0)                     # d1 (20) + vwap (20), nao d1 duas vezes
        self.assertEqual(z["preco_min"], 100.0)                # a faixa e o [min, max] dos niveis
        self.assertAlmostEqual(z["preco_centro"], (100 + 105 + 115) / 3)
        self.assertEqual(z["preco_max"], 115.0)
        self.assertEqual(zonas[1]["forca"], 25.0)

    def test_faixa_minima_de_meio_delta_em_volta_do_centro(self):
        niveis = [dict(preco=100.0, fonte="d1", peso=20.0, rotulo="POC D-1", tag=None),
                  dict(preco=101.0, fonte="vwap", peso=20.0, rotulo="VWAP dia", tag=None)]
        z = mr.clusterizar(niveis, delta=20.0)[0]
        self.assertAlmostEqual(z["preco_centro"], 100.5)
        self.assertEqual((z["preco_min"], z["preco_max"]), (95.5, 105.5))    # centro +- 0,25 x 20

    def test_forca_limitada_a_100(self):
        niveis = [dict(preco=100.0, fonte=f, peso=p, rotulo=f, tag=None)
                  for f, p in (("d1", 20.0), ("composto", 25.0), ("vwap", 20.0), ("walls", 25.0),
                               ("gamma_flip", 30.0))]
        self.assertEqual(mr.clusterizar(niveis, delta=10.0)[0]["forca"], 100.0)


class TestClassificacao(unittest.TestCase):
    def zona(self, tags, forca=50.0, centro=100.0):
        return dict(preco_centro=centro, tags=set(tags), forca=forca)

    def test_vies_de_baixa(self):
        self.assertEqual(mr._classificar(self.zona(set(), centro=110), 100, -60), ("A_FAVOR_TENDENCIA", "VENDA"))
        self.assertEqual(mr._classificar(self.zona({"q05", "put_wall"}, forca=60, centro=90), 100, -60),
                         ("CONTRA_TENDENCIA", "COMPRA"))
        self.assertEqual(mr._classificar(self.zona({"q05", "hvn20"}, forca=60, centro=90), 100, -60),
                         ("CONTRA_TENDENCIA", "COMPRA"))
        self.assertEqual(mr._classificar(self.zona({"q05"}, forca=60, centro=90), 100, -60), ("ALVO", "VENDA"))
        self.assertEqual(mr._classificar(self.zona({"q05", "put_wall"}, forca=59, centro=90), 100, -60),
                         ("ALVO", "VENDA"))
        self.assertEqual(mr._classificar(self.zona(set(), centro=90), 100, -60), ("ALVO", "VENDA"))

    def test_vies_de_alta_e_espelho(self):
        self.assertEqual(mr._classificar(self.zona(set(), centro=90), 100, 60), ("A_FAVOR_TENDENCIA", "COMPRA"))
        self.assertEqual(mr._classificar(self.zona({"q95", "call_wall"}, forca=60, centro=110), 100, 60),
                         ("CONTRA_TENDENCIA", "VENDA"))
        self.assertEqual(mr._classificar(self.zona({"q95"}, forca=60, centro=110), 100, 60), ("ALVO", "COMPRA"))

    def test_vies_neutro_marca_so_as_pontas(self):
        self.assertEqual(mr._classificar(self.zona({"q85"}, centro=110), 100, 0), ("CONTRA_TENDENCIA", "VENDA"))
        self.assertEqual(mr._classificar(self.zona({"vah"}, centro=90), 100, 0), ("CONTRA_TENDENCIA", "COMPRA"))
        self.assertEqual(mr._classificar(self.zona({"hvn20"}, centro=110), 100, 0), ("NEUTRA", "VENDA"))
        self.assertEqual(mr._classificar(self.zona(set(), centro=90), 100, 0), ("NEUTRA", "COMPRA"))

    def test_limites_do_vies(self):
        self.assertEqual(mr._classificar(self.zona(set(), centro=110), 100, -25)[0], "A_FAVOR_TENDENCIA")
        self.assertEqual(mr._classificar(self.zona(set(), centro=110), 100, -24)[0], "NEUTRA")


class TestPlano(unittest.TestCase):
    def test_rr_liquido_do_custo(self):
        zona = dict(preco_min=98.0, preco_centro=100.0, preco_max=102.0, forca=80.0, tags=set())
        outra = dict(preco_min=79.0, preco_centro=80.0, preco_max=81.0, forca=80.0, tags=set())
        plano = mr._plano(zona, "VENDA", 10.0, [zona, outra], {"poc_dia": None, "vwap_dia": None}, custo_lado=5.0)
        self.assertAlmostEqual(plano["stop"], 104.5)                  # 102 + 0,25 x 10
        self.assertEqual(plano["alvo_1"], 80.0)
        self.assertIsNone(plano["alvo_2"])
        esperado = ((100.0 - 80.0) - 10.0) / ((104.5 - 100.0) + 10.0)
        self.assertAlmostEqual(plano["rr"], esperado)

    def test_compra_usa_o_poc_mais_perto_como_primeiro_alvo(self):
        zona = dict(preco_min=98.0, preco_centro=100.0, preco_max=102.0, forca=80.0, tags=set())
        plano = mr._plano(zona, "COMPRA", 8.0, [zona], {"poc_dia": 130.0, "vwap_dia": 120.0}, custo_lado=0.5)
        self.assertAlmostEqual(plano["stop"], 96.0)                   # 98 - 0,25 x 8
        self.assertEqual(plano["alvo_1"], 120.0)
        self.assertEqual(plano["alvo_2"], 130.0)


class TestRegioes(unittest.TestCase):
    def test_zonas_limitadas_e_classificadas_pelo_vies(self):
        db = banco()
        zonas = mr.regioes(db, "WIN$", AGORA, vies=-60)
        self.assertTrue(0 < len(zonas) <= 6)
        self.assertLessEqual(sum(z["lado"] == "ACIMA" for z in zonas), 3)
        self.assertLessEqual(sum(z["lado"] == "ABAIXO" for z in zonas), 3)
        self.assertTrue(all(z["forca"] >= mr.FORCA_MIN for z in zonas))
        for z in zonas:
            self.assertLessEqual(z["preco_min"], z["preco_centro"])
            self.assertLessEqual(z["preco_centro"], z["preco_max"])
            self.assertIn("contem_preco", z)
            self.assertIn(z["alinhamento"], ("A_FAVOR_TENDENCIA", "CONTRA_TENDENCIA", "ALVO", "NEUTRA"))
            if z["lado"] == "ACIMA":
                self.assertGreater(z["distancia_pts"], 0)
            else:
                self.assertLessEqual(z["distancia_pts"], 0)
            if z["alinhamento"] == "A_FAVOR_TENDENCIA":
                self.assertEqual(z["direcao"], "VENDA")
                self.assertIsNotNone(z["stop"])
                self.assertGreater(z["stop"], z["preco_centro"])

    def test_sem_preco_nao_devolve_zona(self):
        self.assertEqual(mr.regioes(banco(com_hoje=False), "WIN$", AGORA, vies=-60), [])

    def test_atualizar_grava_e_le_o_vies_do_cenario(self):
        db_file = Path(tempfile.mkdtemp()) / "book.db"
        db = banco(caminho=db_file)
        db.execute("INSERT INTO cenario_scores VALUES (?,?)", (AGORA - 1000, -60.0))
        db.commit()
        db.close()
        n = mr.atualizar(str(db_file), AGORA)
        self.assertGreater(n, 0)
        con = sqlite3.connect(db_file)
        linhas = con.execute("SELECT ativo, id_zona, preco_min, preco_centro, preco_max, forca_score, alinhamento, "
                             "direcao_operacao, componentes_json, stop_sugerido, status_gatilho "
                             "FROM regioes_operacionais WHERE ativo='WIN$' ORDER BY id_zona").fetchall()
        con.close()
        self.assertTrue(linhas)
        acima = [l for l in linhas if l[3] > 100000.0]
        self.assertTrue(acima)
        for l in acima:
            self.assertEqual(l[6], "A_FAVOR_TENDENCIA")
            self.assertEqual(l[7], "VENDA")
            self.assertGreater(l[9], l[4])                 # stop acima da borda externa
            self.assertIn(l[10], ("DISTANTE", "PROXIMA", "EM_TESTE"))
            self.assertIsInstance(json.loads(l[8]), list)


if __name__ == "__main__":
    unittest.main()
