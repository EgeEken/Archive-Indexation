@echo off
setlocal

rem The folder containing this batch file is the source folder.
set "SOURCE=%~dp0"
set "ENGINE_DIR=C:\Users\EGE\Desktop\Coding\Personal Projects\Completed Projects\Archive Indexation\GPT6-Astra-Testing\Archive-Indexation\Early Testing"
set "ARCHIVE_ROOT=C:\Users\EGE\Desktop\Sony Kamera Arşivi"

rem A copy kept at the archive root asks for a specific all-jpgs folder.
if /I "%SOURCE:~0,-1%"=="%ARCHIVE_ROOT%" (
    echo This batch file is stored at the archive root.
    set /p "SOURCE=Enter the full path of the all-jpgs folder to review: "
    if not defined SOURCE exit /b 1
)

if not exist "%ENGINE_DIR%\select_here.py" (
    echo Could not find the Early Testing engine:
    echo %ENGINE_DIR%
    echo Edit ENGINE_DIR in this batch file if the repository was moved.
    pause
    exit /b 1
)

if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
    set "PYTHON=%LocalAppData%\Programs\Python\Python313\python.exe"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYTHON=python"
    ) else (
        where py >nul 2>nul
        if %errorlevel%==0 (
            set "PYTHON=py -3"
        ) else (
            echo Python was not found on PATH.
            pause
            exit /b 1
        )
    )
)

echo Reviewing JPEGs in:
echo %SOURCE%
echo.
pushd "%SOURCE%"
%PYTHON% "%ENGINE_DIR%\select_here.py" --method handcrafted --ratio 0.10
set "RESULT=%errorlevel%"
popd

if not "%RESULT%"=="0" (
    echo.
    echo Selection failed with exit code %RESULT%.
    pause
    exit /b %RESULT%
)
exit /b 0
