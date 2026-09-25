# Inicia o supervisor de coleta (chamado pela tarefa agendada "JevTrader-Coleta" no logon).
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force dados | Out-Null

# nao inicia uma segunda copia se o supervisor ja estiver rodando
$ja = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*rodar_coleta.py*' }
if ($ja) { exit 0 }

& "$PSScriptRoot\.venv\Scripts\python.exe" -u rodar_coleta.py 'WIN$' --db dados/book.db *>> dados\supervisor.log
