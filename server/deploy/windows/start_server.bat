@echo off
rem Fall detection PC server launcher. Restarts the server when it stops (wait 5-60 s), one instance only.
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
rem 낙상 감지 PC 서버 실행. 서버가 꺼지면(오류·자동 업데이트) 다시 켠다. 멈추려면 이 창을 닫는다.
rem  - 저장소 최상위 폴더 기준으로 실행한다. 다른 파이썬·설정을 쓰려면 FALL_PYTHON, FALL_SERVER_CONFIG 환경변수.
rem  - 서버 기록은 서버가 직접 data\logs\server.log에 쓴다(10 MB씩 5개까지 돌려 씀, FALL_LOG_FILE로 바꿀 수 있음).
rem    콘솔 출력(경고·오류, 갑자기 꺼질 때의 오류 내용)은 data\logs\server-console.log에 모은다.
rem  - 이미 서버가 떠 있으면(/api/health 응답) 새로 켜지 않는다.
rem  - 켜진 지 30초 안에 꺼지면 다시 켜기 전 기다리는 시간을 5초에서 60초까지 늘린다.
rem  - 설정 오류(종료 코드 2)면 다시 켜지 않고 멈춘다. 고친 뒤 아무 키나 누르면 다시 시작한다.
rem 경로에 ( ) & 가 있어도 되게 경로 변수는 늘 따옴표로 감싸고, ( ) 블록 대신 goto로 흐름을 나눈다.
rem 자동 업데이트가 실행 중인 이 파일을 바꿀 수 있다(cmd는 다음 줄을 파일의 같은 바이트 위치에서 다시 읽는다).
rem 그래서 오래 걸리는 명령(서버 실행·기다림) 뒤에는 같은 줄에서 goto해 바뀐 파일의 이름표에서 이어 가게 한다.
cd /d "%~dp0..\..\.."
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if defined FALL_PYTHON set "PYTHON=%FALL_PYTHON:"=%"
if exist "%PYTHON%" goto have_python
echo [오류] 파이썬 가상환경이 없습니다: "%PYTHON%"
echo server\README.md의 '1. 설치'를 먼저 하세요.
pause
exit /b 1

:have_python
if defined FALL_SERVER_CONFIG goto have_config
if exist "server\config.toml" goto have_config
echo [오류] server\config.toml이 없습니다. server\config.example.toml을 복사해 만드세요.
pause
exit /b 1

:have_config
if not exist "data\logs" mkdir "data\logs"
set "LOG=data\logs\server-console.log"
set "CLOCK=data\logs\server-clock.tmp"
if not defined FALL_LOG_FILE set "FALL_LOG_FILE=%CD%\data\logs\server.log"
set "PYTHONUTF8=1"
title 낙상 감지 서버

rem 이 창을 두 번 열었거나 작업 스케줄러와 겹쳐 서버가 이미 응답하면 하나만 둔다(포트는 서버 설정에서 읽음).
"%PYTHON%" -c "import sys,json,urllib.request as r;from server.config import load_settings;u='http://127.0.0.1:'+str(load_settings().port)+'/api/health';ok=json.load(r.build_opener(r.ProxyHandler({})).open(u,timeout=3)).get('ok') is True;ok and print('  '+u);sys.exit(0 if ok else 1)" 2>nul
if errorlevel 1 goto start
echo [안내] 위 주소에서 낙상 감지 서버가 이미 실행 중이라 새로 켜지 않습니다. 이 창은 10초 뒤 닫힙니다.
timeout /t 10 >nul
exit /b 0

:start
set "DELAY=5"

:loop
if not exist "%LOG%" goto log_ready
for %%A in ("%LOG%") do if %%~zA GTR 10485760 move /y "%LOG%" "data\logs\server-console.old.log" >nul
:log_ready
call :now STARTED
echo [%date% %time%] 서버 시작 (기록: "%FALL_LOG_FILE%")
>>"%LOG%" echo [%date% %time%] 서버 시작
"%PYTHON%" -m server >>"%LOG%" 2>&1 & goto stopped

:stopped
set "CODE=%errorlevel%"
call :now ENDED
set /a RAN=ENDED-STARTED
if "%CODE%"=="2" goto config_error
if %RAN% GEQ 30 set "DELAY=5"
echo [%date% %time%] 서버 종료 (코드 %CODE%, %RAN%초 실행), %DELAY%초 뒤 다시 시작합니다
>>"%LOG%" echo [%date% %time%] 서버 종료 (코드 %CODE%, %RAN%초 실행), %DELAY%초 뒤 다시 시작
set /a WAIT=DELAY, PINGS=DELAY+1
if %RAN% LSS 30 set /a DELAY=DELAY*2
if %DELAY% GTR 60 set "DELAY=60"
rem timeout은 키보드 입력이 없는 창에서 바로 실패하므로 그때는 ping으로 기다린다.
(timeout /t %WAIT% /nobreak >nul 2>&1 || ping -n %PINGS% 127.0.0.1 >nul) & goto loop

:config_error
echo [%date% %time%] 서버가 설정 오류로 멈췄습니다 (종료 코드 2). 자동으로 다시 켜지 않습니다.
>>"%LOG%" echo [%date% %time%] 설정 오류(코드 2)로 멈춤. 다시 켜지 않고 기다림
echo ----- 콘솔 출력 마지막 부분: "%CD%\%LOG%" -----
"%PYTHON%" -c "import sys;print(''.join(open(sys.argv[1],encoding='utf-8',errors='replace').readlines()[-12:]).rstrip())" "%LOG%"
echo -----
echo server\config.toml(또는 FALL_SERVER_CONFIG), DEVICE_TOKEN, 파이썬 버전(3.11 이상)을 확인해 고친 뒤
echo 아무 키나 누르면 다시 시작합니다.
pause >nul & set "DELAY=5" & goto loop

:now
rem 첫 인자로 받은 이름의 변수에 지금 시각(초)을 넣는다. 지역 설정마다 모양이 다른 time 변수 대신 파이썬으로 잰다.
set "%~1=0"
"%PYTHON%" -c "import time;print(int(time.time()))" >"%CLOCK%" 2>nul
set /p "%~1=" <"%CLOCK%"
exit /b 0
