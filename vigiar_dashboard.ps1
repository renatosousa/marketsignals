# Mantem o dashboard.py no ar: confere a porta 8050 a cada 60s e reinicia se cair.
# Chamado por iniciar_coleta.ps1 (login) e roda continuamente em segundo plano.
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force dados | Out-Null

while ($true) {
    $vivo = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*dashboard.py*' }
    if (-not $vivo) {
        Write-Output "[$(Get-Date -Format 'dd/MM HH:mm:ss')] dashboard fora do ar, reiniciando"
        Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList @('-u', 'dashboard.py', '--porta', '8050') `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
            -RedirectStandardOutput 'dados\dashboard.log' -RedirectStandardError 'dados\dashboard.log.err'
    }
    Start-Sleep -Seconds 60
}
