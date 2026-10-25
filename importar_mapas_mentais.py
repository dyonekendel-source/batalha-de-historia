"""Importa os mapas mentais oficiais de Geografia (41 fichas, 6º ao 9º ano)
para a coleção "mapas_mentais", já como oficiais e aprovados.

Uso (no mesmo ambiente do server.py, com as mesmas variáveis de ambiente do
Render configuradas se quiser gravar direto no Firestore; sem elas, grava em
mapas_mentais.json local, do jeito que o servidor também usa):

    python3 importar_mapas_mentais.py

Pode ser executado de novo sem duplicar: pula qualquer ficha cujo
disciplina+ano+temaNormalizado já exista na coleção.
"""
import json
import re
import unicodedata
from pathlib import Path

import server  # usa as mesmas funções load_records/save_record/get_firestore_db do servidor

ROOT = Path(__file__).parent
PASTA = ROOT / "dados_mapas_mentais"


def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalizar_tema(s):
    s = _sem_acento(s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    arquivos = sorted(PASTA.glob("g*.json"))
    if not arquivos:
        print("Nenhum arquivo encontrado em", PASTA)
        return

    existentes = server.load_records("mapas_mentais")
    chaves_existentes = {
        (m.get("disciplina"), m.get("ano"), m.get("temaNormalizado"))
        for m in existentes
    }

    importados, pulados = 0, 0
    for arq in arquivos:
        fichas = json.loads(arq.read_text(encoding="utf-8"))
        for ficha in fichas:
            disciplina = str(ficha["disciplina"]).strip().lower()
            ano = str(ficha["ano"]).strip()
            titulo = str(ficha["titulo"]).strip()
            tema_norm = normalizar_tema(titulo)
            chave = (disciplina, ano, tema_norm)
            if chave in chaves_existentes:
                pulados += 1
                continue

            ramos = [
                {
                    "titulo": str(r["titulo"]).strip(),
                    "itens": [str(it).strip() for it in r["itens"]],
                }
                for r in ficha["ramos"]
            ]

            server.save_record("mapas_mentais", {
                "disciplina": disciplina,
                "ano": ano,
                "tema": titulo,
                "temaNormalizado": tema_norm,
                "titulo": titulo,
                "ramos": ramos,
                "autor": None,
                "status": "aprovado",
                "oficial": True,
            })
            chaves_existentes.add(chave)
            importados += 1
            print(f"Importado: [{disciplina}/{ano}] {titulo}")

    print(f"\nTotal: {importados} importados, {pulados} já existiam (pulados).")


if __name__ == "__main__":
    main()
