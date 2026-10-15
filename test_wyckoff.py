"""Testes do wyckoff.py com dados sinteticos (sem banco, sem rede).

  python -m unittest test_wyckoff -v
"""
import unittest

import numpy as np
import pandas as pd

import wyckoff as wy

SESSAO = ("09:00", "17:59")  # 9 barras de 60 min por dia nos testes


def roteiro_acumulacao():
    """Barras de 60 min (o, h, l, c, v): lateral, queda, SC, AR, range com ST, Spring, SOS, LPS, alta."""
    b, p = [], 1000.0

    def barra(o, c, amp=4.0, v=100.0, h=None, l=None):
        b.append((o, h if h is not None else max(o, c) + amp / 2, l if l is not None else min(o, c) - amp / 2, c, v))
        return c

    for i in range(40):                       # aquecimento lateral
        p = barra(p, 1000 + 2 * np.sin(i), v=100 + 5 * (i % 3))
    for _ in range(30):                       # tendencia de baixa
        p = barra(p, p - 3, v=100)
    p = barra(p, 892, h=p + 1, l=880, v=400)  # SC: climax de volume e amplitude, novo fundo
    for c in (898, 905, 912, 919, 926):       # AR
        p = barra(p, c, v=120)
    p = barra(p, 927, h=931, l=923, v=110)    # topo do AR (pivo)
    for c in (920, 912, 904, 897, 890):       # reacao
        p = barra(p, c, v=90)
    p = barra(p, 888, h=891, l=884, v=70)     # ST: reteste do fundo com volume menor
    for i in range(36):                       # fase B: oscilacao dentro do range
        p = barra(p, 907 + 13 * np.sin(i * 2 * np.pi / 12 + np.pi / 2), amp=6, v=80)
    p = barra(p, 892, v=80)
    p = barra(p, 890, h=893, l=874, v=110)   # Spring: perfura o fundo e fecha dentro
    for c in (897, 905, 913, 921, 927):
        p = barra(p, c, v=90)
    p = barra(p, 940, h=942, l=925, v=260)    # SOS
    for c in (945, 948, 942, 938):            # continua e recua com volume menor
        p = barra(p, c, v=80)
    p = barra(p, 936, h=939, l=933, v=60)     # LPS (pivo de fundo acima do meio do range)
    for c in (937, 940, 943, 946, 950, 955, 960, 965, 970, 975, 980, 985):  # fase E
        p = barra(p, c, v=110)
    return b


def espelhar(barras, eixo=2000.0):
    return [(eixo - o, eixo - l, eixo - h, eixo - c, v) for o, h, l, c, v in barras]


def para_1m(barras, dia0="2026-03-02"):
    """Expande barras de 60 min em 60 barras de 1 min (09:00-17:59, dias uteis). Caminho o -> extremo -> extremo -> c."""
    linhas, dia = [], pd.Timestamp(dia0)
    k = 0
    while k < len(barras):
        if dia.dayofweek >= 5:
            dia += pd.Timedelta(days=1)
            continue
        for hh in range(9, 18):
            if k >= len(barras):
                break
            o, h, l, c, v = barras[k]
            pts = [o, l, h, c] if c >= o else [o, h, l, c]
            cam = np.interp(np.linspace(0, 3, 61), [0, 1, 2, 3], pts)
            for m in range(60):
                a, z = cam[m], cam[m + 1]
                linhas.append((dia + pd.Timedelta(hours=hh, minutes=m), a, max(a, z), min(a, z), z, v / 60))
            k += 1
        dia += pd.Timedelta(days=1)
    df = pd.DataFrame(linhas, columns=["hora", "open", "high", "low", "close", "volume"]).set_index("hora")
    return df


class TestReamostragem(unittest.TestCase):
    def test_barras_e_parcial(self):
        m1 = para_1m(roteiro_acumulacao()[:20])
        b = wy.reamostrar(m1, "60min", SESSAO)
        self.assertEqual(len(b), 20)
        self.assertTrue(b.completa.all())
        self.assertTrue((b.index.normalize() == (b.ts_fim - pd.Timedelta(minutes=1)).dt.normalize()).all())
        b2 = wy.reamostrar(m1.iloc[:-10], "60min", SESSAO)  # ultima hora pela metade
        self.assertFalse(b2.completa.iloc[-1])
        self.assertTrue(b2.completa.iloc[:-1].all())

    def test_diario(self):
        m1 = para_1m(roteiro_acumulacao()[:27])
        b = wy.reamostrar(m1, "D", SESSAO)
        self.assertEqual(len(b), 3)
        self.assertAlmostEqual(b.volume.iloc[0], m1.volume.iloc[:540].sum())
        self.assertFalse(wy.reamostrar(m1.iloc[:-30], "D", SESSAO).completa.iloc[-1])

    def test_perfil(self):
        m1 = pd.DataFrame({"open": [10, 10, 20], "high": [10, 11, 21], "low": [10, 9, 19], "close": [10, 10, 20],
                           "volume": [100, 50, 10]}, index=pd.date_range("2026-01-05 10:00", periods=3, freq="1min"))
        prof, poc, vah, val = wy.perfil_volume(m1, wy.ConfigWyckoff(passo_preco=1.0))
        self.assertAlmostEqual(prof.volume.sum(), 160)
        self.assertAlmostEqual(poc, 10.5)
        self.assertLessEqual(val, 10)
        self.assertGreaterEqual(vah, 11)


class TestCenarios(unittest.TestCase):
    def rodar(self, barras):
        return wy.analisar(para_1m(barras), "60min", wy.ConfigWyckoff(), SESSAO)

    def test_acumulacao(self):
        r = self.rodar(roteiro_acumulacao())
        ev = list(r["eventos"].evento)
        seq = [e for e in ev if e in ("SC", "AR", "ST", "SPRING", "SOS", "LPS", "E")]
        for e in ("SC", "AR", "ST", "SPRING", "SOS", "LPS", "E"):
            self.assertIn(e, seq, f"faltou {e}: {ev}")
        self.assertEqual([x for x in dict.fromkeys(seq)], ["SC", "AR", "ST", "SPRING", "SOS", "LPS", "E"])
        self.assertNotIn("BC", ev)
        fases = r["fases"]
        self.assertTrue({"A", "B", "C", "D", "E"} <= set(fases.fase))
        self.assertTrue((fases.tipo[fases.fase.isin(["C", "D", "E"])] == "acumulacao").all())
        # o perfil de volume do range (fase B) tem POC dentro do range
        fb = fases[fases.fase == "B"].iloc[0]
        self.assertTrue(fb.range_lo <= fb.poc <= fb.range_hi and fb.val <= fb.poc <= fb.vah)
        # atraso documentado: AR e LPS saem L barras depois do extremo
        ar = r["eventos"][r["eventos"].evento == "AR"].iloc[0]
        self.assertEqual(ar.atraso_barras, wy.ConfigWyckoff().pivo)

    def test_distribuicao(self):
        r = self.rodar(espelhar(roteiro_acumulacao()))
        ev = list(r["eventos"].evento)
        seq = [e for e in ev if e in ("BC", "AR", "ST", "UTAD", "SOW", "LPSY", "E")]
        self.assertEqual([x for x in dict.fromkeys(seq)], ["BC", "AR", "ST", "UTAD", "SOW", "LPSY", "E"], ev)
        self.assertNotIn("SC", ev)
        fases = r["fases"]
        self.assertTrue((fases.tipo[fases.fase.isin(["C", "D", "E"])] == "distribuicao").all())

    def test_sem_lookahead(self):
        """Resultados das barras <= t nao mudam quando barras futuras sao acrescentadas (inclusive barra parcial)."""
        m1 = para_1m(roteiro_acumulacao())
        cfg = wy.ConfigWyckoff()
        cheio = wy.analisar(m1, "60min", cfg, SESSAO)
        colunas = ["open", "high", "low", "close", "volume", "atr", "vol_rel", "spread_rel", "pos_fech", "obv",
                   "fase", "tipo", "range_id", "range_lo", "range_hi", "evento"]
        for corte in range(600, len(m1), 377):  # cortes no meio de barras
            parc = wy.analisar(m1.iloc[:corte], "60min", cfg, SESSAO)
            pb, fb = parc["barras"], cheio["barras"].loc[parc["barras"].index]
            pd.testing.assert_frame_equal(pb[colunas], fb[colunas], check_dtype=False)
            ult = pb.index[-1]
            evf = cheio["eventos"][cheio["eventos"].hora <= ult].reset_index(drop=True)
            pd.testing.assert_frame_equal(parc["eventos"].reset_index(drop=True), evf, check_dtype=False)
            self.assertTrue((parc["eventos"].ts_disponivel <= pb.ts_fim.iloc[-1]).all())
            # metricas de segmentos ja encerrados tambem nao mudam
            fp, ff = parc["fases"], cheio["fases"]
            if len(fp) > 1:
                fechados = fp.iloc[:-1]
                pd.testing.assert_frame_equal(fechados.reset_index(drop=True),
                                              ff.iloc[:len(fechados)].reset_index(drop=True), check_dtype=False)


class TestPreset(unittest.TestCase):
    """Precedencia: defaults ConfigWyckoff < preset do ativo < overrides explicitos (None nao sobrescreve)."""

    def test_precedencia(self):
        cfg0, nome0 = wy.config_com_preset("BOVA11")
        self.assertIsNone(nome0)
        self.assertEqual(cfg0.pivo, 3)
        self.assertEqual(cfg0.sos_vol, 1.3)
        self.assertEqual(cfg0.passo_preco, 0.0)

        cfg1, nome1 = wy.config_com_preset("WIN")
        self.assertEqual(nome1, "WIN$")
        self.assertEqual(cfg1.pivo, 5)
        self.assertEqual(cfg1.sos_vol, 1.6)
        self.assertEqual(cfg1.sos_atr, 0.5)
        self.assertEqual(cfg1.largura_max_atr, 10.0)
        self.assertEqual(cfg1.pivos_janela, 30)
        self.assertEqual(cfg1.passo_preco, 50.0)
        # campos fora do preset ficam no default
        self.assertEqual(cfg1.n_atr, 14)
        self.assertEqual(cfg1.climax_vol, 2.0)

        cfg2, nome2 = wy.config_com_preset("WIN$", {"pivo": 7, "sos_vol": None, "n_atr": 20})
        self.assertEqual(nome2, "WIN$")
        self.assertEqual(cfg2.pivo, 7)          # flag explicita vence o preset
        self.assertEqual(cfg2.sos_vol, 1.6)     # None nao sobrescreve: fica o preset
        self.assertEqual(cfg2.n_atr, 20)        # override de campo sem preset
        self.assertEqual(cfg2.sos_atr, 0.5)     # resto do preset intacto

        # painel aplica o preset quando cfg=None (caminho do dashboard)
        self.assertEqual(wy.config_com_preset("win")[1], "WIN$")


if __name__ == "__main__":
    unittest.main()
