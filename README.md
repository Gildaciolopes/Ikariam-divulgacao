## Como rodar

```powershell
cd "C:\Users\Administrator\Desktop\Ikariam-divulgacao"
.venv312\Scripts\python.exe main.py
```

O comando abre o painel em uma janela desktop propria.

## Banco

Banco local padrao no executavel: `data\novo2.sqlite3`, na mesma pasta do `.exe`.
Em desenvolvimento/testes, a variavel `BOT_DATA_DIR` pode apontar para uma pasta
isolada. Se o banco local ainda nao existir, o app cria a pasta `data` e um banco
vazio. Nenhum banco em `%APPDATA%\BotDivulgacao` e consultado ou migrado.

## Interface

O executavel inicia o Flask local e exibe o painel em uma janela desktop
PyWebView. Fechar essa janela encerra o aplicativo, os workers Selenium e os
processos Chrome/ChromeDriver controlados pelo bot.

O executavel original `novo 2/Bot.exe` inclui `pymongo` e uma URI MongoDB
embutida. Esta reconstrucao usa SQLite local para evitar depender da credencial
remota e para nao repetir o erro de agregacao do MongoDB.

## Estrutura principal

- `main.py`: entrypoint desktop/web.
- `web_app.py`: painel Flask.
- `src/storage.py`: persistencia SQLite local.
- `src/start.py`: automacao Selenium do Ikariam.
- `templates/`: interface web extraida/reconstruida do `novo 2`.
