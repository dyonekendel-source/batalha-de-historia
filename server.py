import base64, io, json, os, random, string, time, uuid
from pathlib import Path

import qrcode
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
CONTRIB_PATH = ROOT / "contribuicoes_pendentes.json"

# ---------------------------------------------------------------------------
# Firestore (opcional): se as credenciais do Firebase estiverem configuradas
# (via variável de ambiente), a fila de revisão de contribuições dos
# professores é salva lá, o que sobrevive a reinícios/redeploys do Render.
# Sem credenciais configuradas, cai automaticamente pro arquivo JSON local
# (contribuicoes_pendentes.json) — nada quebra enquanto o Firebase não está
# pronto.
# ---------------------------------------------------------------------------
_firestore_db = None
_firestore_checked = False


def get_firestore_db():
    global _firestore_db, _firestore_checked
    if _firestore_checked:
        return _firestore_db
    _firestore_checked = True
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
        cred_path = os.environ.get("FIREBASE_CREDENTIALS_PATH")

        if not firebase_admin._apps:
            if cred_path and Path(cred_path).exists():
                cred = credentials.Certificate(cred_path)
            elif cred_json:
                cred = credentials.Certificate(json.loads(cred_json))
            else:
                return None  # Firebase ainda não configurado
            firebase_admin.initialize_app(cred)

        _firestore_db = firestore.client()
    except Exception as e:
        print("Firestore indisponível, usando arquivo local:", e)
        _firestore_db = None
    return _firestore_db

# Mesma lista de disciplinas usada no site principal (Estudativa), pra manter
# a taxonomia consistente entre o quiz do site e as perguntas criadas aqui.
DISCIPLINAS = [
    "historia", "portugues", "matematica", "geografia", "ciencias",
    "quimica", "fisica", "biologia", "filosofia", "sociologia", "outros",
]

app = FastAPI(title="Batalha de História")
app.mount("/static", StaticFiles(directory=ROOT / "public"), name="static")
rooms = {}


def shuffle_questions(questions):
    """Embaralha as alternativas de cada questão e calcula o índice da
    correta. Usado tanto pros temas do banco quanto pras perguntas que um
    professor cria na hora."""
    result = []
    for q in questions:
        options = list(q["options"])
        random.shuffle(options)
        result.append({
            **q,
            "options": options,
            "correctIndex": options.index(q["correct_answer"]),
        })
    return result


def pick(topic):
    if len(topic["questions"]) < 10:
        raise ValueError("Este tema ainda não possui 10 questões válidas.")
    selected = random.sample(topic["questions"], 10)
    return shuffle_questions(selected)


def load_contribuicoes():
    db = get_firestore_db()
    if db is not None:
        docs = db.collection("contribuicoes_pendentes").stream()
        items = [d.to_dict() for d in docs]
        # Mais recentes primeiro (criadoEm é ISO 8601, então a ordenação
        # alfabética já corresponde à ordenação cronológica).
        items.sort(key=lambda x: x.get("criadoEm", ""), reverse=True)
        return items

    if not CONTRIB_PATH.exists():
        return []
    try:
        return json.loads(CONTRIB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_contribuicao(disciplina, tema, professor, perguntas):
    entry = {
        "id": uuid.uuid4().hex[:8],
        "disciplina": disciplina,
        "tema": tema,
        "professor": professor or "",
        "criadoEm": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "perguntas": perguntas,
    }

    db = get_firestore_db()
    if db is not None:
        db.collection("contribuicoes_pendentes").document(entry["id"]).set(entry)
        return entry

    items = load_contribuicoes()
    items.append(entry)
    CONTRIB_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def validate_custom_perguntas(perguntas):
    if not isinstance(perguntas, list) or len(perguntas) != 10:
        raise ValueError("Envie exatamente 10 perguntas.")
    cleaned = []
    for p in perguntas:
        question = str(p.get("question", "")).strip()
        options = [str(o).strip() for o in p.get("options", []) if str(o).strip()]
        correct = str(p.get("correct_answer", "")).strip()
        if not question or len(options) < 4 or not correct:
            raise ValueError("Cada pergunta precisa de enunciado, 4 alternativas e uma marcada como certa.")
        if correct not in options:
            raise ValueError("A alternativa marcada como certa precisa ser idêntica a uma das opções.")
        if len(set(options)) != len(options):
            raise ValueError("As alternativas de uma mesma pergunta não podem se repetir.")
        cleaned.append({"question": question, "options": options, "correct_answer": correct})
    return cleaned


def make_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if code not in rooms:
            return code


def ranking(room):
    return sorted(
        [
            {"name": p["name"], "score": p["score"], "answered": p["answered"]}
            for p in room["players"].values()
        ],
        key=lambda x: x["score"],
        reverse=True,
    )


def podium(room):
    return ranking(room)[:3]


@app.get("/")
def home():
    return FileResponse(ROOT / "public/index.html")


@app.get("/teacher.html")
def teacher():
    return FileResponse(ROOT / "public/teacher.html")


@app.get("/student.html")
def student():
    return FileResponse(ROOT / "public/student.html")


@app.get("/api/topics")
def topics():
    return JSONResponse([
        {
            "id": t["id"],
            "disciplina": t.get("disciplina", "historia"),
            "name": t["name"],
            "category": t.get("category"),
            "grade": t.get("grade"),
            "question_count": t["question_count"],
        }
        for t in DATA["topics"]
        if str(t.get("name", "")).strip() != "Revoluções Atlânticas" and t.get("question_count", 0) >= 1
    ])


@app.get("/revisar.html")
def revisar():
    return FileResponse(ROOT / "public/revisar.html")


@app.get("/api/disciplinas")
def disciplinas():
    return JSONResponse(DISCIPLINAS)


@app.get("/api/contribuicoes")
def contribuicoes():
    # Lista tudo que professores criaram na hora, ainda não revisado.
    # Sem autenticação (o app inteiro não tem login) — não exponha esse link publicamente.
    return JSONResponse(load_contribuicoes())


@app.get("/api/qr")
def qr(request: Request, room: str):
    host = request.headers.get("host", "localhost:3000")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    url = f"{proto}://{host}/student.html?room={room.upper()}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return PlainTextResponse(base64.b64encode(buf.getvalue()).decode())


async def send(ws, message):
    try:
        await ws.send_json(message)
    except Exception:
        pass


def teacher_state(room):
    q = room["questions"][room["current"]] if room["current"] >= 0 else None
    question = None
    result = None

    if room["phase"] == "question" and q:
        question = {
            "question": q["question"],
            "options": q["options"],
            "difficulty": q.get("difficulty"),
            "subtopic": q.get("subtopic"),
        }

    if room["phase"] == "result" and q:
        result = {
            "correctIndex": q["correctIndex"],
            "correctAnswer": q["correct_answer"],
            "answeredCount": sum(
                1 for p in room["players"].values() if p["last_answered"]
            ),
        }

    return {
        "type": "state",
        "room": room["id"],
        "topic": room["topic"]["name"],
        "phase": room["phase"],
        "questionNumber": room["current"] + 1,
        "total": 10,
        "question": question,
        "result": result,
        "players": ranking(room),
        "podium": podium(room),
    }


def student_state(room, player):
    q = room["questions"][room["current"]] if room["current"] >= 0 else None
    question = None
    result = None

    if room["phase"] == "question" and q:
        question = {
            "question": q["question"],
            "options": q["options"],
        }

    if room["phase"] == "result" and q:
        result = {
            "correctIndex": q["correctIndex"],
            "correctAnswer": q["correct_answer"],
            "answeredCount": sum(
                1 for p in room["players"].values() if p["last_answered"]
            ),
            "yourAnswer": player.get("last_answer"),
            "correct": player.get("last_correct"),
        }

    return {
        "type": "state",
        "phase": room["phase"],
        "questionNumber": room["current"] + 1,
        "total": 10,
        "question": question,
        "result": result,
        "score": player["score"],
        "answered": player["answered"],
        "podium": podium(room),
    }


async def broadcast(room):
    if room.get("teacher"):
        await send(room["teacher"], teacher_state(room))
    for player in list(room["players"].values()):
        await send(player["ws"], student_state(room, player))


async def start_question(room):
    room["phase"] = "question"
    room["current"] += 1
    for p in room["players"].values():
        p["answered"] = False
        p["answer"] = None
        p["last_answered"] = False
        p["last_answer"] = None
        p["last_correct"] = None
    await broadcast(room)


async def finish_question(room):
    if room["phase"] != "question":
        return

    question = room["questions"][room["current"]]
    room["phase"] = "result"

    for p in room["players"].values():
        p["last_answered"] = p["answered"]
        p["last_answer"] = p["answer"]
        p["last_correct"] = (
            bool(p["answered"]) and p["answer"] == question["correctIndex"]
        )
        if p["last_correct"]:
            p["score"] += 1000
        p["answered"] = False

    await broadcast(room)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    role = None
    room = None
    player_id = None

    try:
        while True:
            message = await ws.receive_json()
            action = message.get("action")

            if action == "create":
                topic_id = str(message.get("topicId", "")).strip()
                topic = next(
                    (t for t in DATA["topics"] if str(t["id"]) == topic_id),
                    DATA["topics"][0],
                )

                if len(topic["questions"]) < 10:
                    await send(ws, {"type": "error", "message": "Este tema ainda não possui 10 questões válidas para iniciar uma partida."})
                    continue

                room_id = make_code()
                room = {
                    "id": room_id,
                    "topic": topic,
                    "teacher": ws,
                    "players": {},
                    "questions": pick(topic),
                    "current": -1,
                    "phase": "lobby",
                }
                rooms[room_id] = room
                role = "teacher"
                await send(ws, {
                    "type": "created",
                    "room": room_id,
                    "topic": topic["name"],
                })
                await broadcast(room)

            elif action == "create_custom":
                # A disciplina pode ser uma das já conhecidas (DISCIPLINAS) ou uma
                # matéria nova digitada pelo professor (ex.: "Projeto de Vida") —
                # nos dois casos ela só precisa ter um nome válido.
                disciplina = str(message.get("disciplina", "")).strip()[:60]
                tema = str(message.get("tema", "")).strip()
                professor = str(message.get("professor", "")).strip()

                if len(disciplina) < 2 or not tema:
                    await send(ws, {"type": "error", "message": "Informe o nome da disciplina (mínimo 2 letras) e o tema."})
                    continue

                try:
                    perguntas = validate_custom_perguntas(message.get("perguntas"))
                except ValueError as e:
                    await send(ws, {"type": "error", "message": str(e)})
                    continue

                # Salva pra fila de revisão (não entra automaticamente no acervo oficial).
                save_contribuicao(disciplina, tema, professor, perguntas)

                room_id = make_code()
                room = {
                    "id": room_id,
                    "topic": {"id": "custom", "name": f"{tema} (personalizado)"},
                    "teacher": ws,
                    "players": {},
                    "questions": shuffle_questions(perguntas),
                    "current": -1,
                    "phase": "lobby",
                }
                rooms[room_id] = room
                role = "teacher"
                await send(ws, {
                    "type": "created",
                    "room": room_id,
                    "topic": room["topic"]["name"],
                })
                await broadcast(room)

            elif action == "join":
                room_id = str(message.get("room", "")).upper()
                target = rooms.get(room_id)

                if not target:
                    await send(ws, {"type": "error", "message": "Sala não encontrada."})
                    continue
                if target["phase"] != "lobby":
                    await send(ws, {"type": "error", "message": "A partida já começou."})
                    continue

                player_id = str(id(ws))
                target["players"][player_id] = {
                    "name": str(message.get("name", "Aluno")).strip()[:24] or "Aluno",
                    "score": 0,
                    "answered": False,
                    "answer": None,
                    "last_answered": False,
                    "last_answer": None,
                    "last_correct": None,
                    "ws": ws,
                }
                room = target
                role = "student"
                await send(ws, {"type": "joined", "room": room_id})
                await broadcast(room)

            elif (
                action == "start"
                and role == "teacher"
                and room
                and room["phase"] == "lobby"
                and room["players"]
            ):
                await start_question(room)

            elif action == "answer" and role == "student" and room:
                player = room["players"].get(player_id)
                if player and room["phase"] == "question" and not player["answered"]:
                    try:
                        index = int(message.get("index"))
                    except (TypeError, ValueError):
                        continue
                    option_count = len(room["questions"][room["current"]]["options"])
                    if 0 <= index < option_count:
                        player["answer"] = index
                        player["answered"] = True
                        await broadcast(room)

            elif action == "finish" and role == "teacher" and room:
                await finish_question(room)

            elif (
                action == "next"
                and role == "teacher"
                and room
                and room["phase"] == "result"
            ):
                if room["current"] >= 9:
                    room["phase"] = "podium"
                    await broadcast(room)
                else:
                    await start_question(room)

            elif (
                action == "finalize"
                and role == "teacher"
                and room
                and room["phase"] == "podium"
            ):
                room["phase"] = "final"
                await broadcast(room)

    except WebSocketDisconnect:
        if role == "student" and room and player_id in room["players"]:
            del room["players"][player_id]
            await broadcast(room)
        elif role == "teacher" and room:
            room["teacher"] = None
