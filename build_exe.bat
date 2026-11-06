@echo off
rem Builds dist\Distill\Distill.exe (folder build) and dist\Distill-portable.zip.
rem   build_exe.bat           folder build
rem   build_exe.bat onefile   a single Distill.exe (slower to start; unpacks to a temp folder each time)
rem Release builds come from GitHub Actions (.github\workflows\build.yml); this is for local use.
rem Do not publish a build made here if distill\local_keys.py exists: your keys would be built into it.
setlocal
cd /d "%~dp0"

rem libtorrent has no wheel for the newest Pythons, so prefer 3.13 / 3.12 through the "py" launcher.
set "PY="
for %%V in (3.13 3.12) do if not defined PY (py -%%V -c "" >nul 2>nul && set "PY=py -%%V")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (echo Python 3.12 or 3.13 is required - install it from python.org & exit /b 1)
if not exist .venv-build (%PY% -m venv .venv-build || exit /b 1)
call .venv-build\Scripts\activate.bat || exit /b 1
python -m pip install --quiet --upgrade pip || exit /b 1
python -m pip install --quiet -r requirements-dev.txt || exit /b 1

echo.
echo === Tests ===
rem The window tests (test_ui_smoke, test_motion) can crash or fail on some Windows setups (pixel/timing checks) and would block every build.
set DISTILL_SKIP_UI_TESTS=1
python -m unittest discover -s tests -t . || (echo Tests failed - not building & exit /b 1)

echo.
echo === Building ===
if /i "%1"=="onefile" set DISTILL_ONEFILE=1
pyinstaller --noconfirm --clean Distill.spec || exit /b 1

echo.
echo === Checking the build starts ===
if /i "%1"=="onefile" (set "EXE=dist\Distill.exe") else (set "EXE=dist\Distill\Distill.exe")
if exist selftest.txt del selftest.txt
start /wait "" "%EXE%" --self-test "%CD%\selftest.txt"
set "RC=%ERRORLEVEL%"
if exist selftest.txt type selftest.txt
if not "%RC%"=="0" (echo The built program failed its self-test & exit /b 1)

if /i "%1"=="onefile" (
    echo.
    echo Done: dist\Distill.exe
) else (
    powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\Distill\*' -DestinationPath 'dist\Distill-portable.zip'" || exit /b 1
    echo.
    echo Done: dist\Distill\Distill.exe  and  dist\Distill-portable.zip
)
endlocal
