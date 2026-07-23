# CLAUDE.md — Ikariam Divulgação (Bot Faker)

> Guia de contexto para trabalhar neste projeto. **Missão atual: corrigir 2 bugs**
> descritos na seção [BUGS A CORRIGIR](#bugs-a-corrigir). Leia esse guia inteiro
> antes de tocar no código — a lógica de envio/rotação/contagem é sutil.

---

## 1. O que é o projeto

Bot de automação Selenium que divulga mensagens no jogo **Ikariam**
(`https://br.ikariam.gameforge.com`). Reconstrução em SQLite de um `.exe`
original que usava MongoDB (ver `README.md`). Painel local em Flask exibido numa
janela desktop PyWebView.

**Fluxo de negócio pretendido:**

1. Bot entra num servidor do Ikariam.
2. Manda mensagem de divulgação para os **primeiros 25 membros** (via ranking/highscore).
3. **Troca de servidor**, manda para 25 do próximo, e assim por diante.
4. Rotação **circular**: ao voltar a um servidor já visitado, deve consultar o
   banco (`users_send`) / Outbox do jogo para **não repetir** ninguém, mandar
   para os **próximos 25** e trocar de novo.
5. Objetivo: cobrir todos os membros de todos os servidores **sem repetir nem
   deixar ninguém de fora**.

---

## 2. Como rodar (dev, Linux)

```bash
cd /home/fresh/projects/Ikariam/Ikariam-divulgacao
python3 -m venv .venv                      # já criada
.venv/bin/pip install -r requirements-dev.txt

# Painel web (sem janela desktop PyWebView — ideal p/ dev Linux):
BOT_NO_WEBVIEW=1 .venv/bin/python main.py   # abre http://127.0.0.1:<porta>

# Testes (97 passam hoje):
BOT_DATA_DIR=$(mktemp -d) .venv/bin/python -m pytest -q
```

- `main.py` inicia Flask (`web_app.py`) numa thread daemon + abre PyWebView.
  Em Windows é desktop; o `README.md` tem instruções Windows (`.venv312\Scripts`).
- **`BOT_DATA_DIR`**: aponta banco SQLite p/ pasta isolada (usar SEMPRE em teste).
- Banco padrão: `data/novo2.sqlite3` (criado se não existe). `.gitignore` já
  ignora `data/`, `*.sqlite3`, `.venv/`.
- `BOT_INSTANCE_ID` / `INSTANCE_ID`: isola dados por instância (multi-conta).

### Conta de teste no jogo

- Login Ikariam: `gildacioj399@gmail.com` / senha `!Gigante399`
- URL: `https://br.ikariam.gameforge.com`
- ⚠️ Login real dispara Selenium/Chrome e pode gerar cooldown/captcha. Preferir
  `dry_run` e os testes para validar lógica antes de rodar contra o jogo real.

---

## 3. Arquitetura / arquivos

| Arquivo                   | Papel                                                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `main.py`                 | Entrypoint desktop + Flask embutido.                                                                                   |
| `web_app.py` (859 ln)     | Painel Flask, orquestração de workers/threads, watchdog, progresso, hotkey F8.                                         |
| `src/start.py` (3102 ln)  | **Coração**: `BotDriver` — automação Selenium, loop de rotação de servidores, envio, contagem. É onde estão os 2 bugs. |
| `src/storage.py` (759 ln) | Persistência SQLite. Models: `Accounts`, `Servers`, `UsersSend`, `Settings`, `RuntimeLog`.                             |
| `src/audit_log.py`        | `DetailedAuditLog` — log JSONL opcional (`save_detailed_logs`).                                                        |
| `src/Frames/config.py`    | Config auxiliar.                                                                                                       |
| `templates/`              | UI web (`base.html`, `index.html`).                                                                                    |
| `tests/`                  | pytest — regressões de envio, storage, web/progress, audit, entrypoint.                                                |

### Modelo de dados (`storage.py`)

- **`servers`**: um por (instance, server, flag). Campo **`message_send`** =
  contador de enviados exibido como "Total". `users` = total de membros do servidor.
- **`users_send`**: um registro por destinatário. **UNIQUE(instance_id, server_id,
  username_key)**. Coluna `status` ∈ {`reserved`, `sent`, `cooldown`, `ignored`,
  `failed`}. É a "Outbox local" / dedup.
  - `UsersSend.reserve(...)` → insere `status='reserved'` se ainda não existe
    (dedup atômico, `BEGIN IMMEDIATE`). Retorna `None` se já existe (reserved OU sent).
  - `UsersSend.reconcile_sent(...)` → marca `sent` OU importa da Outbox. **NUNCA é chamado.**
  - `UsersSend.import_sent(...)` → só é chamado pelo fallback de `reconcile_sent`. **Morto na prática.**
  - `UsersSend.replace_server_outbox_snapshot(...)` → importa recipients da Outbox. **NUNCA é chamado.**
  - `UsersSend.count_for_server(server_id, status=...)` → contagem.
- **`settings`**: `time_wait`, `post_send_wait`, `dry_run`, `headless`, `save_detailed_logs`, etc.

### Orquestração (`web_app.py`)

- `/bot/start` → `_run_bot_loop` numa thread → 1 **worker por conta** (até `MAX_PARALLEL_DRIVERS`).
- **`worker()`** (`web_app.py:464-545`): cria `BotDriver`, chama **`bot.StartGame(logger)` UMA vez**
  por tentativa de recovery (`DRIVER_RECOVERY_LIMIT=3`).
  - **Regra crítica**: se `StartGame` **retorna normalmente**, `web_app.py:518-519`
    seta `account.status="activate"` e **`break`** → a conta **encerra de vez**.
    Só re-executa `StartGame` se lançar Exception de **driver perdido** (`:524-531`).
    O loop externo (`:550`) nunca reinicia uma conta já finalizada.
    → **Qualquer saída normal do `while` de `StartGame` = conta morta permanente.**
- **Watchdog idle** (`web_app.py:495-514`): se `bot.last_activity_at` fica parado
  `DRIVER_IDLE_TIMEOUT_SECONDS=150s`, chama `bot.close()` e força recovery.
- **Progresso UI** (`set_progress`, `_build_server_summary`): mostra `server.messageSend`
  como "sent" — ou seja, **exibe exatamente o contador inflado do bug 1**.
- Hotkey **F8** e fechamento da janela param tudo via `stop_event`.

---

## 4. BUGS A CORRIGIR

> Ambos os bugs foram investigados por leitura estática. As linhas abaixo foram
> **confirmadas** no código atual. Não há fix aplicado ainda — este é o preparo.

### 🐞 BUG 1 — Contador infla: mostra 390 enviadas, só 235 chegaram (Outbox real)

**Sintoma:** painel/log diz "Total: 390" mas a Outbox do jogo tem só ~235.
O contador **super-conta**: cada tentativa que clica no botão "enviar" é contada
como sucesso, sem confirmar que a mensagem realmente saiu.

**Causa raiz (confirmada):** `src/start.py:2912`

```python
# _sendMessage(...) — dentro do try de clique no submit:
2911        self._sleep(getattr(self, "postSendWait", 1.0))
2912        return "success", "Mensagem enviada; seguindo sem verificacao posterior.", None
```

Esse `return "success"` **incondicional** (logo após `submit_button.click()`)
torna **código morto** todo o bloco de verificação real que vem depois —
`src/start.py:2914-2955`: os `success_xpaths` ("Sua ordem foi executada"), o
`_find_feedback`, o `_page_has_outbox_sent(username)` (`:2936`), a detecção de
`ignore list` (`:2943`) e de `cooldown` (`:2951`). **Nada disso executa.**
Basta o clique acontecer (mesmo que o jogo rejeite por cooldown/rate-limit/lista
de ignorados/erro AJAX) que a função devolve `"success"`.

**Cadeia da contagem:**

- `_sendMessage` retorna `"success"` → tratado em `src/start.py:2654-2663` →
  chama **`_mark_sent`** (`:2655`).
- **`_mark_sent`** (`src/start.py:2670-2708`) incrementa **todos** os contadores
  em `:2682-2687` **sem prova de entrega**:
  ```python
  2682   self.serverGlobal.messageSend += 1   # "Total" exibido + persistido no SQLite
  2683   self.serverGlobal.save()
  2685   self.messageSendCount += 1
  2686   self.totalSentSession += 1
  2687   self._server_cycle_send_count += 1     # controla o lote de 25
  ```
- Não é double-count (incremento é ponto único). É **contagem-sem-confirmação**.

**Por que a sincronização com a Outbox não corrige:** `_sync_outbox_sent_users`
(`src/start.py:~1149-1283`) só **conta** a Outbox e faz
`resolved_total = max(messageSend, database_sent_total, outbox_total)` (`~:1249`).
Como é `max()`, o valor inflado (390) **nunca é corrigido para baixo** mesmo quando
a Outbox real é menor (235). E `reconcile_sent` / `replace_server_outbox_snapshot`
(que reconciliariam destinatário-a-destinatário contra a Outbox) **nunca são chamados**.

**Efeito colateral grave (liga com o "não pular ninguém"):** o `messageSend`
inflado é usado como offset de resume/retomada (`_resume_highscore_options` ~`:2415`,
`_current_filter_resume_offset` ~`:2435`, aplicado cortando `users = users[offset:]`).
Contador inflado → bot **pula linhas do ranking que nunca receberam** → viola o
"sem deixar ninguém de fora".

**Direção de fix (avaliar, não aplicado):**

- Remover o `return` prematuro de `:2912` (ou só retornar sucesso após
  `_page_has_outbox_sent`/feedback real), reativando `:2914-2955`.
- Só chamar `_mark_sent` quando o envio for **confirmado** (status `allowed`/`success`
  vindo de verificação real, não do clique).
- Trocar o `max()` de `~:1249` por reconciliação real via `reconcile_sent` /
  `replace_server_outbox_snapshot` usando os recipients reais da Outbox
  (`_extract_current_outbox_recipients`, `src/start.py:~837`, hoje nunca usado).
- Cuidado: `dry_run` retorna `"success"` em `:2897` de propósito (simulação) —
  não confundir com o bug.

---

### 🐞 BUG 2 — Bot para de enviar antes de terminar todos os servidores

**Sintoma:** depois de um tempo, o bot para silenciosamente sem concluir os servidores.

**Mecânica de fundo:** o conjunto **`completed_servers`** (`src/start.py:2017`)
**só cresce, nunca é limpo** (contraste: `current_round_servers.clear()` em `:2025`).
Quando `completed_servers ∪ current_round_servers` cobre todos os cards visíveis,
o `while` quebra em **`src/start.py:2030-2031`** (`break`), `StartGame` retorna
normal, e — pela regra crítica do `web_app.py:518-519` — **a conta morre de vez**.

O problema: **estado transitório é tratado como terminal**. Falhas efêmeras marcam
servidores como "concluídos" para sempre.

**Pontos confirmados (ranking de probabilidade):**

1. **`src/start.py:2059-2070`** — ★ principal. `except Exception` amplo em volta de
   `_run_current_server_flow`: **qualquer** erro transiente (TimeoutException,
   WebDriverException, rede, StaleElement, popup) →

   ```python
   2069   completed_servers.add(current_server)   # PERMANENTE
   2070   continue
   ```

   Erros transientes acumulam → todos os servidores viram "completed" → `break` em `:2030`.

2. **`src/start.py:2082-2085`** — ★ `except BotCooldown`: sai do `while`, dorme **uma vez**
   (30-60s), `StartGame` **retorna normal** → conta encerra sem retomar o envio.
   (`BotCooldown` é lançado em `~:2350` e `:2653`.)

3. **`src/start.py:2080-2081`** — servidor que temporariamente não enviou
   (Outbox falhou, ranking não carregou, todos já reservados) →
   `completed_servers.add` permanente.

4. **`src/start.py:2093-2104`** — `_stale_recovery_count` (`src/start.py:455`)
   **nunca reseta na sessão**. 3 `StaleElementReferenceException` acumulados
   (`STALE_ELEMENT_RECOVERY_LIMIT=3`) → `return` (`:2104`) → "Conta pausada". Casa
   com "depois de um tempo, para".

5. **`src/start.py:2411`** — `raise TimeoutException("highscore range has targets
without confirmed sends")`: 1 faixa de ranking bloqueada → propaga → capturada
   em `:2059` → servidor inteiro vira completed.

6. **`src/start.py:2496-2499` / `~:2343`** — "acabou usuários" calculado com dados
   stale: `max_send = users-1` usando `serverGlobal.users` (pode estar baixo/stale)
   ou `messageSend` inflado (do Bug 1) → conclui "Lista finalizada" cedo → completed falso.
   **Bug 1 alimenta o Bug 2 aqui.**

7. **`web_app.py:518-519`** — não há loop de "voltar ao início quando faltou gente":
   retorno normal de `StartGame` nunca re-processa a conta.

**Direção de fix (avaliar, não aplicado):**

- Distinguir "servidor realmente esgotado" de "servidor com erro transiente":
  não fazer `completed_servers.add` em `:2069` / `:2080` para erros recuperáveis;
  usar retry/backoff por servidor com limite, sem marcar como concluído.
- Resetar `_stale_recovery_count` após progresso bem-sucedido.
- `BotCooldown` (`:2082`): após o sleep, **continuar** o loop em vez de deixar
  `StartGame` retornar; ou re-agendar a conta no `web_app`.
- Corrigir Bug 1 primeiro reduz falsos "Lista finalizada" do ponto 6.

---

## 5. Como validar um fix

1. **Testes**: `BOT_DATA_DIR=$(mktemp -d) .venv/bin/python -m pytest -q` — 97 devem
   continuar passando. Ver especialmente `tests/test_start_regressions.py` (57 KB,
   cobre envio/rotação) e `tests/test_web_progress.py`.
2. **`dry_run`**: ativar em Settings → simula envio sem clicar de verdade; bom p/
   exercitar rotação/contagem/dedup sem tocar no jogo.
3. **Contra o jogo real** (só quando necessário): rodar com a conta de teste,
   comparar "Total" exibido vs. Outbox real do Ikariam. Alvo do Bug 1:
   `Total exibido == mensagens na Outbox`.
4. Existe skill `/verify` — usar para dirigir o fluxo end-to-end após mudança não trivial.

## 6. Convenções

- Python 3.14 no dev. Type hints `from __future__ import annotations` em todo lado.
- Não commitar `data/`, `.venv/`, `*.sqlite3` (já no `.gitignore`).
- Logs de conta passam por `_is_account_log_visible` (prefixos em `web_app.py:89-117`);
  erros ruidosos de Selenium são rebaixados salvo `show_selenium_errors`.
- `instance_id` filtra tudo por instância — não vazar dados entre instâncias.
  </content>
