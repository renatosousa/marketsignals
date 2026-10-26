"""Testes para funcoes de inflacao implicita do Tesouro Direto (curvas.py).

Uso:
    python test_tesouro.py
"""
from datetime import date

import curvas
import coletor_tesouro as ct


def test_breakeven_tesouro():
    """Testa calculo de inflacao implicita Prefixado vs IPCA+."""
    # Exemplo real: Prefixado 2029 12,64% vs IPCA+ 2029 6,77%
    be = curvas.breakeven_tesouro(12.64, 6.77)
    assert be is not None
    assert abs(be - 5.4978) < 0.01  # ~5,50% a.a.
    
    # Casos limites
    assert curvas.breakeven_tesouro(None, 6.77) is None
    assert curvas.breakeven_tesouro(12.64, None) is None
    assert curvas.breakeven_tesouro(None, None) is None
    
    # Pre = IPCA -> breakeven ~0
    be_zero = curvas.breakeven_tesouro(6.77, 6.77)
    assert be_zero is not None
    assert abs(be_zero) < 0.001
    
    print("OK test_breakeven_tesouro")


def test_parear_vencimentos():
    """Testa pareamento de titulos Prefixado e IPCA+ por safra."""
    titulos = [
        {"tipo_titulo": "Tesouro Prefixado 2029", "vencimento": "2029-01-01", "taxa_compra": 12.60, "taxa_venda": 12.68},
        {"tipo_titulo": "Tesouro IPCA+ 2029", "vencimento": "2029-05-15", "taxa_compra": 6.75, "taxa_venda": 6.79},
        {"tipo_titulo": "Tesouro Prefixado 2032", "vencimento": "2032-01-01", "taxa_compra": 12.80, "taxa_venda": 12.94},
        {"tipo_titulo": "Tesouro IPCA+ 2032", "vencimento": "2032-08-15", "taxa_compra": 6.75, "taxa_venda": 6.85},
        {"tipo_titulo": "Tesouro Prefixado 2035", "vencimento": "2035-01-01", "taxa_compra": 13.00, "taxa_venda": 13.10},
        # Sem par IPCA+ para 2035
    ]
    
    pares = curvas.parear_vencimentos_tesouro(titulos)
    
    assert len(pares) == 2  # 2029 e 2032 tem pares
    
    # Par 2029
    p29 = next(p for p in pares if p["safra"] == 2029)
    assert p29["venc_pre"] == "2029-01-01"
    assert p29["venc_ipca"] == "2029-05-15"
    assert abs(p29["taxa_pre"] - 12.64) < 0.01  # media de compra e venda
    assert abs(p29["taxa_ipca"] - 6.77) < 0.01
    assert p29["breakeven"] is not None
    assert abs(p29["breakeven"] - 5.50) < 0.1  # tolerancia maior
    assert p29["diferenca_dias"] == 134  # 01/01 ate 15/05
    
    # Par 2032
    p32 = next(p for p in pares if p["safra"] == 2032)
    assert p32["safra"] == 2032
    assert p32["venc_pre"] == "2032-01-01"
    assert p32["venc_ipca"] == "2032-08-15"
    assert abs(p32["taxa_pre"] - 12.87) < 0.01
    assert abs(p32["taxa_ipca"] - 6.80) < 0.01
    assert p32["diferenca_dias"] == 227  # 01/01 ate 15/08
    
    print("OK test_parear_vencimentos")


def test_parear_sem_taxa():
    """Testa que titulos sem taxa sao ignorados."""
    titulos = [
        {"tipo_titulo": "Tesouro Prefixado 2029", "vencimento": "2029-01-01", "taxa_compra": None, "taxa_venda": None},
        {"tipo_titulo": "Tesouro IPCA+ 2029", "vencimento": "2029-05-15", "taxa_compra": 6.75, "taxa_venda": 6.79},
    ]
    
    pares = curvas.parear_vencimentos_tesouro(titulos)
    assert len(pares) == 0  # nao ha par completo
    
    print("OK test_parear_sem_taxa")


def test_parear_apenas_compra():
    """Testa que titulos com apenas taxa de compra funcionam."""
    titulos = [
        {"tipo_titulo": "Tesouro Prefixado 2029", "vencimento": "2029-01-01", "taxa_compra": 12.64, "taxa_venda": None},
        {"tipo_titulo": "Tesouro IPCA+ 2029", "vencimento": "2029-05-15", "taxa_compra": 6.77, "taxa_venda": None},
    ]
    
    pares = curvas.parear_vencimentos_tesouro(titulos)
    assert len(pares) == 1
    
    p = pares[0]
    assert p["safra"] == 2029
    assert abs(p["taxa_pre"] - 12.64) < 0.01
    assert abs(p["taxa_ipca"] - 6.77) < 0.01
    
    print("OK test_parear_apenas_compra")


def test_parear_multiplos_anos():
    """Testa pareamento com varios anos."""
    titulos = [
        {"tipo_titulo": "Tesouro Prefixado 2027", "vencimento": "2027-01-01", "taxa_compra": 11.50, "taxa_venda": 11.60},
        {"tipo_titulo": "Tesouro IPCA+ 2026", "vencimento": "2026-05-15", "taxa_compra": 6.20, "taxa_venda": 6.30},
        {"tipo_titulo": "Tesouro Prefixado 2029", "vencimento": "2029-01-01", "taxa_compra": 12.60, "taxa_venda": 12.68},
        {"tipo_titulo": "Tesouro IPCA+ 2029", "vencimento": "2029-05-15", "taxa_compra": 6.75, "taxa_venda": 6.79},
        {"tipo_titulo": "Tesouro Prefixado 2040", "vencimento": "2040-01-01", "taxa_compra": 13.50, "taxa_venda": 13.60},
        {"tipo_titulo": "Tesouro IPCA+ 2040", "vencimento": "2040-08-15", "taxa_compra": 7.00, "taxa_venda": 7.10},
    ]
    
    pares = curvas.parear_vencimentos_tesouro(titulos)
    assert len(pares) == 2  # 2029 e 2040; 2027 e 2026 nao fazem par
    
    safras = sorted([p["safra"] for p in pares])
    assert safras == [2029, 2040]
    
    print("OK test_parear_multiplos_anos")


def test_pareamento_ignora_titulos_com_cupom():
    """NTN-F e NTN-B com juros semestrais nao podem sobrescrever o Prefixado/IPCA+ sem cupom do mesmo ano."""
    titulos = [
        {"tipo_titulo": "Tesouro Prefixado", "vencimento": "2029-01-01", "taxa_compra": 12.60, "taxa_venda": 12.68},
        {"tipo_titulo": "Tesouro Prefixado com Juros Semestrais", "vencimento": "2029-01-01", "taxa_compra": 9.00, "taxa_venda": 9.10},
        {"tipo_titulo": "Tesouro IPCA+", "vencimento": "2029-05-15", "taxa_compra": 6.75, "taxa_venda": 6.79},
        {"tipo_titulo": "Tesouro IPCA+ com Juros Semestrais", "vencimento": "2029-05-15", "taxa_compra": 4.00, "taxa_venda": 4.10},
    ]
    for ordem in (titulos, titulos[::-1]):  # o resultado nao pode depender da ordem das linhas
        pares = curvas.parear_vencimentos_tesouro(ordem)
        assert len(pares) == 1
        assert abs(pares[0]["taxa_pre"] - 12.64) < 0.01 and abs(pares[0]["taxa_ipca"] - 6.77) < 0.01
    print("OK test_pareamento_ignora_titulos_com_cupom")


CSV_MANHA = (
    "Tipo Titulo;Data Vencimento;Data Base;Taxa Compra Manha;Taxa Venda Manha;PU Compra Manha;PU Venda Manha;PU Base Manha\n"
    "Tesouro Prefixado;01/01/2029;22/10/2026;12,60;12,70;650,10;649,00;650,00\n"
    "Tesouro IPCA+;15/05/2029;22/10/2026;6,75;6,85;3.100,50;3.090,00;3.095,00\n"
    "Tesouro Selic;01/03/2031;22/10/2026;0,05;0,10;14.000,00;13.990,00;13.995,00\n"
)


def test_parser_aceita_colunas_com_e_sem_sufixo_manha():
    linhas = ct.parsear_csv(CSV_MANHA)
    assert len(linhas) == 2  # a Selic fica de fora
    pre = next(l for l in linhas if l["tipo_titulo"] == "Tesouro Prefixado")
    assert pre["data"] == "2026-10-22" and pre["vencimento"] == "2029-01-01"
    assert pre["taxa_compra"] == 12.60 and pre["taxa_venda"] == 12.70
    ipca = next(l for l in linhas if l["tipo_titulo"] == "Tesouro IPCA+")
    assert ipca["pu_compra"] == 3100.50  # separador de milhar
    sem_sufixo = CSV_MANHA.replace(" Manha", "")
    linhas2 = ct.parsear_csv(sem_sufixo)
    assert linhas2[0]["taxa_compra"] == 12.60
    print("OK test_parser_aceita_colunas_com_e_sem_sufixo_manha")


def test_parser_falha_com_cabecalho_inesperado():
    try:
        ct.parsear_csv("Titulo;Vencimento;Rentabilidade\nX;01/01/2029;12,6\n")
    except ValueError as e:
        assert "cabecalho" in str(e)
    else:
        raise AssertionError("devia falhar com cabecalho desconhecido (antes gravava NULL em silencio)")
    print("OK test_parser_falha_com_cabecalho_inesperado")


if __name__ == "__main__":
    test_breakeven_tesouro()
    test_parear_vencimentos()
    test_parear_sem_taxa()
    test_parear_apenas_compra()
    test_parear_multiplos_anos()
    test_pareamento_ignora_titulos_com_cupom()
    test_parser_aceita_colunas_com_e_sem_sufixo_manha()
    test_parser_falha_com_cabecalho_inesperado()
    print("\nTodos os testes passaram!")
