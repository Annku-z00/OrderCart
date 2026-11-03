@echo off
REM ===========================================================================
REM  OrderCart - one-click PyInstaller build (PyWebview matte-dark dashboard)
REM
REM  Produces:  dist\OrderCart\OrderCart.exe   (--onedir, no console window)
REM             plus dist\OrderCart\_internal\ (bundled ui\, app.ico)
REM
REM  NOTE: config.yaml is deliberately NOT bundled - it holds the WAHA
REM  api_key.  The app starts on the built-in defaults and writes config.yaml
REM  in its own folder the first time the Settings view is saved.
REM
REM  Run this from the project folder:   build.bat
REM ===========================================================================

setlocal

set APP_NAME=OrderCart
set ENTRY=main_web.py

echo.
echo === OrderCart build (PyWebview UI) ===
echo.

REM --- 1. make sure the build tools are present -----------------------------
echo [1/6] Checking build dependencies...
REM OrderCart needs Python 3.12 + reportlab (Anaconda).  The Windows Store
REM ``python`` on PATH is often 3.10 and lacks reportlab, so prefer py -3.12.
set "PY=py -3.12"
py -3.12 --version >nul 2>&1
if errorlevel 1 set "PY=python"

REM Put Anaconda's DLL folders on PATH so PyInstaller can resolve libexpat,
REM liblzma, ffi, tcl/tk and the other native dependencies of the bundle.
for /f "delims=" %%i in ('%PY% -c "import sys,os;print(os.path.dirname(sys.executable))"') do set "PY_ROOT=%%i"
if defined PY_ROOT set "PATH=%PY_ROOT%;%PY_ROOT%\Library\bin;%PY_ROOT%\Library\mingw-w64\bin;%PY_ROOT%\DLLs;%PY_ROOT%\Scripts;%PATH%"

%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo      PyInstaller not found - installing requirements...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)
%PY% -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo      pywebview not found - installing...
    %PY% -m pip install pywebview
    if errorlevel 1 goto :fail
)

REM --- 2. clean previous build output ---------------------------------------
echo [2/6] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if not exist %APP_NAME%.spec (
    echo      %APP_NAME%.spec missing - cannot build.
    goto :fail
)

REM --- 3. bundle the UI into flags ------------------------------------------
echo [3/6] Preparing bundled data...
if not exist app.ico (
    echo      app.ico missing - building it from ui\logo.png...
    %PY% make_icon.py
    if errorlevel 1 goto :fail
)
REM Stamp the commit into build_info.json so the in-app update check
REM (updater.py) can tell an installed build from the current GitHub head.
%PY% make_build_info.py
if errorlevel 1 goto :fail

REM --- 4. run PyInstaller ----------------------------------------------------
echo [4/6] Running PyInstaller (this can take a few minutes)...
REM OrderCart.spec holds the hidden imports, the matte UI data and the
REM excludes needed for a clean Anaconda build (and the recursion-limit fix).
%PY% -m PyInstaller --noconfirm --clean %APP_NAME%.spec
if errorlevel 1 goto :fail

REM --- 5. ship data files next to the exe -------------------------------------
echo [5/6] Copying support files next to the executable...
REM The exe lives in dist\%APP_NAME%\ - that folder is the app root at runtime,
REM so config.example.yaml / app.ico / build_info.json must land there.
if exist config.example.yaml copy /y config.example.yaml "dist\%APP_NAME%\" >nul
if exist app.ico copy /y app.ico "dist\%APP_NAME%\" >nul 2>&1
if exist build_info.json copy /y build_info.json "dist\%APP_NAME%\" >nul 2>&1

REM --- 6. done ---------------------------------------------------------------
echo.
echo === Build complete ===
echo.
echo   Executable : dist\%APP_NAME%\%APP_NAME%.exe
echo   Bundle dir : dist\%APP_NAME%\_internal\  (ui\ HTML/CSS/JS is bundled here)
echo.
echo Ship the whole dist\OrderCart\ folder (exe + _internal).
echo A config.yaml next to the exe is used when present; otherwise the app
echo starts on the built-in defaults and writes one when Settings is saved.
echo First launch needs internet: OCR runs on Google Lens (the client is built in).
echo.
echo Push the release with:  git tag vX.Y.Z ^&^& git push origin main --tags
echo.

endlocal
exit /b 0

:fail
echo.
echo *** BUILD FAILED ***
echo Check the messages above. If a dependency is missing, run:
echo     py -3.12 -m pip install -r requirements.txt
echo.
endlocal
exit /b 1
