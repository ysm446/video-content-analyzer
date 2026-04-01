@echo off
chcp 65001 > nul
setlocal

echo =============================================
echo  Release VRAM
echo =============================================
echo.
echo Stopping Video Content Analyzer backend processes...

powershell -NoLogo -NoProfile -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "$killed=@();" ^
  "$llama=Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'llama-server.exe' };" ^
  "foreach($p in $llama){ Stop-Process -Id $p.ProcessId -Force; $killed += ('llama-server.exe PID=' + $p.ProcessId) };" ^
  "$python=Get-CimInstance Win32_Process | Where-Object {" ^
  "  ($_.Name -match '^python(?:w)?(?:\.exe)?$' -or $_.Name -ieq 'py.exe') -and (" ^
  "    $_.CommandLine -match 'run_backend\.py' -or" ^
  "    $_.CommandLine -match 'uvicorn.+backend\.server:app'" ^
  "  )" ^
  "};" ^
  "foreach($p in $python){ Stop-Process -Id $p.ProcessId -Force; $killed += ($p.Name + ' PID=' + $p.ProcessId) };" ^
  "$ports=8765,8767;" ^
  "foreach($port in $ports){" ^
  "  $conns=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue;" ^
  "  foreach($conn in $conns){" ^
  "    if($conn.OwningProcess -and -not ($killed -match ('PID=' + $conn.OwningProcess + '$'))){" ^
  "      Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue;" ^
  "      $killed += ('port ' + $port + ' PID=' + $conn.OwningProcess)" ^
  "    }" ^
  "  }" ^
  "};" ^
  "if($killed.Count -eq 0){ Write-Host 'No target processes were running.' } else { Write-Host 'Stopped:'; $killed | ForEach-Object { Write-Host (' - ' + $_) } }"

echo.
echo VRAM should be released after the processes fully exit.
echo If memory still remains, close Electron as well and run this again.
echo.
pause

