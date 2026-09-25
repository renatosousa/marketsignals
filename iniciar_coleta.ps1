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
}

$rodando = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*rodar_coleta.py*' } | ForEach-Object { $_.CommandLine.TrimEnd() }

foreach ($nome in $coletas.Keys) {
    $args_coleta = $coletas[$nome]
    # nao inicia uma segunda copia do mesmo supervisor (mesmos argumentos)
    if ($rodando | Where-Object { $_.EndsWith("rodar_coleta.py $($args_coleta -join ' ')") }) { continue }
    $log = "dados\supervisor_$nome.log"
    Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList (@('-u', 'rodar_coleta.py') + $args_coleta) `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
}
