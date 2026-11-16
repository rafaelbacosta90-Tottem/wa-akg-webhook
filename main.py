import os
import logging
from typing import Any, Dict, Optional
import httpx
from fastapi import FastAPI, Request, BackgroundTasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("wa-akg-webhook")

app = FastAPI(title="WA-AKG Webhook - Tottem Tecnologia")

# Configurações lidas das variáveis de ambiente do Render
WA_BASE_URL = os.getenv("WA_BASE_URL", "https://zap.tottemtecnologia.com.br/api").rstrip("/")
SESSION_ID = os.getenv("SESSION_ID", "")
API_KEY = os.getenv("API_KEY", "")

HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
}

LABELS_CACHE: Dict[str, str] = {}


async def enviar_texto(jid: str, texto: str) -> None:
    url = f"{WA_BASE_URL}/messages/{SESSION_ID}/{jid}/send"
    payload = {"message": {"text": texto}}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=HEADERS)
            logger.info(f"Envio para {jid} | Status: {resp.status_code} | Resp: {resp.text}")
        except Exception as e:
            logger.error(f"Falha ao enviar texto para {jid}: {e}")


async def carregar_ids_etiquetas() -> Dict[str, str]:
    url = f"{WA_BASE_URL}/labels/{SESSION_ID}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=HEADERS)
            if resp.status_code == 200:
                for item in resp.json().get("labels", []):
                    nome = item.get("name", "").strip().lower()
                    label_id = item.get("id")
                    if nome and label_id:
                        LABELS_CACHE[nome] = label_id
        except Exception as e:
            logger.error(f"Erro ao listar etiquetas: {e}")
    return LABELS_CACHE


async def aplicar_etiqueta_chat(jid: str, nome_etiqueta: str) -> None:
    chave = nome_etiqueta.strip().lower()
    if chave not in LABELS_CACHE:
        await carregar_ids_etiquetas()

    label_id = LABELS_CACHE.get(chave)
    if not label_id:
        logger.warning(f"Etiqueta '{nome_etiqueta}' não encontrada na sessão {SESSION_ID}.")
        return

    url = f"{WA_BASE_URL}/labels/{SESSION_ID}/chat/{jid}/labels"
    payload = {"labelIds": [label_id], "action": "add"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.put(url, json=payload, headers=HEADERS)
            logger.info(f"Etiqueta '{nome_etiqueta}' aplicada em {jid} | Status: {resp.status_code}")
        except Exception as e:
            logger.error(f"Erro ao aplicar etiqueta '{nome_etiqueta}' em {jid}: {e}")


async def processar_webhook(payload: Dict[str, Any]) -> None:
    logger.info(f"Webhook recebido: {payload}")

    evento = payload.get("event", "")
    if evento and evento not in {"message.received", "messages.upsert"}:
        return

    data = payload.get("data", payload)
    key = data.get("key", {})
    message_obj = data.get("message", {})

    if key.get("fromMe", data.get("fromMe", False)):
        return

    jid: Optional[str] = key.get("remoteJid") or data.get("remoteJid") or data.get("from")
    if not jid or "@g.us" in jid:
        return
    if "@" not in jid:
        jid = f"{jid}@s.whatsapp.net"

    texto = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or data.get("content")
        or data.get("text")
        or data.get("body")
        or ""
    ).strip()

    if not texto:
        return

    cmd = texto.lower()

    if cmd in {"oi", "olá", "ola", "menu", "inicio", "início", "bom dia", "boa tarde", "boa noite"}:
        menu = (
            "Olá! Bem-vindo à *Tottem Tecnologia*.\n\n"
            "Responda apenas com o número da opção desejada:\n"
            "1 - Suporte Técnico\n"
            "2 - Comercial e Orçamentos"
        )
        await enviar_texto(jid, menu)

    elif texto == "1":
        await aplicar_etiqueta_chat(jid, "Suporte")
        await enviar_texto(
            jid,
            "Você foi direcionado para o *Suporte Técnico*.\n"
            "Envie sua dúvida ou problema que nossa equipe já vai lhe atender."
        )

    elif texto == "2":
        await aplicar_etiqueta_chat(jid, "Comercial")
        await enviar_texto(
            jid,
            "Você foi direcionado para o setor *Comercial*.\n"
            "Um de nossos consultores já vai dar continuidade ao seu atendimento."
        )


@app.get("/")
async def health_check():
    return {"status": "online", "service": "wa-akg-webhook"}


@app.post("/webhook/whatsapp")
async def receber_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid_json"}

    background_tasks.add_task(processar_webhook, payload)
    return {"status": "ok"}
