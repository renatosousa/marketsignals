# Agenda o download da posicao em aberto das opcoes de BOVA11 e de dolar (BDI da B3, D-1) em dias uteis.
# A B3 publica a posicao de D-1 de madrugada e o cadastro de series por volta das 07:25 (em 29/10/2026); a rodada
# das 08:30 pega publicacao atrasada (o script sai com 2 e nada grava quando ainda nao saiu; regravar e idempotente).
# --metodo paginas: a exportacao CSV morreu em 499 depois de 4 min em 07/10 e 29/10/2026; a paginacao leva ~1,5 min.
# Uso: powershell -ExecutionPolicy Bypass -File agendar_posicoes_b3.ps1
$py = "C:\desenv\Jev-trader\.venv\Scripts\python.exe"
# duas acoes em sequencia: opcoes de BOVA11 (painel e GEX do WIN) e opcoes de dolar DOL/WDO (GEX do WDO, ~4 min)
$acao = @((New-ScheduledTaskAction -Execute $py -Argument "posicoes_b3_csv.py --metodo paginas" -WorkingDirectory "C:\desenv\Jev-trader"),
          (New-ScheduledTaskAction -Execute $py -Argument "posicoes_b3_dolar.py" -WorkingDirectory "C:\desenv\Jev-trader"))
$dias = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$gatilhos = @((New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dias -At "07:45"),
              (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dias -At "08:30"))
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew
$usuario = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "JevTrader-PosicoesB3" -Action $acao -Trigger $gatilhos -Settings $config `
    -Principal $usuario -Force `
    -Description "Posicao em aberto das opcoes de BOVA11 e de dolar (B3, D-1) para o painel e o GEX do WIN e do WDO." | Out-Null
Get-ScheduledTask -TaskName "JevTrader-PosicoesB3" | Get-ScheduledTaskInfo | Format-List TaskName, NextRunTime
