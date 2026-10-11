"""Testes basicos do coletor de noticias.

Verifica normalizacao, deduplicacao, parse de feeds, e regras anti-lookahead.
"""
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import coletor_noticias as cn
import banco


def test_canon_url():
    """Testa canonicalizacao de URL."""
    assert cn.canon_url("https://Example.com/path/?utm_source=twitter&ref=fb#anchor") == \
           "https://example.com/path"
    assert cn.canon_url("https://site.com/page/") == "https://site.com/page"
    assert cn.canon_url("https://site.com/") == "https://site.com/"
    print("  test_canon_url: OK")


def test_norm_titulo():
    """Testa normalizacao de titulo."""
    assert "bolsa caiu" in cn.norm_titulo("A bolsa caiu.")
    assert "bolsa caiu" in cn.norm_titulo("URGENTE: A bolsa caiu!")
    assert "acoes" in cn.norm_titulo("Ações sobem")
    print("  test_norm_titulo: OK")


def test_jaccard():
    """Testa similaridade de Jaccard."""
    assert cn.jaccard("bolsa caiu forte", "bolsa caiu muito") > 0.5
    assert cn.jaccard("bolsa sobe", "dolar cai") < 0.2
    print("  test_jaccard: OK")


def test_ts_br_ms():
    """Testa conversao de timestamps."""
    struct_utc = time.strptime("2026-10-11 12:00:00", "%Y-%m-%d %H:%M:%S")
    ts = cn.ts_br_ms(struct_time_utc=struct_utc)
    
    dt_esperado = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)
    ts_esperado = int(dt_esperado.timestamp() * 1000)
    assert abs(ts - ts_esperado) < 1000
    print("  test_ts_br_ms: OK")


def test_filtro_relevancia():
    """Testa filtro de palavras-chave."""
    import threading
    parar = threading.Event()
    c = cn.Classificador(":memory:", False, parar)
    
    rel, tema = c._filtro_relevancia("Copom eleva Selic", "", "infomoney")
    assert rel == 1.0
    assert tema == "copom_selic"
    
    rel, tema = c._filtro_relevancia("Receita lança edital", "", "valor")
    assert rel < 1.0
    print("  test_filtro_relevancia: OK")


def test_dedup():
    """Testa deduplicacao no banco."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = banco.abrir(str(db_path), cn.SCHEMA)
        sessao = "20261011_120000"
        ts_coleta = int(datetime.now(timezone.utc).timestamp() * 1000)
        
        from collections import deque
        dedup = deque(maxlen=1000)
        
        noticias = [
            {"titulo": "Bolsa sobe", "url": "https://site.com/1", "resumo": "Texto 1",
             "ts_pub_ms": ts_coleta - 60000, "fonte": "test", "idioma": "pt", "evento": None},
            {"titulo": "Bolsa sobe", "url": "https://site.com/2", "resumo": "Texto 2",
             "ts_pub_ms": ts_coleta - 50000, "fonte": "test", "idioma": "pt", "evento": None},
        ]
        
        n_novas, n_dup, originais = cn.gravar_noticias(db, noticias, sessao, ts_coleta, dedup)
        assert n_novas == 1
        assert n_dup == 1
        
        cursor = db.execute("SELECT COUNT(*) FROM noticias")
        assert cursor.fetchone()[0] == 2
        
        cursor = db.execute("SELECT COUNT(*) FROM noticias WHERE dup_de IS NOT NULL")
        assert cursor.fetchone()[0] == 1
        
        db.close()
        print("  test_dedup: OK")


def test_anti_lookahead():
    """Testa regra anti-lookahead: ts_pub_ms > ts_coleta_ms + 5min -> NULL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = banco.abrir(str(db_path), cn.SCHEMA)
        sessao = "20261011_120000"
        ts_coleta = int(datetime.now(timezone.utc).timestamp() * 1000)
        
        from collections import deque
        dedup = deque(maxlen=1000)
        
        noticias = [
            {"titulo": "Noticia futura", "url": "https://site.com/fut", "resumo": "",
             "ts_pub_ms": ts_coleta + 10 * 60000, "fonte": "test", "idioma": "pt", "evento": None},
        ]
        
        cn.gravar_noticias(db, noticias, sessao, ts_coleta, dedup)
        
        cursor = db.execute("SELECT ts_pub_ms FROM noticias WHERE titulo = 'Noticia futura'")
        row = cursor.fetchone()
        assert row[0] is None
        
        db.close()
        print("  test_anti_lookahead: OK")


def test_agregacao():
    """Testa agregacao de 1 min (mock simples)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = banco.abrir(str(db_path), cn.SCHEMA)
        sessao = "20261011_120000"
        ts_coleta = int(datetime.now(timezone.utc).timestamp() * 1000)
        ts_min = (ts_coleta // 60000) * 60000
        
        from collections import deque
        dedup = deque(maxlen=1000)
        
        noticias = [
            {"titulo": f"Noticia {i}", "url": f"https://site.com/{i}", "resumo": "",
             "ts_pub_ms": ts_coleta - 1000 * i, "fonte": "test", "idioma": "pt", "evento": None}
            for i in range(5)
        ]
        
        n_novas, n_dup, originais = cn.gravar_noticias(db, noticias, sessao, ts_coleta, dedup)
        
        for orig in originais:
            db.execute(
                "INSERT INTO noticias_score VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (orig["id"], "finbert", "test", ts_coleta, -0.5, 0.1, 0.7, 0.2, 0.7, 0.8, "fiscal_br", None,
                 "{}")
            )
        db.commit()
        
        cn.agregar(db, ts_coleta, ["finbert"])
        
        cursor = db.execute("SELECT * FROM noticias_1min WHERE ts_ms = ?", (ts_min,))
        linhas = cursor.fetchall()
        assert len(linhas) > 0
        
        db.close()
        print("  test_agregacao: OK")


def main():
    print("\nExecutando testes do coletor_noticias...\n")
    
    test_canon_url()
    test_norm_titulo()
    test_jaccard()
    test_ts_br_ms()
    test_filtro_relevancia()
    test_dedup()
    test_anti_lookahead()
    test_agregacao()
    
    print("\nTodos os testes passaram!\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
