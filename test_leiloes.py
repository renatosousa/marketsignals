"""Testes de coletor_leiloes.py e leiloes.py sem rede: planilhas montadas em memoria com o layout real do Tesouro.

  python -m unittest test_leiloes -v
"""
import sqlite3
import unittest
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from unittest import mock

import numpy as np
import openpyxl
import pandas as pd

import coletor_anbima
import coletor_leiloes as cl
import leiloes

CABECALHO_PT = ["Data do leilão", "Título", "Tipo de leilão", "Volta", "Data de liquidação", "Data de vencimento", "Oferta",
                "Taxa média", "Taxa de corte", "Venda", "Financeiro (R$)", "Venda para Bacen", "Financeiro para Bacen (R$)"]
CABECALHO_EN = ["Auction Date", "Bond Type", "Auction Type*", "Round", "Settlement Date", "Maturity Date", "Quantity Tendered",
                "Average Rate", "Accepted Rate", "Quantity Accepted", "Total Amount Accepted (R$)", "Quantity to Central Bank",
                "Total Amount to Central Bank (R$)"]
D = datetime


def linha(data, titulo, tipo, volta, liq, venc, oferta, media, corte, venda, fin, bacen=0.0, fin_bacen=0.0):
    return [data, titulo, tipo, f"{volta}.ª volta", liq, venc, oferta, media, corte, venda, fin, bacen, fin_bacen]


# recorte real do arquivo do Tesouro (aba "Ano 2026", leilao de 06/01/2026)
LFT_2032 = linha(D(2026, 1, 6), "LFT", "Venda", 1, D(2026, 1, 7), D(2032, 3, 1), 750000.0, 0.001065, 0.001065, 750000.0, 13506269547.68, 500000.0, 9004179698.5)
NTNB_2029 = linha(D(2026, 1, 6), "NTN-B", "Venda", 1, D(2026, 1, 7), D(2029, 5, 15), 300000.0, 0.07828, 0.07828, 300000.0, 1318785198.88, 2000000.0, 8791901326.0)
NTNB_2029_V2 = linha(D(2026, 1, 6), "NTN-B", "Venda", 2, D(2026, 1, 7), D(2029, 5, 15), 75000.0, 0.07828, 0.07828, 0.0, 0.0)


def xlsx(linhas, topo=4, col0=1, aba="Ano 2026"):
    """.xlsx com `topo` linhas de titulo/legenda, `col0` colunas vazias a esquerda, cabecalho PT, cabecalho EN e os dados."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = aba
    for i in range(topo):
        ws.append([None] * col0 + [f"Tesouro Nacional (linha {i})"])
    ws.append([None] * col0 + CABECALHO_PT)
    ws.append([None] * col0 + CABECALHO_EN)
    for l in linhas:
        ws.append([None] * col0 + l)
    ws.append([None] * col0 + [None] * 13)  # linha vazia no rodape
    b = BytesIO()
    wb.save(b)
    return b.getvalue()


class TestParser(unittest.TestCase):
    def test_cabecalho_dinamico_e_13_colunas(self):
        for topo, col0 in ((4, 1), (9, 1), (2, 0), (6, 3)):
            regs, desc = cl.parsear(xlsx([LFT_2032, NTNB_2029], topo=topo, col0=col0), "x.xlsx")
            self.assertEqual(len(regs), 2, (topo, col0))
            self.assertEqual(desc, 0)
        r = regs[0]
        self.assertEqual((r["data"], r["titulo"], r["tipo"], r["volta"], r["liquidacao"], r["vencimento"]),
                         ("2026-01-06", "LFT", "VENDA", 1, "2026-01-07", "2032-03-01"))

    def test_xlsx_sem_tag_dimension(self):
        """O xlsx do Tesouro nao traz <dimension>: openpyxl em read_only enxergaria uma unica linha vazia."""
        import re
        import zipfile
        b = BytesIO()
        with zipfile.ZipFile(BytesIO(xlsx([LFT_2032, NTNB_2029])), "r") as zin, zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                dados = zin.read(item.filename)
                if item.filename.startswith("xl/worksheets/sheet"):
                    dados = re.sub(rb"<dimension[^>]*/>", b"", dados)
                zout.writestr(item, dados)
        self.assertEqual(len(cl.parsear(b.getvalue(), "x.xlsx")[0]), 2)

    def test_aceitacao_lft_2032_de_06_01_2026(self):
        r = cl.parsear(xlsx([LFT_2032]), "x.xlsx")[0][0]
        self.assertEqual(r["oferta"], 750000)
        self.assertEqual(r["venda"], 750000)
        self.assertEqual(r["financeiro"], 13506269547.68)
        self.assertEqual(r["venda_bacen"], 500000)

    def test_normalizacao(self):
        l = linha(D(2004, 1, 29), "NTN-C", "Extra Compra", 2, D(2004, 2, 2), D(2021, 4, 1), "null", 0.0829, 0.0829, 38100.0, 50458160.0, "", "")
        r = cl.parsear(xlsx([l]), "x.xlsx")[0][0]
        self.assertEqual((r["tipo"], r["volta"]), ("EXTRA_COMPRA", 2))
        self.assertIsNone(r["oferta"])           # 'null' do Tesouro nao e zero
        self.assertIsNone(r["venda_bacen"])      # celula vazia nao e zero
        self.assertEqual(r["venda"], 38100)

    def test_linha_sem_vencimento_e_descartada_e_contada(self):
        l = list(LFT_2032)
        l[5] = None
        regs, desc = cl.parsear(xlsx([l, NTNB_2029]), "x.xlsx")
        self.assertEqual((len(regs), desc), (1, 1))

    def test_layout_que_mudou_falha_alto(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([None, "Data do leilão", "Título", "Volta"])
        ws.append([None, D(2026, 1, 6), "LFT", "1.ª volta"])
        b = BytesIO()
        wb.save(b)
        with self.assertRaisesRegex(ValueError, "layout"):
            cl.parsear(b.getvalue(), "x.xlsx")

    def test_arquivo_sem_dados_falha(self):
        with self.assertRaises(ValueError):
            cl.parsear(xlsx([]), "x.xlsx")

    def test_xls_real_se_existir(self):
        f = Path(__file__).parent / "dados" / "raw" / "leiloes" / "historico-leiloes-todos.xls"
        if not f.exists():
            self.skipTest("historico-leiloes-todos.xls nao baixado")
        regs, desc = cl.parsear(f.read_bytes(), f.name)
        self.assertGreaterEqual(len(regs), 19951)
        self.assertEqual(desc, 0)
        lft = [r for r in regs if r["data"] == "2026-01-06" and r["titulo"] == "LFT" and r["volta"] == 1 and r["vencimento"] == "2032-03-01"]
        self.assertEqual((lft[0]["oferta"], lft[0]["venda"], lft[0]["financeiro"], lft[0]["venda_bacen"]),
                         (750000, 750000, 13506269547.68, 500000))


class TestBanco(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(leiloes.SCHEMA)

    def carregar(self, linhas):
        return cl.gravar(self.db, cl.parsear(xlsx(linhas), "x.xlsx")[0], "x.xlsx")

    def test_upsert_idempotente(self):
        a = self.carregar([LFT_2032, NTNB_2029])
        b = self.carregar([LFT_2032, NTNB_2029])
        self.assertEqual((len(a["novos"]), len(b["novos"]), b["iguais"], len(b["revisados"])), (2, 0, 2, 0))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM leilao_resultados").fetchone(), (2,))

    def test_revisao_por_hash_atualiza_e_registra(self):
        self.carregar([NTNB_2029])
        visto = self.db.execute("SELECT visto_em FROM leilao_resultados").fetchone()
        rev = list(NTNB_2029)
        rev[9] = 290000.0  # o Tesouro corrigiu a quantidade aceita
        r = self.carregar([rev])
        self.assertEqual((len(r["novos"]), len(r["revisados"])), (0, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM leilao_resultados").fetchone(), (1,))
        self.assertEqual(self.db.execute("SELECT venda,visto_em FROM leilao_resultados").fetchone(), (290000.0, visto[0]))
        antes, depois = self.db.execute("SELECT antes,depois FROM leilao_revisoes").fetchone()
        self.assertIn("300000.0", antes)
        self.assertIn("290000.0", depois)

    def test_copia_exata_some_e_chave_igual_com_valores_diferentes_ganha_seq(self):
        outra = list(NTNB_2029)
        outra[10] = 1318785198.99
        r = self.carregar([NTNB_2029, NTNB_2029, outra])
        self.assertEqual((len(r["novos"]), r["copias"]), (2, 1))
        self.assertEqual(self.db.execute("SELECT seq FROM leilao_resultados ORDER BY seq").fetchall(), [(1,), (2,)])
        self.assertEqual(len(self.carregar([NTNB_2029, NTNB_2029, outra])["novos"]), 0)  # recarregar nao duplica

    def test_baixar_ano_cai_de_xlsx_para_xls(self):
        with mock.patch.object(cl, "baixar", side_effect=lambda url: None if url.endswith(".xlsx") else b"xls"):
            self.assertEqual(cl.baixar_ano(2015), ("historico-leiloes-2015.xls", b"xls"))
        with mock.patch.object(cl, "baixar", return_value=None):
            self.assertIsNone(cl.baixar_ano(2027))

    def test_update_sem_arquivo_sai_com_2(self):
        with mock.patch.object(cl, "baixar", return_value=None), mock.patch.object(cl, "agora_br", return_value=datetime(2026, 6, 1)):
            self.assertEqual(cl.update(self.db, 2026), 2)

    def test_update_grava_e_calcula_metricas(self):
        with mock.patch.object(cl, "baixar_ano", return_value=("historico-leiloes-2026.xlsx", xlsx([LFT_2032, NTNB_2029, NTNB_2029_V2]))), \
                mock.patch.object(cl, "guardar_bruto"):
            self.assertEqual(cl.update(self.db, 2026), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM leilao_metricas").fetchone(), (3,))


class TestCalendario(unittest.TestCase):
    def test_feriados_2026(self):
        f = leiloes.feriados(2026)
        self.assertEqual(leiloes.pascoa(2026), date(2026, 4, 5))
        for d in (date(2026, 2, 16), date(2026, 2, 17), date(2026, 4, 3), date(2026, 6, 4), date(2026, 11, 20), date(2026, 11, 2)):
            self.assertIn(d, f)
        self.assertNotIn(date(2023, 11, 20), leiloes.feriados(2023))  # Consciencia Negra so desde 2024

    def test_dias_uteis(self):
        self.assertEqual(int(leiloes.du("2026-11-03", "2026-11-04")), 1)
        self.assertEqual(int(leiloes.du("2026-10-30", "2026-11-03")), 1)  # so o dia 03: 02/11 e feriado


class TestMetricas(unittest.TestCase):
    def test_ltn_duration_e_pu(self):
        # LTN que vence em 252 du com taxa de 10%: PU = 1000/1,1 e duration modificada = 1/1,1
        liq = "2026-11-04"
        venc = str(np.busday_offset(np.datetime64(liq), 252, holidays=leiloes.HOL))
        p, dur = leiloes.preco_duration("LTN", venc, liq, 0.10)
        self.assertAlmostEqual(p, 1000 / 1.1, places=6)
        self.assertAlmostEqual(dur, 1 / 1.1, places=6)

    def test_ntnf_ao_par_vale_mil(self):
        p, dur = leiloes.preco_duration("NTN-F", "2029-01-01", "2026-07-01", 1.10 ** 0.5 * 1.10 ** 0.5 - 1)
        self.assertAlmostEqual(p, 1000, delta=5)   # cupom de 10% a.a. descontado a 10% a.a., numa data de cupom
        self.assertTrue(0 < dur < 2.5)

    def test_ntnb_fluxos_em_fracao_do_vna(self):
        t, cf = leiloes.fluxos("NTN-B", "2029-05-15", "2026-11-04")
        self.assertEqual(len(cf), 6)  # 15/11/26 ... 15/05/29
        self.assertAlmostEqual(cf[0], 1.06 ** 0.5 - 1)
        self.assertAlmostEqual(cf[-1], 1.06 ** 0.5)

    def linhas_df(self, itens):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        cl.gravar(db, cl.parsear(xlsx(itens), "x.xlsx")[0], "x.xlsx")
        return db

    def test_metricas_basicas(self):
        db = self.linhas_df([LFT_2032, NTNB_2029, NTNB_2029_V2])
        leiloes.atualizar(db)
        m = {(r[0], r[1]): r[2:] for r in db.execute(
            "SELECT r.titulo,r.volta,m.absorcao,m.spread_bps,m.pu_implicito,m.bacen_share,m.dv01_brl,m.duracao_mod FROM leilao_resultados r "
            "JOIN leilao_metricas m ON m.resultado_id=r.id")}
        absorcao, spread, pu, bacen, dv01, dur = m[("NTN-B", 1)]
        self.assertEqual((absorcao, spread), (1.0, 0.0))
        self.assertAlmostEqual(pu, 1318785198.88 / 300000, places=4)
        self.assertAlmostEqual(bacen, 2000000 / 2300000)         # Bacen e adicional a venda
        self.assertAlmostEqual(dv01, dur * 1318785198.88 * 1e-4)
        self.assertIsNone(m[("LFT", 1)][4])                     # LFT: so financeiro, sem DV01
        self.assertEqual(m[("NTN-B", 2)][0], 0.0)               # 2a volta sem venda: absorcao 0, sem spread/DV01
        self.assertIsNone(m[("NTN-B", 2)][1])

    def test_compra_tem_dv01_negativo(self):
        l = linha(D(2026, 3, 16), "LTN", "Extra Compra", 1, D(2026, 3, 17), D(2028, 1, 1), 5000000.0, 0.1358, 0.1358, 5000000.0, 3983058000.0)
        db = self.linhas_df([l])
        leiloes.atualizar(db)
        self.assertLess(db.execute("SELECT dv01_brl FROM leilao_metricas").fetchone()[0], 0)

    def test_troca_e_lft_sem_dv01(self):
        l = linha(D(2010, 3, 16), "LTN", "Troca", 1, D(2010, 3, 17), D(2013, 1, 1), 5000000.0, 0.1, 0.1, 5000000.0, 3.0e9)
        db = self.linhas_df([l])
        leiloes.atualizar(db)
        self.assertIsNone(db.execute("SELECT dv01_brl FROM leilao_metricas").fetchone()[0])

    def test_concessao_usa_a_vespera_da_anbima_pulando_feriado(self):
        db = self.linhas_df([linha(D(2026, 11, 3), "NTN-B", "Venda", 1, D(2026, 11, 4), D(2029, 5, 15), 50000.0, 0.0790, 0.0795, 50000.0, 2.3e8)])
        db.executescript("CREATE TABLE anbima_titulos (data TEXT, titulo TEXT, vencimento TEXT, tx_indicativa REAL);"
                         "CREATE TABLE anbima_ettj (data TEXT, vertice_du INTEGER, ettj_ipca REAL, ettj_pre REAL);")
        db.executemany("INSERT INTO anbima_titulos VALUES (?,?,?,?)", [
            ("2026-10-29", "NTN-B", "2029-05-15", 7.90), ("2026-10-30", "NTN-B", "2029-05-15", 7.8649), ("2026-11-03", "NTN-B", "2029-05-15", 7.70)])
        db.executemany("INSERT INTO anbima_ettj VALUES (?,?,?,?)", [("2026-10-30", 400, 7.80, 14.0), ("2026-10-30", 800, 8.00, 15.0)])
        leiloes.atualizar(db)
        conc, dia, curva = db.execute("SELECT concessao_bps,anbima_data,curva_bps FROM leilao_metricas").fetchone()
        self.assertEqual(dia, "2026-10-30")                      # 02/11 foi feriado; 03/11 nao e vespera do proprio leilao
        self.assertAlmostEqual(conc, 795 - 786.49, places=2)
        self.assertIsNotNone(curva)

    def test_sem_anbima_concessao_fica_null(self):
        db = self.linhas_df([NTNB_2029])
        leiloes.atualizar(db)
        self.assertIsNone(db.execute("SELECT concessao_bps FROM leilao_metricas").fetchone()[0])


class TestZScore(unittest.TestCase):
    def serie(self, n, absorcoes=None):
        """n leiloes semanais de LTN 2029 1a volta, oferta 1.000.000, anteriores a 2026-01-01 + um alvo."""
        itens = []
        for k in range(n):
            d = date.fromordinal(date(2025, 1, 6).toordinal() + 7 * k)
            venda = 1_000_000.0 * (absorcoes[k] if absorcoes else 1.0 - 0.01 * (k % 5))
            itens.append(linha(datetime(d.year, d.month, d.day), "LTN", "Venda", 1, datetime(d.year, d.month, d.day), D(2029, 1, 1),
                               1_000_000.0, 0.13, 0.13, venda, venda * 800))
        return itens

    def z(self, itens):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        cl.gravar(db, cl.parsear(xlsx(itens), "x.xlsx")[0], "x.xlsx")
        leiloes.atualizar(db)
        return [r[0] for r in db.execute("SELECT m.z_absorcao_3a FROM leilao_resultados r JOIN leilao_metricas m ON m.resultado_id=r.id ORDER BY r.data")]

    def test_menos_de_8_observacoes_devolve_null(self):
        z = self.z(self.serie(9))                    # o 9o tem 8 anteriores; os 8 primeiros tem menos de 8
        self.assertTrue(all(x is None for x in z[:8]))
        self.assertIsNotNone(z[8])
        z = self.z(self.serie(8))                    # o ultimo tem so 7 anteriores
        self.assertTrue(all(x is None for x in z))

    def test_z_usa_so_o_passado(self):
        a = self.z(self.serie(10))
        b = self.z(self.serie(10) + self.serie(11)[10:])  # um leilao futuro nao muda o z dos anteriores
        self.assertEqual(a, b[:10])

    def test_leilao_fraco_tem_z_muito_negativo(self):
        absorcoes = [1.0, 0.98, 1.0, 0.99, 1.0, 0.97, 1.0, 1.0, 0.99, 0.2]
        self.assertLess(self.z(self.serie(10, absorcoes))[-1], -2)


class TestAlertas(unittest.TestCase):
    def rodar(self, itens, anbima=None):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        cl.gravar(db, cl.parsear(xlsx(itens), "x.xlsx")[0], "x.xlsx")
        if anbima:
            db.executescript("CREATE TABLE anbima_titulos (data TEXT, titulo TEXT, vencimento TEXT, tx_indicativa REAL);")
            db.executemany("INSERT INTO anbima_titulos VALUES (?,?,?,?)", anbima)
        leiloes.atualizar(db, None, "0000-00-00")
        return db

    def tipos(self, db):
        return sorted((r[0], r[1]) for r in db.execute("SELECT tipo,severidade FROM leilao_alertas"))

    def ltn(self, volta, oferta, venda, corte=0.13, data=D(2026, 11, 5), tipo="Venda", venc=D(2029, 1, 1)):
        return linha(data, "LTN", tipo, volta, D(2026, 11, 6), venc, oferta, corte, corte, venda, venda * 800)

    def test_fraco_por_absorcao(self):
        self.assertIn(("LEILAO_FRACO", "alta"), self.tipos(self.rodar([self.ltn(1, 1_000_000.0, 400_000.0)])))

    def test_aceitacao_parcial_e_so_informativa(self):
        self.assertEqual(self.tipos(self.rodar([self.ltn(1, 1_000_000.0, 600_000.0)])), [("ACEITACAO_PARCIAL", "info")])

    def test_venda_zero_aviso_na_1a_volta_e_baixa_na_2a(self):
        self.assertEqual(self.tipos(self.rodar([self.ltn(1, 1_000_000.0, 0.0)])), [("LEILAO_FRACO", "alta"), ("VENDA_ZERO", "aviso")])
        self.assertEqual(self.tipos(self.rodar([self.ltn(2, 250_000.0, 0.0)])), [("VENDA_ZERO", "baixa")])

    def test_leilao_integral_sem_alerta(self):
        self.assertEqual(self.tipos(self.rodar([self.ltn(1, 1_000_000.0, 1_000_000.0)])), [])

    def test_leilao_forte_precisa_de_concessao_e_2a_volta_integral(self):
        itens = [self.ltn(1, 1_000_000.0, 1_000_000.0, corte=0.1300), self.ltn(2, 250_000.0, 250_000.0, corte=0.1300)]
        db = self.rodar(itens, anbima=[("2026-11-04", "LTN", "2029-01-01", 13.05)])  # corte 130 bps... = 13,00% < 13,05%: concessao -5 bps
        self.assertIn(("LEILAO_FORTE", "info"), self.tipos(db))
        self.assertNotIn(("LEILAO_FORTE", "info"), self.tipos(self.rodar(itens)))  # sem ANBIMA nao ha concessao: nao inventa

    def test_extra_sempre_alerta_uma_vez_por_dia_e_titulo(self):
        itens = [self.ltn(1, 5e6, 5e6, tipo="Extra Compra", venc=D(2028, 1, 1)), self.ltn(1, 5e6, 1e6, tipo="Extra Compra", venc=D(2029, 1, 1)),
                 self.ltn(1, 5e5, 0.0, tipo="Extra Venda", venc=D(2029, 1, 1))]
        t = self.tipos(self.rodar(itens))
        self.assertEqual(t.count(("EXTRA_COMPRA", "aviso")), 1)
        self.assertEqual(t.count(("EXTRA_VENDA", "aviso")), 1)

    def test_bacen_informativo(self):
        l = linha(D(2026, 11, 5), "LFT", "Venda", 1, D(2026, 11, 6), D(2032, 3, 1), 750000.0, 0.0, 0.0, 750000.0, 1.35e10, 500000.0, 9e9)
        self.assertEqual(self.tipos(self.rodar([l])), [("BACEN", "info")])

    def test_lote_grande_por_z_de_tamanho(self):
        itens = [self.ltn(1, 1e6, 500_000.0 + 1000 * (k % 3), data=datetime.fromordinal(date(2025, 1, 6).toordinal() + 7 * k))
                 for k in range(10)]
        itens.append(self.ltn(1, 5e6, 3_000_000.0, data=D(2025, 12, 4)))
        self.assertIn(("LOTE_GRANDE", "info"), self.tipos(self.rodar(itens)))

    def test_lote_grande_por_dv01_do_dia(self):
        itens = []
        for k in range(45):
            d = datetime.fromordinal(date(2025, 1, 6).toordinal() + 7 * k)
            itens.append(self.ltn(1, 1e6, 1e6, data=d, venc=D(2029, 1, 1)))
        itens.append(self.ltn(1, 9e6, 9e6, data=D(2026, 6, 1)))
        self.assertIn(("LOTE_GRANDE_DV01", "aviso"), self.tipos(self.rodar(itens)))

    def test_sem_duplicar_alertas(self):
        db = self.rodar([self.ltn(1, 1_000_000.0, 400_000.0)])
        n = db.execute("SELECT COUNT(*) FROM leilao_alertas").fetchone()[0]
        leiloes.atualizar(db, None, "0000-00-00")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM leilao_alertas").fetchone()[0], n)

    def test_alertas_so_a_partir_da_data_pedida(self):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        cl.gravar(db, cl.parsear(xlsx([self.ltn(1, 1e6, 0.0, data=D(2020, 1, 7))]), "x.xlsx")[0], "x.xlsx")
        leiloes.atualizar(db, None, "2026-01-01")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM leilao_alertas").fetchone()[0], 0)


class TestAnbima(unittest.TestCase):
    TEXTO = ("ANBIMA - Associacao\r\nTaxas\r\nTitulo@Data Referencia@Codigo SELIC@Data Base/Emissao@Data Vencimento@Tx. Compra@Tx. Venda@Tx. Indicativas@PU@"
             "Desvio padrao@Interv. Ind. Inf. (D0)@Interv. Ind. Sup. (D0)@Interv. Ind. Inf. (D+1)@Interv. Ind. Sup. (D+1)@Criterio\r\n"
             "NTN-B@20261103@760199@20000715@20290515@7,8649@7,8449@7,8649@4718,342695@0,04@7,50@8,10@7,40@8,20@Calculado\r\n")

    def test_parse_do_arquivo_ms(self):
        resp = mock.Mock(content=self.TEXTO.encode("latin-1"))
        with mock.patch.object(coletor_anbima, "_requisicao", return_value=resp):
            regs = coletor_anbima.baixar_titulos(date(2026, 11, 3))
        self.assertEqual((regs[0]["titulo"], regs[0]["vencimento"], regs[0]["tx_indicativa"], regs[0]["pu"]), ("NTN-B", "2029-05-15", 7.8649, 4718.342695))

    def test_404_em_feriado_devolve_none(self):
        with mock.patch.object(coletor_anbima, "_requisicao", return_value=None):
            self.assertIsNone(coletor_anbima.baixar_titulos(date(2026, 11, 2)))


class TestPagina(unittest.TestCase):
    def test_montar_e_proximos(self):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        cl.gravar(db, cl.parsear(xlsx([LFT_2032, NTNB_2029, NTNB_2029_V2]), "x.xlsx")[0], "x.xlsx")
        leiloes.atualizar(db, None, "0000-00-00")
        m = leiloes.montar(db)
        self.assertEqual((m["dia"], len(m["linhas"]), len(m["resumo"])), ("2026-01-06", 3, 2))
        p = leiloes.proximos_leiloes(date(2026, 11, 4))
        self.assertEqual([x["data"] for x in p[:2]], ["2026-11-05", "2026-11-10"])  # quinta e terca
        self.assertIn("estimativa", p[0]["origem"])

    def test_montar_sem_dados(self):
        db = sqlite3.connect(":memory:")
        db.executescript(leiloes.SCHEMA)
        self.assertIsNone(leiloes.montar(db)["dia"])


if __name__ == "__main__":
    unittest.main()
