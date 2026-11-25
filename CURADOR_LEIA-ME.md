# 🤖 Robô Curador do Estudativa — como ligar

Toda **terça às 19h** (Brasília) o robô analisa a semana e manda sugestões de
conteúdo no **Telegram** e no **e-mail**. O histórico fica na aba
**🧭 Robô Curador** do painel do admin (`/admin.html`).

O robô **não gasta nada** além do que o site já usa: roda no Render, com a
mesma Groq gratuita da Estudativa IA. Ele só sugere — nunca publica nada.

---

## 1. Criar o bot do Telegram (2 minutos)

1. No Telegram, procure **@BotFather** e envie `/newbot`.
2. Escolha um nome (ex.: *Robô Curador Estudativa*) e um usuário terminado em `bot`
   (ex.: `curador_estudativa_bot`).
3. O BotFather responde com uma **chave** parecida com `123456789:AAH...`.
   Guarde — ela vai no Render como `TELEGRAM_BOT_TOKEN`.
4. Abra o seu bot novo e envie **/start** (sem isso ele não consegue te mandar mensagens).

## 2. Criar a chave de e-mail no Resend (3 minutos)

O plano gratuito do Render bloqueia o envio de e-mail pelo jeito tradicional
(SMTP), então usamos o **Resend**, que é gratuito (até 3.000 e-mails/mês).

1. Crie a conta em **resend.com** usando **o mesmo e-mail que vai receber o relatório**.
2. Vá em **API Keys → Create API Key** e copie a chave (`re_...`).

> Sem verificar um domínio, o Resend só entrega para o e-mail dono da conta —
> por isso a conta precisa ser criada com o e-mail que recebe o relatório.

## 3. Configurar o Render

No Render: serviço **batalha-de-historia → Environment → Add Environment Variable**:

| Variável | Valor |
|---|---|
| `CURADOR_TOKEN` | uma senha longa inventada por você (ex.: 30 letras e números aleatórios) |
| `TELEGRAM_BOT_TOKEN` | a chave do BotFather |
| `RESEND_API_KEY` | a chave do Resend |
| `CURADOR_EMAIL_PARA` | o e-mail que recebe o relatório |
| `TELEGRAM_CHAT_ID` | veja o passo 4 |

Salve (o Render reinicia o serviço sozinho).

## 4. Descobrir o seu chat do Telegram

1. Abra `/admin.html` → aba **🧭 Robô Curador**.
2. Clique em **🔎 Descobrir meu chat ID do Telegram**.
3. Copie o número que aparecer para a variável `TELEGRAM_CHAT_ID` no Render.

## 5. Ligar o agendamento de terça 19h (GitHub)

No GitHub, no repositório **batalha-de-historia**:
**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Valor |
|---|---|
| `CURADOR_URL` | o endereço do servidor no Render (ex.: `https://batalha-de-historia.onrender.com`), sem barra no final |
| `CURADOR_TOKEN` | **a mesma** senha usada no Render |

## 6. Testar

- No painel, clique em **👀 Gerar prévia** para ver um relatório sem enviar.
- Clique em **📨 Gerar e enviar agora** para testar Telegram e e-mail.
- Para testar o agendamento: GitHub → aba **Actions** → *Robô Curador semanal* → **Run workflow**.

---

### O que o robô analisa

- Partidas criadas e o **desempenho por questão** (quantos responderam e acertaram — anônimo, sem nomes de alunos).
- Perguntas que os alunos fazem à **Estudativa IA**, agrupadas por assunto.
- **Lacunas** do banco de questões (ex.: temas com menos de 10 questões, temas de História sem partidas há 30 dias).
- **Pendências** do painel (mapas mentais e perguntas de professores esperando revisão).

### Importante: histórico permanente

Se o **Firestore** não estiver configurado (veja o item *Histórico permanente*
na aba do robô), os dados ficam em arquivos que o Render apaga a cada
atualização do site. O robô funciona do mesmo jeito, mas só enxerga o que
aconteceu desde a última atualização.
