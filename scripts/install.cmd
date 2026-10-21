@echo off
rem Indus Agent installer for Command Prompt.
rem Runs the PowerShell installer from the latest release in a PowerShell process whose
rem execution policy is bypassed for that one process only. Arguments are passed through,
rem for example: install.cmd -Channel canary
setlocal
set "INDUS_INSTALL_PS1=https://github.com/indusagi/indus-agent/releases/latest/download/install.ps1"
echo.
echo  Indus Agent installer
echo  Starting the PowerShell installer...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod -UseBasicParsing '%INDUS_INSTALL_PS1%'))) %*; exit $LASTEXITCODE"
if errorlevel 1 (
    echo.
    echo  The install did not finish. To run it from PowerShell yourself:
    echo    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm '%INDUS_INSTALL_PS1%' | iex"
    echo.
    pause
    endlocal
    exit /b 1
)
endlocal
exit /b 0
