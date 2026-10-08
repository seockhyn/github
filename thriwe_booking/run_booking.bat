@echo off
rem Windows 작업 스케줄러에서 실행할 배치 파일 (아부다비 자정 10분 전에 실행되도록 등록)
rem 실행 옵션을 그대로 넘김. 예) run_booking.bat --tee 16:00
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where py >nul 2>&1 && (set PY=py) || (set PY=python)
echo ===== %date% %time% ===== >> booking.log
%PY% book.py --config config.toml %* >> booking.log 2>&1
