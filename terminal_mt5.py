"""Conexao com o MT5 da Genial (a unica corretora com os dados da B3 que o projeto usa).

Ha mais de um MT5 instalado no PC (Genial, MetaTrader 5 generico com conta demo da MetaQuotes, ActivTrades). Sem `path`,
`mt5.initialize()` abre ou se liga ao terminal registrado no Windows: em 5/11/2026 o MT5 generico se atualizou as 09:05,
virou o padrao e os coletores abriram a conta demo (initialize: "Authorization failed"). Aqui o caminho e fixo (JEV_MT5
sobrepoe) e a conexao a outra corretora e recusada, para nunca gravar cotacao de outra fonte no banco.
"""
import os

CAMINHO = os.environ.get("JEV_MT5", r"C:\Program Files\Genial Investimentos MetaTrader 5\terminal64.exe")
SERVIDOR = "Genial"  # parte do nome do servidor da conta (GenialInvestimentos-PRD)


def iniciar(mt5):
    """Como mt5.initialize(): True se conectou (ver mt5.last_error() se False). Sai (SystemExit) se o terminal estiver
    logado em outra corretora."""
    if not (mt5.initialize(path=CAMINHO) if os.path.exists(CAMINHO) else mt5.initialize()):
        return False
    conta = mt5.account_info()
    if conta is not None and SERVIDOR.lower() not in (conta.server or "").lower():
        mt5.shutdown()
        raise SystemExit(f"MT5 conectado a {conta.server} (conta {conta.login}), nao a {SERVIDOR}: abra o MT5 da Genial "
                         f"({CAMINHO})")
    return True
