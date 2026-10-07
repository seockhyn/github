@echo off
rem Windows 작업 스케줄러에서 실행할 배치 파일 (아부다비 자정 10분 전에 실행되도록 등록)
cd /d %~dp0
python book.py --config config.toml >> booking.log 2>&1
