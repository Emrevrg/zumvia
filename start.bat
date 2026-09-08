@echo off
REM ============================================================
REM  ZUMVIA - Windows tek tikla baslatma
REM ============================================================
cd /d "%~dp0backend"

if not exist ".venv" (
    echo [1/4] Sanal ortam olusturuluyor...
    python -m venv .venv || goto :error
)

echo [2/4] Bagimliliklar kontrol ediliyor...
call .venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :error

if not exist ".env" (
    echo [3/4] Guvenlik anahtarlari uretiliyor...
    call .venv\Scripts\python.exe -c "import base64,os,secrets;mk=base64.urlsafe_b64encode(os.urandom(32)).decode();js=secrets.token_urlsafe(48);open('.env','w').write('VQ_MASTER_KEY=%%s\nVQ_JWT_SECRET=%%s\nVQ_DATABASE_URL=sqlite:///./zumvia.db\nVQ_LOG_LEVEL=INFO\nVQ_FORCE_PAPER_ONLY=false\n' %% (mk,js))" || goto :error
    echo     .env olusturuldu - bu dosyayi kimseyle paylasmayin.
) else (
    echo [3/4] .env mevcut, atlaniyor.
)

echo [4/4] Sunucu baslatiliyor...
echo.
echo     Tarayicidan acin:  http://localhost:8000
echo     Durdurmak icin:    Ctrl+C
echo.
call .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
goto :eof

:error
echo.
echo HATA: Kurulum tamamlanamadi. Python 3.11+ kurulu mu?
pause
