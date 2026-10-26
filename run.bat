@echo off
setlocal
chcp 65001 >nul
title PaperFlow Local
cd /d "%~dp0"
if errorlevel 1 goto directory_error
if defined PAPERFLOW_PYTHON goto explicit_python
if not defined CONDA_PREFIX goto local_config
if not exist "%CONDA_PREFIX%\conda-meta" goto local_config
set "PAPERFLOW_PYTHON=%CONDA_PREFIX%\python.exe"
goto explicit_python

:local_config
if not exist ".paperflow-python" goto conda_command
set /p PAPERFLOW_PYTHON=<".paperflow-python"
if defined PAPERFLOW_PYTHON goto explicit_python

:conda_command
where conda >nul 2>nul
if errorlevel 1 goto conda_error
if not defined PAPERFLOW_CONDA_ENV set "PAPERFLOW_CONDA_ENV=pdf_parallel_trans"
call conda run --no-capture-output -n "%PAPERFLOW_CONDA_ENV%" python -X utf8 -m paperflow.launcher
goto finished

:explicit_python
if not exist "%PAPERFLOW_PYTHON%" goto python_error
"%PAPERFLOW_PYTHON%" -X utf8 -m paperflow.launcher

:finished
set "PAPERFLOW_EXIT=%ERRORLEVEL%"
if not "%PAPERFLOW_EXIT%"=="0" (
    echo.
    echo Startup failed. Please review the details above and README.md.
    pause
)
exit /b %PAPERFLOW_EXIT%

:python_error
echo Conda interpreter was not found: "%PAPERFLOW_PYTHON%"
echo Set PAPERFLOW_PYTHON to the python.exe in your project Conda environment.
pause
exit /b 1

:conda_error
echo Conda was not found. Open Anaconda Prompt, activate your environment, and run this file.
echo Or set PAPERFLOW_PYTHON to the python.exe in your project Conda environment.
pause
exit /b 1

:directory_error
echo Could not open the project directory.
pause
exit /b 1
