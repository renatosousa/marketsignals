"""Cliente da API publica do BDI (Boletim Diario) da B3: posicao em aberto de derivativos listados.

*** NAO USADO EM PRODUCAO (ver opcoes_manual.py) ***
Endpoint descoberto por engenharia reversa do portal https://arquivos.b3.com.br/bdi/tabelas
(tabela "OpenPositionsEquities", classificacao Derivativos > Derivativos de bolsa). Publico, sem
autenticacao, dados de D-1. Validado campo a campo via requisicao feita PELO NAVEGADOR contra a
tela do site em 29/09/2026 (BOVAJ181W1: coberto=44, travado=1.572.331, descoberto=55.131,
titulares=45, lancadores=80 -> bate).

PROBLEMA CONHECIDO: via `requests` puro (sem navegador), a paginacao nao e confiavel -- em
testes repetidos no mesmo dia, duas varreduras completas das 45 paginas (~44 mil linhas cada)
NUNCA encontraram esse mesmo simbolo, enquanto o navegador (mesma URL, mesmo metodo) encontrava
em poucas paginas. A causa provavel e algum tipo de mitigacao anti-bot (fingerprint de
TLS/HTTP) que serve conteudo degradado/duplicado para clientes nao-navegador, sem erro explicito.
Por isso o dashboard usa colagem manual (opcoes_manual.py) em vez deste modulo. Fica aqui como
base caso um dia valha a pena automatizar via navegador real (Playwright).

Colunas (indice no array 'values' -> nome B3 -> significado):
  0 TckrSymb   simbolo (ex.: BOVAJ181W1)           9 TtlBlckdPos  travado
  1 ISIN                                          10 UcvrdQty     descoberto
  2 Asst       ativo-objeto (4 letras, sem o 11)   11 TtlPos       total (coberto+travado+descob.)
  3 XprtnCd                                        12 BrrwrQty     titulares (nº de investidores)
  4 SgmtNm     "EQUITY CALL" / "EQUITY PUT"        13 LndrQty      lancadores (nº de investidores)
  5 OpnIntrst                                      14 CurQty
  6 VartnOpnIntrst                                 15 LockedQty
  7 DstrbtnId  id da serie/distribuicao            16 UnlockedQty
  8 CvrdQty    coberto                             17 (campo sem nome nos metadados, ~sempre null)
                                                    18 FwdPric

Uso como biblioteca:
    from posicoes_b3 import buscar_posicoes
    df = buscar_posicoes("BOVA", "2026-09-28")

Uso como script: python posicoes_b3.py BOVA 2026-09-28
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

BASE = "https://arquivos.b3.com.br/bdi/table/OpenPositionsEquities"
PAGE_SIZE = 1000
PARALELISMO = 8
COLUNAS = ["symbol", "isin", "subjacente", "xprtn_cd", "tipo_b3", "opn_intrst", "var_opn_intrst",
          "distribuicao_id", "coberto", "travado", "descoberto", "total_pos", "titulares",
          "lancadores", "cur_qty", "locked_qty", "unlocked_qty", "_reservado", "fwd_pric"]


def _pagina(sessao, p, tentativas=3):
    for i in range(tentativas):
        try:
            r = requests.post(f"{BASE}/{sessao}/{sessao}/{p}/{PAGE_SIZE}", json={}, timeout=30,
                              headers={"Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()["table"]
        except (requests.RequestException, KeyError, ValueError):
            if i == tentativas - 1:
                raise
            time.sleep(1 + i)


def buscar_posicoes(subjacente, sessao, so_ativo=True):
    """Posicao em aberto de todas as series do ativo-objeto (4 letras, ex. 'BOVA') numa sessao
    (data 'YYYY-MM-DD', pregao/dado D-1 normalmente ja disponivel). Sequencia paginada: a 1a
    pagina informa quantas existem no total; as demais sao buscadas em paralelo.
    -> DataFrame com as COLUNAS acima, so as linhas de `subjacente` se so_ativo=True.
    """
    primeira = _pagina(sessao, 1)
    n_paginas = primeira["pageCount"]
    linhas = list(primeira["values"])
    if n_paginas > 1:
        with ThreadPoolExecutor(PARALELISMO) as ex:
            for pag in ex.map(lambda p: _pagina(sessao, p)["values"], range(2, n_paginas + 1)):
                linhas += pag
    df = pd.DataFrame(linhas, columns=COLUNAS).drop_duplicates()  # paginas concorrentes podem se sobrepor
    if so_ativo:
        df = df[df["subjacente"].str.upper() == subjacente.upper()].reset_index(drop=True)
    df["sessao"] = sessao
    return df


if __name__ == "__main__":
    sub = sys.argv[1] if len(sys.argv) > 1 else "BOVA"
    sess = sys.argv[2] if len(sys.argv) > 2 else pd.Timestamp.now().strftime("%Y-%m-%d")
    df = buscar_posicoes(sub, sess)
    print(f"{len(df)} series de {sub} em {sess}")
    print(df[["symbol", "tipo_b3", "coberto", "travado", "descoberto", "titulares", "lancadores"]]
          .sort_values("descoberto", ascending=False).head(15).to_string(index=False))
