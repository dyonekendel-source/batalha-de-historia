import base64, io, json, random, string
from pathlib import Path

import qrcode
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))

app = FastAPI(title="Batalha de História")
app.mount("/static", StaticFiles(directory=ROOT / "public"), name="static")
rooms = {}


def pick(topic):
    if len(topic["questions"]) < 10:
        raise ValueError("Este tema ainda não possui 10 questões válidas.")
    selected = random.sample(topic["questions"], 10)
    result = []
    for q in selected:
        options = list(q["options"])
        random.shuffle(options)
        result.append({
            **q,
            "options": options,
            "correctIndex": options.index(q["correct_answer"]),
        })
    return result


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
        {"id": t["id"], "name": t["name"], "question_count": t["question_count"]}
        for t in DATA["topics"]
    ])


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
