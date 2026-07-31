@echo off

PATH=C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Scripts;C:\ProgramData\Anaconda3\Library\bin;%PATH%

@echo on  

cd /d "%~dp0"
setlocal enabledelayedexpansion

call python ./src/main.py
