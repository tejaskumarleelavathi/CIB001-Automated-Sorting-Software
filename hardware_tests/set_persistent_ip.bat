@echo off
REM Set persistent IPs on Lucid cameras using IpConfigUtility.
REM overhead 253102168 -> 172.16.9.21
REM eoa      253102169 -> 172.16.9.22
REM
REM Run this from a normal command prompt (not Git Bash).
REM After it completes: power-cycle BOTH cameras.

SET UTIL="C:\Program Files\Lucid Vision Labs\Arena SDK\x64Release\IpConfigUtility_v140.exe"
SET OVERHEAD_MAC=0x1C0FAFC23EA0
SET EOA_MAC=0x1C0FAFC23F9D

echo ============================================
echo Setting persistent IPs on Lucid cameras
echo ============================================
echo.

echo [1/4] Listing current cameras...
%UTIL% /list
echo.

echo [2/4] Setting overhead camera (253102168) to 172.16.9.21 ...
%UTIL% /persist -m %OVERHEAD_MAC% -a 172.16.9.21 -s 255.255.255.0 -g 0.0.0.0
echo.

echo Waiting 2s...
timeout /t 2 /nobreak >nul

echo [3/4] Setting eoa camera (253102169) to 172.16.9.22 ...
%UTIL% /persist -m %EOA_MAC% -a 172.16.9.22 -s 255.255.255.0 -g 0.0.0.0
echo.

echo [4/4] Listing cameras to verify...
%UTIL% /list
echo.

echo ============================================
echo Done. POWER-CYCLE BOTH CAMERAS NOW.
echo After reboot:
echo   overhead (253102168) will be at 172.16.9.21
echo   eoa      (253102169) will be at 172.16.9.22
echo ============================================
pause
