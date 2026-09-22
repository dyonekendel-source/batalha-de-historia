# Batalha de História — MVP funcional

## O que esta versão corrige
- O banco `questions.json` está ligado ao servidor.
- Os 30 temas aparecem no painel do professor.
- Cada partida sorteia 10 questões do tema escolhido.
- As alternativas são embaralhadas e a resposta correta é recalculada.
- QR Code usa o endereço real do computador na rede.
- Professor e alunos se comunicam em tempo real por WebSocket.

## Como iniciar no Windows
1. Instale o Python 3.10 ou superior.
2. Dê duplo clique em `INICIAR_WINDOWS.bat`.
3. Abra no computador do professor: `http://localhost:3000/teacher.html`.
4. Escolha o tema e clique em **Criar sala**.
5. Para os celulares funcionarem, computador e celulares devem estar na mesma rede Wi-Fi.
6. No computador, abra o jogo pelo IP do computador, por exemplo `http://192.168.0.10:3000/teacher.html`, quando precisar que o QR Code use esse IP.

## Importante
Não abra `index.html` com duplo clique pelo Explorador de Arquivos. O jogo precisa do servidor para carregar as perguntas e sincronizar os jogadores.

## Teste rápido
- Abra `teacher.html` no computador.
- Escolha **Pré-História**.
- Crie a sala.
- Entre com dois celulares usando o código/QR Code.
- Clique em **Iniciar partida**.
- Responda nos celulares.
- Confira a pontuação e o ranking.

## Estrutura
- `server.py` — servidor e lógica da partida.
- `questions.json` — banco completo de 900 questões.
- `public/teacher.html` — painel do professor/projetor.
- `public/student.html` — tela dos alunos.
- `public/index.html` — página inicial.
