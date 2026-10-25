@echo off
rem 중앙 서버 통합 실행 (Windows). 더블클릭하면 서버와 웹이 켜지고 관리자 화면이 열립니다.
rem 모델 없이 시험하려면: run_server.bat --fake
chcp 65001 > nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_server.py %*
) else (
    python run_server.py %*
)
pause
