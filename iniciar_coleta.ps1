# Inicia um supervisor por coleta (chamado pela tarefa agendada "JevTrader-Coleta" no logon).
# Todos gravam em dados/book.db; cada coleta tem seu log em dados/supervisor_<nome>.log.
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force dados | Out-Null

# nome do log -> argumentos do rodar_coleta.py (ativo, --db, extras)
$coletas = [ordered]@{
    'WIN'           = @('WIN$', '--db', 'dados/book.db')
    # acoes nao tem historico de ticks na Genial (copy_ticks espera ~100 s e trava o MT5): agressao pelo tick ao vivo
    'BOVA11'        = @('BOVA11', '--db', 'dados/book.db', '--trades-tick')
    'BOVA11_opcoes' = @('BOVA11', '--db', 'dados/book.db', '--script', 'coletor_opcoes.py')
    'MACRO'         = @('MACRO', '--db', 'dados/book.db', '--script', 'coletor_macro.py')
    # VIX (CBOE, atrasado ~15 min) pela cotacao publica da Cboe, sem login; substitui o coletor da IBKR
    'VIX'           = @('VIX', '--db', 'dados/book.db', '--script', 'coletor_vix.py')
    # saldo de agressao calculado pelo Profit (RTD); so grava com o Profit aberto e logado
    'PROFIT'        = @('PROFIT', '--db', 'dados/book.db', '--script', 'coletor_profit_rtd.py')
    # leiloes de compromissadas do BC (Tomador 09:00, 1047 12:00-12:30) e Selic: busca a cada 20 min
    'BCB'           = @('BCB', '--db', 'dados/book.db', '--script', 'coletor_bcb.py')
    # desativado em 19/10/2026: so gravava o VIX e exigia o IB Gateway logado todo dia (coletor_ibkr.py segue no repo)
    # 'IBKR'        = @('IBKR', '--db', 'dados/book.db', '--script', 'coletor_ibkr.py')
    # agressao por minuto de 12 acoes + WDO (variaveis para estimar o fluxo estrangeiro; coletor_agressao.py)
    'AGRESSAO'      = @('AGRESSAO', '--db', 'dados/book.db', '--script', 'coletor_agressao.py')
    # regime do WIN a cada minuto; na partida treina o modelo do dia (modelo_regime.py)
    'REGIME'        = @('REGIME', '--db', 'dados/book.db', '--script', 'coletor_regime.py')
    # noticias (RSS BR, BCB, Fed, Finnhub, GDELT) + sentimento FinBERT local; ver coletor_noticias.py
    # DESATIVADO em 20/10/2026: faltam feedparser/transformers/FinBERT no .venv e o supervisor reiniciava a cada 10 s.
    # Para reativar: instalar as dependencias (README_NOTICIAS.md) e descomentar a linha abaixo.
    # 'NOTICIAS'    = @('NOTICIAS', '--db', 'dados/book.db', '--script', 'coletor_noticias.py', '--inicio', '06:00', '--fim', '23:30')
}

$rodando = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*rodar_coleta.py*' } | ForEach-Object { $_.CommandLine.TrimEnd() }

# dashboard (http://127.0.0.1:8050): vigia proprio, fora do horario dos coletores, reinicia se cair
$vigia = Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" | Where-Object { $_.CommandLine -like '*vigiar_dashboard.ps1*' }
if (-not $vigia) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "$PSScriptRoot\vigiar_dashboard.ps1") `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput 'dados\vigia_dashboard.log' -RedirectStandardError 'dados\vigia_dashboard.log.err'
}

foreach ($nome in $coletas.Keys) {
    $args_coleta = $coletas[$nome]
    # nao inicia uma segunda copia do mesmo supervisor (mesmos argumentos)
    if ($rodando | Where-Object { $_.EndsWith("rodar_coleta.py $($args_coleta -join ' ')") }) { continue }
    $log = "dados\supervisor_$nome.log"
    Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList (@('-u', 'rodar_coleta.py') + $args_coleta) `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
}
