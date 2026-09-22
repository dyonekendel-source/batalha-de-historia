# Batalha de História — publicação no Render

## 1. Coloque esta pasta em um repositório GitHub
A pasta que contém `server.py`, `questions.json`, `requirements.txt` e `render.yaml` deve ser a raiz do repositório.

## 2. No Render
- New → Web Service
- Conecte o repositório GitHub
- O Render pode detectar o `render.yaml`.
- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Plan: Free

## 3. Depois do deploy
O Render fornecerá um endereço `https://...onrender.com`.
Abra esse endereço para o painel inicial. O QR Code da sala usa automaticamente o endereço público do serviço.

## 4. Importante
Não abra `index.html` diretamente. O jogo precisa ser servido pelo FastAPI.

O plano Free pode colocar o serviço em espera após 15 minutos sem tráfego. Ao acessar novamente, ele pode levar cerca de um minuto para iniciar. Durante uma partida com conexões WebSocket ativas, mensagens WebSocket recebidas também contam como atividade. 
