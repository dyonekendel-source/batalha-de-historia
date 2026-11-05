"""
Robô Curador do Estudativa
==========================

Uma vez por semana (terça, 19h de Brasília — disparado pelo GitHub Actions,
veja .github/workflows/curador-semanal.yml), o robô:

1. Junta os dados da semana que o servidor já guarda:
   - partidas criadas (jogos_partidas) e o desempenho por questão
     (resultados_partidas — acertos/erros, anônimo);
   - perguntas que os alunos fizeram à Estudativa IA (ia_perguntas);
   - pendências do painel (mapas mentais e perguntas de professores);
   - lacunas do banco de questões (temas com poucas questões ou nunca jogados).
2. Pede à Estudativa IA (Groq) sugestões de conteúdo novo, com o motivo de
   cada uma. Se a Groq falhar ou não estiver configurada, monta sugestões
   por regras simples — o relatório sai do mesmo jeito.
3. Salva o relatório (relatorios_curador) e envia por Telegram e por e-mail.

Nada aqui publica conteúdo no site: o robô só observa e sugere.

Variáveis de ambiente (todas no Render, nunca no GitHub):
  CURADOR_TOKEN        senha que o agendador usa para chamar /api/curador/rodar
  TELEGRAM_BOT_TOKEN   chave do bot (dada pelo @BotFather)
  TELEGRAM_CHAT_ID     seu chat com o bot (o painel ajuda a descobrir)
  RESEND_API_KEY       chave do Resend (envio de e-mail por API)
  CURADOR_EMAIL_PARA   e-mail que recebe o relatório
  CURADOR_EMAIL_DE     (opcional) remetente; padrão: onboarding@resend.dev
  GROQ_API_KEY         já existe — é a mesma da Estudativa IA

Por que Resend e não Gmail direto? O plano gratuito do Render bloqueia as
portas de e-mail (SMTP). O Resend envia por HTTPS, que funciona normalmente.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter, defaultdict

import httpx

FMT = "%Y-%m-%dT%H:%M:%S"
BRASILIA_OFFSET = -3 * 3600  # o Brasil não tem horário de verão desde 2019

TIPOS_EMOJI = {
    "quiz": "🎯",
    "mapa mental": "🧠",
    "vídeo curto": "🎬",
    "video curto": "🎬",
    "jogo": "🎮",
    "resumo ativo": "📘",
    "revisão": "🔁",
    "revisao": "🔁",
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _ts(segundos_atras: float) -> str:
    # Mesmo formato e mesmo relógio usados por save_record() em server.py.
    return time.strftime(FMT, time.localtime(time.time() - segundos_atras))


def _data_br(epoch: float) -> str:
    return time.strftime("%d/%m", time.gmtime(epoch + BRASILIA_OFFSET))


def _no_periodo(item: dict, inicio: str, fim: str | None = None) -> bool:
    c = item.get("criadoEm", "")
    return c >= inicio and (fim is None or c < fim)


def _pct(parte: int, total: int) -> int | None:
    return round(100 * parte / total) if total else None


def _emoji(tipo: str) -> str:
    return TIPOS_EMOJI.get((tipo or "").strip().lower(), "💡")


# ---------------------------------------------------------------------------
# 1. Coleta e análise
# ---------------------------------------------------------------------------
def coletar_dados(load_records, topics, dias: int = 7) -> dict:
    agora = time.time()
    inicio = _ts(dias * 86400)
    inicio_anterior = _ts(2 * dias * 86400)

    partidas_todas = load_records("jogos_partidas")
    partidas = [p for p in partidas_todas if _no_periodo(p, inicio)]
    partidas_anterior = [p for p in partidas_todas if _no_periodo(p, inicio_anterior, inicio)]

    resultados = [r for r in load_records("resultados_partidas") if _no_periodo(r, inicio)]

    ia_todas = [i for i in load_records("ia_perguntas") if _no_periodo(i, inicio)]
    alertas = sum(1 for i in ia_todas if i.get("alerta"))
    ia = [i for i in ia_todas if not i.get("alerta")]

    # --- desempenho por questão e por tema --------------------------------
    por_questao: dict[str, dict] = {}
    por_tema: dict[str, dict] = defaultdict(lambda: {"respostas": 0, "acertos": 0, "partidas": 0, "disciplina": ""})
    total_respostas = total_acertos = 0
    for r in resultados:
        tema = r.get("tema") or "?"
        t = por_tema[tema]
        t["partidas"] += 1
        t["disciplina"] = r.get("disciplina") or t["disciplina"]
        for q in r.get("questoes") or []:
            resp = int(q.get("responderam") or 0)
            ac = int(q.get("acertaram") or 0)
            total_respostas += resp
            total_acertos += ac
            t["respostas"] += resp
            t["acertos"] += ac
            chave = q.get("questao_id") or q.get("pergunta", "")[:80]
            if not chave:
                continue
            item = por_questao.setdefault(chave, {
                "tema": tema,
                "pergunta": q.get("pergunta", ""),
                "resposta_correta": q.get("resposta_correta", ""),
                "respostas": 0,
                "acertos": 0,
            })
            item["respostas"] += resp
            item["acertos"] += ac

    questoes_dificeis = sorted(
        (
            {**q, "acerto_pct": _pct(q["acertos"], q["respostas"])}
            for q in por_questao.values()
            if q["respostas"] >= 3 and q["acertos"] / q["respostas"] < 0.5
        ),
        key=lambda q: (q["acerto_pct"], -q["respostas"]),
    )[:8]

    temas_desempenho = sorted(
        (
            {"tema": nome, **v, "acerto_pct": _pct(v["acertos"], v["respostas"])}
            for nome, v in por_tema.items()
            if v["respostas"] > 0
        ),
        key=lambda t: t["acerto_pct"],
    )
    temas_faceis = [t for t in temas_desempenho if t["respostas"] >= 20 and t["acerto_pct"] >= 90]

    # --- temas mais jogados -------------------------------------------------
    jogados = Counter(p.get("tema") or "?" for p in partidas)

    # --- lacunas do banco de questões ---------------------------------------
    jogados_30d = {
        p.get("tema") for p in partidas_todas if _no_periodo(p, _ts(30 * 86400))
    }
    sem_questoes = []
    poucas_questoes = []
    nunca_jogados_historia = []
    for t in topics:
        n = len(t.get("questions") or [])
        info = {"tema": t.get("name"), "disciplina": t.get("disciplina", ""), "questoes": n}
        if n < 10:
            sem_questoes.append(info)
        elif n < 25:
            poucas_questoes.append(info)
        if t.get("disciplina") == "historia" and n >= 10 and t.get("name") not in jogados_30d:
            nunca_jogados_historia.append(t.get("name"))

    # --- pendências do painel ------------------------------------------------
    try:
        mapas_pendentes = sum(1 for m in load_records("mapas_mentais") if m.get("status") != "aprovado")
    except Exception:
        mapas_pendentes = 0
    try:
        contribuicoes_pendentes = len(load_records("contribuicoes_pendentes"))
    except Exception:
        contribuicoes_pendentes = 0

    return {
        "periodo": {
            "dias": dias,
            "inicio": _data_br(agora - dias * 86400),
            "fim": _data_br(agora),
        },
        "numeros": {
            "partidas": len(partidas),
            "partidas_semana_anterior": len(partidas_anterior),
            "partidas_com_resultado": len(resultados),
            "respostas_de_alunos": total_respostas,
            "acerto_medio_pct": _pct(total_acertos, total_respostas),
            "perguntas_ia": len(ia),
            "alertas_bem_estar": alertas,
            "mapas_pendentes": mapas_pendentes,
            "contribuicoes_pendentes": contribuicoes_pendentes,
        },
        "temas_mais_jogados": jogados.most_common(8),
        "temas_desempenho": temas_desempenho[:10],
        "temas_faceis": temas_faceis[:5],
        "questoes_dificeis": questoes_dificeis,
        "perguntas_ia": [str(i.get("pergunta", ""))[:220] for i in ia[:80]],
        "lacunas": {
            "sem_questoes_suficientes": sem_questoes[:10],
            "poucas_questoes": sorted(poucas_questoes, key=lambda x: x["questoes"])[:10],
            "historia_sem_partidas_30d": nunca_jogados_historia[:12],
        },
    }


# ---------------------------------------------------------------------------
# 2. Sugestões (IA com plano B)
# ---------------------------------------------------------------------------
PROMPT_SISTEMA = (
    "Você é o Robô Curador do Estudativa, uma plataforma brasileira de jogos, quizzes, "
    "mapas mentais e resumos para estudantes do Ensino Fundamental (6º ao 9º ano), com "
    "foco em História. Seu trabalho é ler os dados de uso da semana e sugerir ao professor "
    "dono do site QUAIS CONTEÚDOS NOVOS ele deve criar, sempre explicando o motivo com base "
    "nos números. Seja concreto e prático; escreva em português do Brasil, em tom direto e "
    "amigável. Nunca invente números: use só os dados recebidos. Se houver poucos dados, "
    "diga isso no resumo e apoie-se nas lacunas do banco de questões e nas perguntas à IA.\n\n"
    "Responda SOMENTE com um JSON neste formato:\n"
    "{\n"
    '  "resumo": "2 ou 3 frases sobre a semana",\n'
    '  "temas_ia": [{"tema": "assunto que os alunos perguntaram à IA", "vezes": 3}],\n'
    '  "sugestoes": [\n'
    "    {\n"
    '      "tipo": "quiz | mapa mental | vídeo curto | jogo | resumo ativo | revisão",\n'
    '      "titulo": "título curto do conteúdo sugerido",\n'
    '      "tema": "tema relacionado",\n'
    '      "prioridade": "alta | média | baixa",\n'
    '      "motivo": "por que, citando o dado",\n'
    '      "ideia": "como seria o conteúdo, em 1 ou 2 frases"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Dê de 3 a 5 sugestões, da mais importante para a menos importante. Em temas_ia, agrupe "
    "as perguntas dos alunos por assunto (no máximo 6 assuntos); deixe a lista vazia se não "
    "houver perguntas."
)


async def sugerir_com_ia(dados: dict, groq_key: str | None, groq_model: str) -> dict | None:
    if not groq_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}"},
                json={
                    "model": groq_model,
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": PROMPT_SISTEMA},
                        {"role": "user", "content": "Dados da semana:\n" + json.dumps(dados, ensure_ascii=False)},
                    ],
                },
            )
        if resp.status_code != 200:
            print("Robô Curador: Groq respondeu", resp.status_code, resp.text[:300])
            return None
        conteudo = resp.json()["choices"][0]["message"]["content"]
        saida = json.loads(conteudo)
        sugestoes = [s for s in saida.get("sugestoes") or [] if isinstance(s, dict) and s.get("titulo")]
        if not sugestoes:
            return None
        return {
            "resumo": str(saida.get("resumo") or "").strip(),
            "temas_ia": [t for t in saida.get("temas_ia") or [] if isinstance(t, dict) and t.get("tema")][:6],
            "sugestoes": sugestoes[:5],
            "fonte": "ia",
        }
    except Exception as e:
        print("Robô Curador: falha ao pedir sugestões à Groq:", repr(e))
        return None


def sugerir_por_regras(dados: dict) -> dict:
    """Plano B: sugestões montadas só com regras, sem IA."""
    s = []
    temas_usados = set()
    for q in dados["questoes_dificeis"]:
        if q["tema"] in temas_usados:
            continue
        temas_usados.add(q["tema"])
        quem = "Ninguém acertou" if q["acerto_pct"] == 0 else f"Só {q['acerto_pct']}% acertaram"
        s.append({
            "tipo": "mapa mental",
            "titulo": f"Revisão: {q['tema']}",
            "tema": q["tema"],
            "prioridade": "alta",
            "motivo": f"{quem} a questão “{q['pergunta'][:90]}…”.",
            "ideia": "Um mapa mental que deixe clara a ideia central dessa questão, seguido de um quiz curto de revisão.",
        })
        if len(s) == 2:
            break
    for t in dados["lacunas"]["sem_questoes_suficientes"][:2]:
        s.append({
            "tipo": "quiz",
            "titulo": f"Banco de questões: {t['tema']}",
            "tema": t["tema"],
            "prioridade": "alta" if t["disciplina"] == "historia" else "média",
            "motivo": f"O tema tem só {t['questoes']} questões e não dá para montar uma partida (mínimo 10).",
            "ideia": "Criar pelo menos 20 questões para o tema entrar nas partidas.",
        })
    for t in dados["temas_faceis"][:1]:
        s.append({
            "tipo": "quiz",
            "titulo": f"Versão desafio: {t['tema']}",
            "tema": t["tema"],
            "prioridade": "baixa",
            "motivo": f"{t['acerto_pct']}% de acerto: o quiz atual está fácil para a turma.",
            "ideia": "Questões mais interpretativas, com fontes históricas e imagens.",
        })
    if len(s) < 3 and dados["lacunas"]["historia_sem_partidas_30d"]:
        tema = dados["lacunas"]["historia_sem_partidas_30d"][0]
        s.append({
            "tipo": "vídeo curto",
            "titulo": f"Divulgar: {tema}",
            "tema": tema,
            "prioridade": "média",
            "motivo": "Ninguém jogou esse tema nos últimos 30 dias.",
            "ideia": "Um vídeo curto apresentando o tema, com link para o quiz no site.",
        })
    n = dados["numeros"]
    if n["partidas"] == 0:
        resumo = "Nenhuma partida foi registrada nesta semana, então as sugestões se apoiam nas lacunas do banco de questões."
    else:
        resumo = f"Foram {n['partidas']} partidas na semana"
        if n["acerto_medio_pct"] is not None:
            resumo += f", com acerto médio de {n['acerto_medio_pct']}%"
        resumo += "."
    return {"resumo": resumo, "temas_ia": [], "sugestoes": s[:5], "fonte": "regras"}


# ---------------------------------------------------------------------------
# 3. Formatação (Telegram e e-mail)
# ---------------------------------------------------------------------------
def _comparacao(atual: int, anterior: int) -> str:
    if anterior == 0:
        return ""
    diff = atual - anterior
    if diff == 0:
        return " (igual à semana passada)"
    return f" ({'+' if diff > 0 else ''}{diff} vs. semana passada)"


def texto_telegram(rel: dict) -> str:
    e = html.escape
    n = rel["numeros"]
    p = rel["periodo"]
    linhas = [
        f"🤖 <b>Robô Curador — {p['inicio']} a {p['fim']}</b>",
        "",
        f"<i>{e(rel.get('resumo') or '')}</i>",
        "",
        "📊 <b>Números da semana</b>",
        f"• Partidas: {n['partidas']}{_comparacao(n['partidas'], n['partidas_semana_anterior'])}",
    ]
    if n["respostas_de_alunos"]:
        linhas.append(f"• Respostas de alunos: {n['respostas_de_alunos']} (acerto médio {n['acerto_medio_pct']}%)")
    linhas.append(f"• Perguntas à Estudativa IA: {n['perguntas_ia']}")

    if rel.get("questoes_dificeis"):
        q = rel["questoes_dificeis"][0]
        quanto = "ninguém acertou" if q["acerto_pct"] == 0 else f"só {q['acerto_pct']}% de acerto"
        linhas += ["", f"❗ <b>Questão que mais derrubou:</b> {e(q['pergunta'][:140])}… — {quanto} ({e(q['tema'])})"]

    linhas += ["", "💡 <b>Sugestões</b>"]
    for i, s in enumerate(rel.get("sugestoes") or [], 1):
        prio = " 🔥" if str(s.get("prioridade", "")).lower() == "alta" else ""
        linhas.append(f"{i}. {_emoji(s.get('tipo'))} <b>{e(s.get('titulo', ''))}</b>{prio}")
        if s.get("motivo"):
            linhas.append(f"   ↳ {e(s['motivo'])}")

    pend = []
    if n["mapas_pendentes"]:
        pend.append(f"{n['mapas_pendentes']} mapa(s) mental(is) para aprovar")
    if n["contribuicoes_pendentes"]:
        pend.append(f"{n['contribuicoes_pendentes']} lote(s) de perguntas de professores")
    if n["alertas_bem_estar"]:
        pend.append(f"{n['alertas_bem_estar']} alerta(s) de bem-estar na IA — veja no painel")
    if pend:
        linhas += ["", "📌 <b>Pendências</b>"] + [f"• {e(x)}" for x in pend]

    if rel.get("painel_url"):
        linhas += ["", f'🔗 <a href="{e(rel["painel_url"])}">Abrir o relatório completo no painel</a>']
    texto = "\n".join(linhas)
    return texto if len(texto) <= 4000 else texto[:3990] + "…"


def html_email(rel: dict) -> str:
    e = html.escape
    n = rel["numeros"]
    p = rel["periodo"]
    cor = "#1f6feb"

    def card(valor, rotulo):
        return (
            f'<td style="padding:12px;text-align:center;border:1px solid #e3e8ef;border-radius:10px;background:#f7f9fc">'
            f'<div style="font-size:26px;font-weight:800;color:{cor}">{e(str(valor))}</div>'
            f'<div style="font-size:12px;color:#5b6675">{e(rotulo)}</div></td>'
        )

    acerto = f"{n['acerto_medio_pct']}%" if n["acerto_medio_pct"] is not None else "—"
    cards = (
        '<table role="presentation" width="100%" cellspacing="8" style="border-collapse:separate"><tr>'
        + card(n["partidas"], "partidas")
        + card(n["respostas_de_alunos"], "respostas de alunos")
        + card(acerto, "acerto médio")
        + card(n["perguntas_ia"], "perguntas à IA")
        + "</tr></table>"
    )

    sug_html = ""
    for i, s in enumerate(rel.get("sugestoes") or [], 1):
        prio = str(s.get("prioridade", "")).lower()
        cor_prio = {"alta": "#d93025", "média": "#e37400", "media": "#e37400"}.get(prio, "#5b6675")
        sug_html += (
            '<div style="border:1px solid #e3e8ef;border-radius:12px;padding:14px 16px;margin:10px 0">'
            f'<div style="font-size:16px;font-weight:700">{i}. {_emoji(s.get("tipo"))} {e(s.get("titulo", ""))}</div>'
            f'<div style="font-size:12px;color:#5b6675;margin:4px 0 8px">{e(s.get("tipo", ""))} · {e(s.get("tema", ""))} · '
            f'<b style="color:{cor_prio}">prioridade {e(prio or "—")}</b></div>'
            f'<div style="font-size:14px"><b>Por quê:</b> {e(s.get("motivo", ""))}</div>'
            f'<div style="font-size:14px;margin-top:4px"><b>Ideia:</b> {e(s.get("ideia", ""))}</div>'
            "</div>"
        )

    dif_html = ""
    if rel.get("questoes_dificeis"):
        linhas = "".join(
            f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eef1f5">{e(q["pergunta"][:160])}'
            f'<div style="color:#5b6675;font-size:12px">{e(q["tema"])} · resposta: {e(q["resposta_correta"][:80])}</div></td>'
            f'<td style="padding:6px 8px;border-bottom:1px solid #eef1f5;text-align:right;white-space:nowrap;color:#d93025;font-weight:700">'
            f'{q["acerto_pct"]}%</td></tr>'
            for q in rel["questoes_dificeis"][:6]
        )
        dif_html = (
            '<h3 style="margin:24px 0 6px">❗ Questões com mais erros</h3>'
            f'<table width="100%" style="border-collapse:collapse;font-size:14px">{linhas}</table>'
        )

    ia_html = ""
    if rel.get("temas_ia"):
        itens = "".join(f"<li>{e(t['tema'])} ({e(str(t.get('vezes', '')))}x)</li>" for t in rel["temas_ia"])
        ia_html = f'<h3 style="margin:24px 0 6px">🤖 O que os alunos perguntaram à IA</h3><ul style="font-size:14px">{itens}</ul>'

    jog_html = ""
    if rel.get("temas_mais_jogados"):
        itens = "".join(f"<li>{e(t)} — {c} partida(s)</li>" for t, c in rel["temas_mais_jogados"][:6])
        jog_html = f'<h3 style="margin:24px 0 6px">🎮 Temas mais jogados</h3><ul style="font-size:14px">{itens}</ul>'

    pend = []
    if n["mapas_pendentes"]:
        pend.append(f"{n['mapas_pendentes']} mapa(s) mental(is) para aprovar")
    if n["contribuicoes_pendentes"]:
        pend.append(f"{n['contribuicoes_pendentes']} lote(s) de perguntas de professores para revisar")
    if n["alertas_bem_estar"]:
        pend.append(f"{n['alertas_bem_estar']} alerta(s) de bem-estar nas perguntas à IA (veja no painel)")
    pend_html = ""
    if pend:
        pend_html = '<h3 style="margin:24px 0 6px">📌 Pendências</h3><ul style="font-size:14px">' + "".join(f"<li>{e(x)}</li>" for x in pend) + "</ul>"

    botao = ""
    if rel.get("painel_url"):
        botao = (
            f'<p style="margin:28px 0 8px"><a href="{e(rel["painel_url"])}" '
            f'style="background:{cor};color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700">'
            "Abrir no painel</a></p>"
        )

    fonte = "Sugestões geradas pela Estudativa IA." if rel.get("fonte") == "ia" else "Sugestões geradas por regras automáticas (a IA não respondeu desta vez)."

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#1c2430;line-height:1.45">'
        f'<h2 style="margin:0 0 4px">🤖 Robô Curador do Estudativa</h2>'
        f'<div style="color:#5b6675;font-size:13px;margin-bottom:14px">Semana de {p["inicio"]} a {p["fim"]}</div>'
        f'<p style="font-size:15px">{e(rel.get("resumo") or "")}</p>'
        f"{cards}"
        '<h3 style="margin:24px 0 6px">💡 Sugestões de conteúdo</h3>'
        f"{sug_html}{dif_html}{ia_html}{jog_html}{pend_html}{botao}"
        f'<p style="color:#8a94a3;font-size:12px;margin-top:28px">{fonte} O robô só sugere — nada foi publicado no site.</p>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# 4. Envio
# ---------------------------------------------------------------------------
async def enviar_telegram(texto: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"ok": False, "erro": "Telegram não configurado (faltam TELEGRAM_BOT_TOKEN e/ou TELEGRAM_CHAT_ID)."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        if r.status_code == 200 and r.json().get("ok"):
            return {"ok": True}
        return {"ok": False, "erro": f"Telegram respondeu {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao falar com o Telegram: {e!r}"}


async def enviar_email(assunto: str, corpo_html: str, corpo_texto: str) -> dict:
    chave = os.environ.get("RESEND_API_KEY", "").strip()
    para = [x.strip() for x in os.environ.get("CURADOR_EMAIL_PARA", "").split(",") if x.strip()]
    remetente = os.environ.get("CURADOR_EMAIL_DE", "").strip() or "Robô Curador Estudativa <onboarding@resend.dev>"
    if not chave or not para:
        return {"ok": False, "erro": "E-mail não configurado (faltam RESEND_API_KEY e/ou CURADOR_EMAIL_PARA)."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {chave}"},
                json={"from": remetente, "to": para, "subject": assunto, "html": corpo_html, "text": corpo_texto},
            )
        if r.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "erro": f"Resend respondeu {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao falar com o Resend: {e!r}"}


async def descobrir_chats_telegram() -> dict:
    """Ajuda na configuração: lista os chats que mandaram mensagem ao bot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "erro": "Configure primeiro TELEGRAM_BOT_TOKEN no Render."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getUpdates")
        dados = r.json()
        if not dados.get("ok"):
            return {"ok": False, "erro": f"Telegram recusou: {str(dados)[:200]}"}
        chats = {}
        for u in dados.get("result", []):
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                nome = chat.get("first_name") or chat.get("title") or chat.get("username") or ""
                chats[str(chat["id"])] = nome
        return {"ok": True, "chats": [{"chat_id": k, "nome": v} for k, v in chats.items()]}
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao falar com o Telegram: {e!r}"}


# ---------------------------------------------------------------------------
# 5. Tudo junto
# ---------------------------------------------------------------------------
async def gerar_relatorio(*, load_records, topics, groq_key, groq_model, painel_url="", dias=7) -> dict:
    dados = coletar_dados(load_records, topics, dias)
    # A IA recebe os dados sem a lista completa de perguntas de alunos repetida
    # no relatório salvo — ela só volta agrupada por assunto (temas_ia).
    sugestoes = await sugerir_com_ia(dados, groq_key, groq_model) or sugerir_por_regras(dados)
    rel = {k: v for k, v in dados.items() if k != "perguntas_ia"}
    rel.update(sugestoes)
    rel["painel_url"] = painel_url
    return rel


async def enviar_relatorio(rel: dict, telegram=True, email=True) -> dict:
    envio = {}
    if telegram:
        envio["telegram"] = await enviar_telegram(texto_telegram(rel))
    if email:
        assunto = f"🤖 Robô Curador — sugestões da semana ({rel['periodo']['inicio']} a {rel['periodo']['fim']})"
        texto_simples = re.sub(r"<[^>]+>", "", texto_telegram(rel))
        envio["email"] = await enviar_email(assunto, html_email(rel), html.unescape(texto_simples))
    return envio
