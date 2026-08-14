@echo off
setlocal
set "SCHOLAR_SLIDES_ROOT=%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "SCHOLAR_SLIDES_PYTHON=%SCHOLAR_SLIDES_ROOT%\runtime\.venv\Scripts\python.exe"
if not exist "%SCHOLAR_SLIDES_PYTHON%" (
  echo ERROR: scholar-slides Python runtime is missing: %SCHOLAR_SLIDES_PYTHON% 1>&2
  exit /b 1
)
"%SCHOLAR_SLIDES_PYTHON%" "%SCHOLAR_SLIDES_ROOT%\runtime\scripts\scholar_slides.py" %*
exit /b %ERRORLEVEL%
