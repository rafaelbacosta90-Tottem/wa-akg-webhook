import os
import re
import io
import csv
import ssl
import time
import uuid
import random
import asyncio
import hashlib
import secrets
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import httpx
import pymysql
import openpyxl
import imageio_ffmpeg
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tottem-atendimento")

app = FastAPI(title="Tottem OmniDesk — Atendimento & CRM Corporativo")

# ==============================================================================
# VARIÁVEIS DE AMBIENTE (RENDER)
# ==============================================================================
WA_BASE_URL = os.getenv("WA_BASE_URL", "https://zap.tottemtecnologia.com.br/api").rstrip("/")
SESSION_ID = os.getenv("SESSION_ID", "")
API_KEY = os.getenv("API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@tottemtecnologia.com.br")
ADMIN_SENHA = os.getenv("ADMIN_SENHA", "tottem2026")

HEADERS_WA = {"X-API-Key": API_KEY}
HEADERS_WA_JSON = {"Content-Type": "application/json", "X-API-Key": API_KEY}

MEDIA_DIR = Path("media_files")
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

# Estado em memória da janela deslizante Anti-Banimento e progresso de disparo
RATE_LIMIT_STATE = {
    "window_start": 0.0,
    "count": 0,
    "lock": asyncio.Lock(),
}

CAMPANHA_STATUS = {
    "em_execucao": False,
    "total": 0,
    "enviados": 0,
    "falhas": 0,
    "atual": "",
    "mensagem_status": "Pronto para iniciar disparos.",
}


# ==============================================================================
# SEGURANÇA, NORMALIZAÇÃO DE TELEFONE E ÁUDIO OGG/OPUS
# ==============================================================================
def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def converter_para_ogg(origem: Path) -> Path:
    destino_ogg = origem.with_suffix(".ogg")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y", "-i", str(origem),
        "-vn", "-c:a", "libopus", "-ar", "16000", "-ac", "1", "-b:a", "24k",
        str(destino_ogg)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return destino_ogg


def normalizar_numero_wa(celular: str, telefone: str = "") -> Optional[str]:
    """Prioriza celular; se vazio, usa telefone. Retorna JID formatado (55...)."""
    for bruto in (celular, telefone):
        digitos = re.sub(r"\D", "", str(bruto or ""))
        if len(digitos) >= 10:
            if not digitos.startswith("55") and len(digitos) in (10, 11):
                digitos = "55" + digitos
            return f"{digitos}@s.whatsapp.net"
    return None


def aplicar_variaveis_template(template: str, lead: Dict[str, Any]) -> str:
    texto = template
    mapa = {
        "{nome}": lead.get("nome") or lead.get("razao_social") or "Cliente",
        "{razao_social}": lead.get("razao_social") or lead.get("nome") or "",
        "{cnpj}": lead.get("cnpj") or "",
        "{telefone}": lead.get("telefone") or "",
        "{celular}": lead.get("celular") or "",
    }
    for chave, valor in mapa.items():
        texto = texto.replace(chave, str(valor))
    return texto


# ==============================================================================
# BANCO DE DADOS MYSQL (AIVEN)
# ==============================================================================
def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("Variável DATABASE_URL não configurada.")
    parsed = urlparse(DATABASE_URL)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        ssl=ssl_ctx,
    )


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perfis_atendimento (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    codigo VARCHAR(30) UNIQUE NOT NULL,
                    nome VARCHAR(100) NOT NULL,
                    opcao_menu VARCHAR(10) NOT NULL,
                    descricao VARCHAR(255) DEFAULT '',
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios_painel (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nome VARCHAR(120) NOT NULL,
                    email VARCHAR(150) UNIQUE NOT NULL,
                    senha_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) DEFAULT 'ATENDENTE',
                    perfil_id INT NULL,
                    token_sessao VARCHAR(120) NULL,
                    ativo TINYINT(1) DEFAULT 1,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT fk_usuario_perfil FOREIGN KEY (perfil_id)
                        REFERENCES perfis_atendimento(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS atendimentos (
                    jid VARCHAR(100) PRIMARY KEY,
                    nome VARCHAR(150) DEFAULT '',
                    setor VARCHAR(30) DEFAULT 'TRIAGEM',
                    status VARCHAR(30) DEFAULT 'TRIAGEM',
                    ultima_mensagem TEXT,
                    notas_crm TEXT,
                    nao_lidas INT DEFAULT 0,
                    atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mensagens_atendimento (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    jid VARCHAR(100) NOT NULL,
                    remetente VARCHAR(80) NOT NULL,
                    conteudo TEXT NOT NULL,
                    tipo_midia VARCHAR(20) DEFAULT 'TEXT',
                    url_midia VARCHAR(255) NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_jid (jid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS leads_crm (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    cnpj VARCHAR(30) DEFAULT '',
                    nome VARCHAR(180) DEFAULT '',
                    razao_social VARCHAR(220) DEFAULT '',
                    telefone VARCHAR(40) DEFAULT '',
                    celular VARCHAR(40) DEFAULT '',
                    jid VARCHAR(100) UNIQUE NOT NULL,
                    status_lead VARCHAR(30) DEFAULT 'NOVO',
                    origem VARCHAR(100) DEFAULT 'PLANILHA',
                    ultimo_disparo DATETIME NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_cnpj (cnpj),
                    INDEX idx_status (status_lead)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS templates_disparo (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    titulo VARCHAR(120) NOT NULL,
                    conteudo TEXT NOT NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS config_antiban (
                    id INT PRIMARY KEY,
                    habilitado TINYINT(1) DEFAULT 1,
                    limite_mensagens INT DEFAULT 5,
                    janela_segundos INT DEFAULT 10,
                    atraso_min_ms INT DEFAULT 1000,
                    atraso_max_ms INT DEFAULT 3000,
                    atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            cur.execute("""
                INSERT IGNORE INTO config_antiban
                    (id, habilitado, limite_mensagens, janela_segundos, atraso_min_ms, atraso_max_ms)
                VALUES (1, 1, 5, 10, 1000, 3000)
            """)

            cur.execute("""
                INSERT IGNORE INTO templates_disparo (id, titulo, conteudo)
                VALUES (
                    1,
                    'Apresentação Comercial B2B',
                    'Olá *{nome}* da empresa *{razao_social}* (CNPJ: {cnpj})! 👋\n\nSomos da *Tottem Tecnologia* e gostaríamos de apresentar nossas soluções para impulsionar seus resultados. Podemos conversar?'
                )
            """)

            cur.execute("""
                INSERT IGNORE INTO perfis_atendimento (codigo, nome, opcao_menu, descricao)
                VALUES
                    ('SUPORTE', 'Suporte Técnico', '1', 'Atendimento técnico e chamados'),
                    ('COMERCIAL', 'Comercial e Orçamentos', '2', 'Vendas, propostas e licitações')
            """)

            cur.execute("SELECT id FROM usuarios_painel WHERE email = %s", (ADMIN_EMAIL,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO usuarios_painel (nome, email, senha_hash, role, perfil_id)
                    VALUES (%s, %s, %s, 'ADMIN', NULL)
                    """,
                    ("Administrador", ADMIN_EMAIL, hash_senha(ADMIN_SENHA)),
                )
        logger.info("Banco de dados CRM & Atendimento sincronizado.")
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    if DATABASE_URL:
        init_db()


# ==============================================================================
# MOTOR DE PROTEÇÃO ANTI-BANIMENTO (JANELA DE TEMPO + ATRASO ALEATÓRIO)
# ==============================================================================
def obter_config_antiban_db() -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM config_antiban WHERE id = 1")
            cfg = cur.fetchone()
            return cfg or {
                "habilitado": 1,
                "limite_mensagens": 5,
                "janela_segundos": 10,
                "atraso_min_ms": 1000,
                "atraso_max_ms": 3000,
            }
    finally:
        conn.close()


async def aguardar_protecao_antiban():
    """Aplica o controle de taxa por janela de tempo configurado no painel."""
    cfg = obter_config_antiban_db()
    if not cfg.get("habilitado"):
        return

    limite = max(1, int(cfg.get("limite_mensagens") or 5))
    janela = max(1, int(cfg.get("janela_segundos") or 10))
    min_ms = max(0, int(cfg.get("atraso_min_ms") or 1000))
    max_ms = max(min_ms, int(cfg.get("atraso_max_ms") or 3000))

    delay_segundos = 0.0
    async with RATE_LIMIT_STATE["lock"]:
        agora = time.monotonic()
        if agora - RATE_LIMIT_STATE["window_start"] >= janela:
            RATE_LIMIT_STATE["window_start"] = agora
            RATE_LIMIT_STATE["count"] = 0

        RATE_LIMIT_STATE["count"] += 1
        if RATE_LIMIT_STATE["count"] > limite:
            delay_ms = random.randint(min_ms, max_ms)
            delay_segundos = delay_ms / 1000.0

    if delay_segundos > 0:
        logger.info(f"[Anti-Ban] Limite ({limite} msgs/{janela}s) atingido. Aguardando {delay_segundos:.2f}s...")
        await asyncio.sleep(delay_segundos)


# ==============================================================================
# INTEGRAÇÃO WA-AKG
# ==============================================================================
async def enviar_texto_wa(jid: str, texto: str) -> bool:
    await aguardar_protecao_antiban()
    url = f"{WA_BASE_URL}/messages/{SESSION_ID}/{jid}/send"
    payload = {"message": {"text": texto}}
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=HEADERS_WA_JSON)
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Erro envio texto WA: {e}")
            return False


async def enviar_midia_wa(jid: str, file_path: Path, filename: str, media_type: str, caption: str = "") -> bool:
    await aguardar_protecao_antiban()
    url = f"{WA_BASE_URL}/messages/{SESSION_ID}/{jid}/media"
    mime_map = {
        "audio": "audio/ogg; codecs=opus",
        "voice": "audio/ogg; codecs=opus",
        "image": "image/jpeg",
        "video": "video/mp4",
        "document": "application/pdf",
    }
    mime = mime_map.get(media_type, "application/octet-stream")

    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            with open(file_path, "rb") as f:
                files = {"file": (filename, f, mime)}
                tipo_api = "voice" if media_type in {"audio", "voice"} else media_type
                data = {"type": tipo_api, "caption": caption}
                resp = await client.post(url, headers=HEADERS_WA, files=files, data=data)
                return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Erro envio mídia WA: {e}")
            return False


async def baixar_midia_wa(data: Dict[str, Any], key: Dict[str, Any], tipo: str) -> Optional[str]:
    ext_map = {
        "IMAGE": ".jpg",
        "AUDIO": ".ogg",
        "VOICE": ".ogg",
        "VIDEO": ".mp4",
        "DOCUMENT": ".pdf",
        "STICKER": ".webp",
    }
    ext = ext_map.get(tipo.upper(), ".bin")
    file_url = data.get("fileUrl")
    urls_tentativa: List[str] = []

    if file_url:
        if file_url.startswith("http"):
            urls_tentativa.append(file_url)
        else:
            base_host = WA_BASE_URL.replace("/api", "")
            urls_tentativa.append(f"{base_host}{file_url if file_url.startswith('/') else '/' + file_url}")

    for mid in [data.get("id"), key.get("id")]:
        if mid:
            urls_tentativa.append(f"{WA_BASE_URL}/messages/{SESSION_ID}/download/{mid}/media")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in urls_tentativa:
            try:
                resp = await client.get(url, headers=HEADERS_WA, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 100:
                    nome_arquivo = f"in_{uuid.uuid4().hex}{ext}"
                    destino = MEDIA_DIR / nome_arquivo
                    destino.write_bytes(resp.content)
                    return f"/media/{nome_arquivo}"
            except Exception as e:
                logger.error(f"Erro download mídia {url}: {e}")
    return None


def registrar_mensagem(
    jid: str,
    remetente: str,
    conteudo: str,
    nome: str = "",
    incrementar_nao_lidas: bool = False,
    tipo_midia: str = "TEXT",
    url_midia: Optional[str] = None,
    setor_padrao: Optional[str] = None,
    status_padrao: Optional[str] = None,
):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mensagens_atendimento (jid, remetente, conteudo, tipo_midia, url_midia)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (jid, remetente, conteudo, tipo_midia, url_midia),
            )
            inc = 1 if incrementar_nao_lidas else 0
            resumo = conteudo if conteudo else f"[{tipo_midia}]"
            setor_ins = setor_padrao or "TRIAGEM"
            status_ins = status_padrao or "TRIAGEM"
            cur.execute("""
                INSERT INTO atendimentos (jid, nome, setor, status, ultima_mensagem, nao_lidas, atualizado_em)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    nome = IF(%s != '', %s, nome),
                    setor = IF(%s IS NOT NULL, %s, setor),
                    status = IF(%s IS NOT NULL, %s, status),
                    ultima_mensagem = %s,
                    nao_lidas = nao_lidas + %s,
                    atualizado_em = NOW()
            """, (
                jid, nome, setor_ins, status_ins, resumo, inc,
                nome, nome,
                setor_padrao, setor_padrao,
                status_padrao, status_padrao,
                resumo, inc
            ))
    finally:
        conn.close()


# ==============================================================================
# WEBHOOK DE RECEBIMENTO
# ==============================================================================
async def processar_webhook(payload: Dict[str, Any]) -> None:
    try:
        evento = payload.get("event", "")
        if evento and evento not in {"message.received", "messages.upsert"}:
            return

        data = payload.get("data", payload)
        key = data.get("key", {})
        message_obj = data.get("message", {})

        if key.get("fromMe", data.get("fromMe", False)):
            return

        jid: Optional[str] = key.get("remoteJid") or data.get("remoteJid") or data.get("from")
        if not jid or "@g.us" in jid or "status@broadcast" in jid:
            return
        if "@" not in jid:
            jid = f"{jid}@s.whatsapp.net"

        push_name = data.get("pushName") or payload.get("pushName") or jid.split("@")[0]
        msg_type = (data.get("type") or "TEXT").upper()

        texto = (
            message_obj.get("conversation")
            or message_obj.get("extendedTextMessage", {}).get("text")
            or data.get("content")
            or data.get("text")
            or data.get("body")
            or ""
        ).strip()

        url_midia = None
        if msg_type in {"IMAGE", "AUDIO", "VOICE", "VIDEO", "DOCUMENT", "STICKER"} or data.get("fileUrl"):
            url_midia = await baixar_midia_wa(data, key, msg_type)
            if not texto:
                texto = f"Mídia recebida ({msg_type})"
        else:
            msg_type = "TEXT"

        if not texto and not url_midia:
            return

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM atendimentos WHERE jid = %s", (jid,))
                atendimento = cur.fetchone()
                cur.execute("SELECT * FROM perfis_atendimento ORDER BY opcao_menu ASC")
                perfis = cur.fetchall()
                cur.execute("SELECT * FROM leads_crm WHERE jid = %s", (jid,))
                lead = cur.fetchone()
                if lead:
                    cur.execute("UPDATE leads_crm SET status_lead = 'EM_ATENDIMENTO' WHERE jid = %s", (jid,))
        finally:
            conn.close()

        nome_exibicao = (lead["nome"] or lead["razao_social"]) if lead else push_name
        status_atual = atendimento["status"] if atendimento else "TRIAGEM"

        registrar_mensagem(
            jid=jid,
            remetente="CLIENTE",
            conteudo=texto,
            nome=nome_exibicao,
            incrementar_nao_lidas=(status_atual == "EM_ATENDIMENTO"),
            tipo_midia=msg_type,
            url_midia=url_midia,
        )

        if status_atual == "EM_ATENDIMENTO" and texto.lower() not in {"#menu", "#sair"}:
            return

        perfil_escolhido = next((p for p in perfis if p["opcao_menu"].strip() == texto), None)

        if perfil_escolhido:
            codigo_setor = perfil_escolhido["codigo"]
            nome_setor = perfil_escolhido["nome"]
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE atendimentos SET setor = %s, status = 'EM_ATENDIMENTO', nao_lidas = 1 WHERE jid = %s",
                        (codigo_setor, jid),
                    )
            finally:
                conn.close()

            resp_msg = (
                f"✅ Você foi direcionado para *{nome_setor}*.\n"
                "Pode enviar sua mensagem, áudio ou anexo que nossa equipe já vai lhe atender."
            )
            await enviar_texto_wa(jid, resp_msg)
            registrar_mensagem(jid, "BOT", resp_msg)
        else:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE atendimentos SET setor = 'TRIAGEM', status = 'TRIAGEM' WHERE jid = %s",
                        (jid,),
                    )
            finally:
                conn.close()

            linhas_opcoes = "\n".join([f"*{p['opcao_menu']}* - {p['nome']}" for p in perfis])
            menu = (
                f"Olá, *{nome_exibicao}*! Bem-vindo à *Tottem Tecnologia*.\n\n"
                f"Responda apenas com o número do departamento desejado:\n{linhas_opcoes}"
            )
            await enviar_texto_wa(jid, menu)
            registrar_mensagem(jid, "BOT", menu)
    except Exception as e:
        logger.exception(f"Erro ao processar webhook: {e}")


# ==============================================================================
# MODELOS E ROTAS DA API
# ==============================================================================
class LoginReq(BaseModel):
    email: str
    senha: str


class PerfilReq(BaseModel):
    codigo: str
    nome: str
    opcao_menu: str
    descricao: str = ""


class UsuarioReq(BaseModel):
    nome: str
    email: str
    senha: Optional[str] = None
    role: str = "ATENDENTE"
    perfil_id: Optional[int] = None


class EnvioTextoReq(BaseModel):
    jid: str
    texto: str


class AcaoTicketReq(BaseModel):
    jid: str
    novo_setor: Optional[str] = None
    notas_crm: Optional[str] = None


class TemplateReq(BaseModel):
    titulo: str
    conteudo: str


class AntiBanReq(BaseModel):
    habilitado: bool
    limite_mensagens: int
    janela_segundos: int
    atraso_min_ms: int
    atraso_max_ms: int


class DisparoMassaReq(BaseModel):
    template_id: int
    setor_destino: str
    apenas_novos: bool = True


class IniciarChatLeadReq(BaseModel):
    lead_id: int
    setor_destino: str


def obter_usuario_atual(request: Request) -> Dict[str, Any]:
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.nome, u.email, u.role, u.perfil_id,
                       p.codigo AS perfil_codigo, p.nome AS perfil_nome
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                WHERE u.token_sessao = %s AND u.ativo = 1
            """, (token,))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="Sessão inválida")
            return user
    finally:
        conn.close()


def exigir_admin(user: Dict[str, Any]):
    if user.get("role") != "ADMIN":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")


@app.post("/api/auth/login")
def login(dados: LoginReq):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.*, p.codigo AS perfil_codigo, p.nome AS perfil_nome
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                WHERE u.email = %s AND u.senha_hash = %s AND u.ativo = 1
            """, (dados.email.strip().lower(), hash_senha(dados.senha)))
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="Credenciais inválidas")

            token = secrets.token_hex(32)
            cur.execute("UPDATE usuarios_painel SET token_sessao = %s WHERE id = %s", (token, user["id"]))

            return {
                "token": token,
                "usuario": {
                    "id": user["id"],
                    "nome": user["nome"],
                    "email": user["email"],
                    "role": user["role"],
                    "perfil_id": user["perfil_id"],
                    "perfil_codigo": user["perfil_codigo"] or "TODOS",
                    "perfil_nome": user["perfil_nome"] or "Supervisão Geral",
                },
            }
    finally:
        conn.close()


# --- ROTAS ADMIN: PERFIS E USUÁRIOS ---
@app.get("/api/admin/perfis")
def listar_perfis(request: Request):
    obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM perfis_atendimento ORDER BY opcao_menu ASC")
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/admin/perfis")
def criar_perfil(dados: PerfilReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO perfis_atendimento (codigo, nome, opcao_menu, descricao)
                VALUES (%s, %s, %s, %s)
                """,
                (dados.codigo.strip().upper(), dados.nome.strip(), dados.opcao_menu.strip(), dados.descricao.strip()),
            )
        return {"status": "criado"}
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=400, detail="Código já existe.")
    finally:
        conn.close()


@app.delete("/api/admin/perfis/{perfil_id}")
def excluir_perfil(perfil_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM perfis_atendimento WHERE id = %s", (perfil_id,))
        return {"status": "excluido"}
    finally:
        conn.close()


@app.get("/api/admin/usuarios")
def listar_usuarios(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.nome, u.email, u.role, u.perfil_id, u.ativo,
                       p.nome AS perfil_nome, p.codigo AS perfil_codigo
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                ORDER BY u.id ASC
            """)
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/admin/usuarios")
def criar_usuario(dados: UsuarioReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    if not dados.senha:
        raise HTTPException(status_code=400, detail="Senha obrigatória")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO usuarios_painel (nome, email, senha_hash, role, perfil_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (dados.nome.strip(), dados.email.strip().lower(), hash_senha(dados.senha), dados.role, dados.perfil_id),
            )
        return {"status": "criado"}
    except pymysql.err.IntegrityError:
        raise HTTPException(status_code=400, detail="E-mail já cadastrado.")
    finally:
        conn.close()


@app.put("/api/admin/usuarios/{usuario_id}/vincular")
def vincular_usuario_perfil(usuario_id: int, dados: UsuarioReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE usuarios_painel
                SET nome = %s, email = %s, role = %s, perfil_id = %s
                WHERE id = %s
                """,
                (dados.nome.strip(), dados.email.strip().lower(), dados.role, dados.perfil_id, usuario_id),
            )
        return {"status": "atualizado"}
    finally:
        conn.close()


@app.delete("/api/admin/usuarios/{usuario_id}")
def excluir_usuario(usuario_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    if user["id"] == usuario_id:
        raise HTTPException(status_code=400, detail="Não é possível excluir seu próprio usuário.")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM usuarios_painel WHERE id = %s", (usuario_id,))
        return {"status": "excluido"}
    finally:
        conn.close()


# --- ROTAS CRM LEADS, IMPORTAÇÃO PLANILHA, TEMPLATES E DISPARO EM MASSA ---
@app.get("/api/admin/leads")
def listar_leads(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads_crm ORDER BY id DESC LIMIT 1000")
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/admin/leads/importar")
async def importar_planilha_leads(request: Request, arquivo: UploadFile = File(...)):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    conteudo = await arquivo.read()
    nome_arq = (arquivo.filename or "").lower()
    linhas_dict: List[Dict[str, str]] = []

    if nome_arq.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(conteudo), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise HTTPException(status_code=400, detail="Planilha vazia.")
        cabecalhos = [str(c or "").strip().lower() for c in rows[0]]
        for r in rows[1:]:
            linhas_dict.append({cabecalhos[i]: str(r[i] or "").strip() for i in range(len(cabecalhos)) if i < len(r)})
    else:
        texto_csv = conteudo.decode("utf-8-sig", errors="ignore")
        delimitador = ";" if ";" in texto_csv.splitlines()[0] else ","
        reader = csv.DictReader(io.StringIO(texto_csv), delimiter=delimitador)
        for row in reader:
            linhas_dict.append({str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()})

    def buscar_col(d: Dict[str, str], chaves: List[str]) -> str:
        for k, v in d.items():
            k_norm = k.replace("ã", "a").replace("ç", "c").replace("_", " ").strip()
            if k_norm in chaves and v:
                return v
        return ""

    importados = 0
    ignorados = 0
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for item in linhas_dict:
                cnpj = buscar_col(item, ["cnpj", "cpf/cnpj", "documento"])
                nome = buscar_col(item, ["nome", "contato", "responsavel", "socio"])
                razao = buscar_col(item, ["razao social", "razao", "empresa", "fantasia"])
                telefone = buscar_col(item, ["telefone", "fone", "tel", "telefone fixo"])
                celular = buscar_col(item, ["celular", "whatsapp", "cel", "movel"])

                jid = normalizar_numero_wa(celular, telefone)
                if not jid:
                    ignorados += 1
                    continue

                cur.execute("""
                    INSERT INTO leads_crm (cnpj, nome, razao_social, telefone, celular, jid, status_lead, origem)
                    VALUES (%s, %s, %s, %s, %s, %s, 'NOVO', %s)
                    ON DUPLICATE KEY UPDATE
                        cnpj = IF(%s != '', %s, cnpj),
                        nome = IF(%s != '', %s, nome),
                        razao_social = IF(%s != '', %s, razao_social),
                        telefone = IF(%s != '', %s, telefone),
                        celular = IF(%s != '', %s, celular)
                """, (
                    cnpj, nome, razao, telefone, celular, jid, arquivo.filename or "PLANILHA",
                    cnpj, cnpj, nome, nome, razao, razao, telefone, telefone, celular, celular
                ))
                importados += 1
    finally:
        conn.close()

    return {"status": "ok", "importados": importados, "ignorados": ignorados}


@app.delete("/api/admin/leads/{lead_id}")
def excluir_lead(lead_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM leads_crm WHERE id = %s", (lead_id,))
        return {"status": "excluido"}
    finally:
        conn.close()


@app.post("/api/admin/leads/abrir-conversa")
def abrir_conversa_lead(dados: IniciarChatLeadReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads_crm WHERE id = %s", (dados.lead_id,))
            lead = cur.fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="Lead não encontrado")

            nome_chat = lead["nome"] or lead["razao_social"] or lead["jid"].split("@")[0]
            cur.execute("""
                INSERT INTO atendimentos (jid, nome, setor, status, ultima_mensagem, atualizado_em)
                VALUES (%s, %s, %s, 'EM_ATENDIMENTO', 'Conversa iniciada via CRM', NOW())
                ON DUPLICATE KEY UPDATE
                    setor = %s,
                    status = 'EM_ATENDIMENTO',
                    atualizado_em = NOW()
            """, (lead["jid"], nome_chat, dados.setor_destino, dados.setor_destino))
            return {"status": "ok", "jid": lead["jid"]}
    finally:
        conn.close()


@app.get("/api/admin/templates")
def listar_templates(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM templates_disparo ORDER BY id DESC")
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/admin/templates")
def criar_template(dados: TemplateReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO templates_disparo (titulo, conteudo) VALUES (%s, %s)",
                (dados.titulo.strip(), dados.conteudo.strip()),
            )
        return {"status": "criado"}
    finally:
        conn.close()


@app.delete("/api/admin/templates/{tpl_id}")
def excluir_template(tpl_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM templates_disparo WHERE id = %s", (tpl_id,))
        return {"status": "excluido"}
    finally:
        conn.close()


@app.get("/api/admin/antiban")
def obter_antiban(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    return obter_config_antiban_db()


@app.post("/api/admin/antiban")
def salvar_antiban(dados: AntiBanReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE config_antiban
                SET habilitado = %s,
                    limite_mensagens = %s,
                    janela_segundos = %s,
                    atraso_min_ms = %s,
                    atraso_max_ms = %s
                WHERE id = 1
            """, (
                1 if dados.habilitado else 0,
                max(1, dados.limite_mensagens),
                max(1, dados.janela_segundos),
                max(0, dados.atraso_min_ms),
                max(dados.atraso_min_ms, dados.atraso_max_ms),
            ))
        return {"status": "salvo"}
    finally:
        conn.close()


async def executar_campanha_background(leads: List[Dict[str, Any]], template_texto: str, setor_destino: str, autor: str):
    CAMPANHA_STATUS["em_execucao"] = True
    CAMPANHA_STATUS["total"] = len(leads)
    CAMPANHA_STATUS["enviados"] = 0
    CAMPANHA_STATUS["falhas"] = 0

    try:
        for lead in leads:
            jid = lead["jid"]
            nome_lead = lead.get("nome") or lead.get("razao_social") or jid.split("@")[0]
            CAMPANHA_STATUS["atual"] = f"{nome_lead} ({jid.split('@')[0]})"
            CAMPANHA_STATUS["mensagem_status"] = f"Enviando para {nome_lead}..."

            mensagem_final = aplicar_variaveis_template(template_texto, lead)
            ok = await enviar_texto_wa(jid, mensagem_final)

            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    if ok:
                        CAMPANHA_STATUS["enviados"] += 1
                        cur.execute(
                            "UPDATE leads_crm SET status_lead = 'CONTATADO', ultimo_disparo = NOW() WHERE id = %s",
                            (lead["id"],),
                        )
                    else:
                        CAMPANHA_STATUS["falhas"] += 1
                        cur.execute(
                            "UPDATE leads_crm SET status_lead = 'FALHA_ENVIO' WHERE id = %s",
                            (lead["id"],),
                        )
            finally:
                conn.close()

            if ok:
                registrar_mensagem(
                    jid=jid,
                    remetente=f"CAMPANHA ({autor})",
                    conteudo=mensagem_final,
                    nome=nome_lead,
                    incrementar_nao_lidas=False,
                    tipo_midia="TEXT",
                    setor_padrao=setor_destino,
                    status_padrao="EM_ATENDIMENTO",
                )
    finally:
        CAMPANHA_STATUS["em_execucao"] = False
        CAMPANHA_STATUS["atual"] = ""
        CAMPANHA_STATUS["mensagem_status"] = (
            f"Concluído: {CAMPANHA_STATUS['enviados']} enviados, {CAMPANHA_STATUS['falhas']} falhas."
        )


@app.post("/api/admin/disparo-massa")
async def iniciar_disparo_massa(dados: DisparoMassaReq, request: Request, background_tasks: BackgroundTasks):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    if CAMPANHA_STATUS["em_execucao"]:
        raise HTTPException(status_code=400, detail="Já existe um disparo em massa em andamento.")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM templates_disparo WHERE id = %s", (dados.template_id,))
            tpl = cur.fetchone()
            if not tpl:
                raise HTTPException(status_code=404, detail="Template de mensagem não encontrado.")

            if dados.apenas_novos:
                cur.execute("SELECT * FROM leads_crm WHERE status_lead = 'NOVO' ORDER BY id ASC")
            else:
                cur.execute("SELECT * FROM leads_crm ORDER BY id ASC")
            leads = cur.fetchall()
    finally:
        conn.close()

    if not leads:
        raise HTTPException(status_code=400, detail="Nenhum lead disponível para o filtro selecionado.")

    background_tasks.add_task(
        executar_campanha_background,
        leads,
        tpl["conteudo"],
        dados.setor_destino,
        user["nome"],
    )
    return {"status": "iniciado", "total": len(leads)}


@app.get("/api/admin/disparo-massa/status")
def status_disparo_massa(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    return CAMPANHA_STATUS


# --- ROTAS DE ATENDIMENTO ---
@app.get("/")
async def health_check():
    return {"status": "online", "service": "tottem-omnidesk-crm"}


@app.post("/webhook/whatsapp")
async def receber_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid_json"}
    background_tasks.add_task(processar_webhook, payload)
    return {"status": "ok"}


@app.get("/api/painel/chats")
def listar_chats(request: Request, filtro_setor: Optional[str] = None):
    user = obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            base_sql = """
                SELECT a.*, DATE_FORMAT(a.atualizado_em, '%%H:%%i') as hora_fmt,
                       l.cnpj, l.razao_social, l.telefone AS lead_telefone, l.celular AS lead_celular
                FROM atendimentos a
                LEFT JOIN leads_crm l ON a.jid = l.jid
            """
            if user["role"] == "ADMIN":
                if filtro_setor and filtro_setor != "TODOS":
                    cur.execute(
                        base_sql + " WHERE a.setor = %s AND a.status = 'EM_ATENDIMENTO' ORDER BY a.atualizado_em DESC",
                        (filtro_setor,),
                    )
                else:
                    cur.execute(base_sql + " ORDER BY a.atualizado_em DESC LIMIT 150")
            else:
                setor_user = user["perfil_codigo"]
                if not setor_user:
                    return []
                cur.execute(
                    base_sql + " WHERE a.setor = %s AND a.status = 'EM_ATENDIMENTO' ORDER BY a.atualizado_em DESC",
                    (setor_user,),
                )
            return cur.fetchall()
    finally:
        conn.close()


@app.get("/api/painel/mensagens/{jid}")
def listar_mensagens(jid: str, request: Request):
    obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE atendimentos SET nao_lidas = 0 WHERE jid = %s", (jid,))
            cur.execute(
                """
                SELECT id, remetente, conteudo, tipo_midia, url_midia,
                       DATE_FORMAT(criado_em, '%%H:%%i') as hora
                FROM mensagens_atendimento
                WHERE jid = %s ORDER BY id ASC LIMIT 300
                """,
                (jid,),
            )
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/painel/notas")
def salvar_notas_crm(dados: AcaoTicketReq, request: Request):
    obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE atendimentos SET notas_crm = %s WHERE jid = %s", (dados.notas_crm or "", dados.jid))
        return {"status": "salvo"}
    finally:
        conn.close()


@app.post("/api/painel/enviar")
async def responder_texto(dados: EnvioTextoReq, request: Request):
    user = obter_usuario_atual(request)
    sucesso = await enviar_texto_wa(dados.jid, dados.texto)
    if not sucesso:
        raise HTTPException(status_code=500, detail="Falha ao enviar mensagem pelo WA-AKG")

    assinatura = f"{user['nome']} • {user['perfil_codigo'] or 'ADMIN'}"
    registrar_mensagem(dados.jid, assinatura, dados.texto, tipo_midia="TEXT")
    return {"status": "enviado"}


@app.post("/api/painel/enviar-midia")
async def responder_midia(
    request: Request,
    jid: str = Form(...),
    tipo: str = Form(...),
    legenda: str = Form(""),
    arquivo: UploadFile = File(...),
):
    user = obter_usuario_atual(request)
    ext = Path(arquivo.filename or "anexo.bin").suffix or ".bin"
    nome_salvo = f"out_{uuid.uuid4().hex}{ext}"
    caminho_salvo = MEDIA_DIR / nome_salvo

    conteudo_bytes = await arquivo.read()
    caminho_salvo.write_bytes(conteudo_bytes)

    if tipo in {"voice", "audio"} and ext.lower() not in {".ogg"}:
        try:
            caminho_ogg = converter_para_ogg(caminho_salvo)
            caminho_salvo = caminho_ogg
            nome_salvo = caminho_ogg.name
        except Exception as e:
            logger.error(f"Erro na conversão FFmpeg para OGG: {e}")

    sucesso = await enviar_midia_wa(jid, caminho_salvo, nome_salvo, tipo, legenda)
    if not sucesso:
        raise HTTPException(status_code=500, detail="Falha ao enviar mídia pelo WA-AKG")

    assinatura = f"{user['nome']} • {user['perfil_codigo'] or 'ADMIN'}"
    texto_registro = legenda if legenda else f"Anexo enviado ({nome_salvo})"
    registrar_mensagem(
        jid,
        assinatura,
        texto_registro,
        tipo_midia=tipo.upper(),
        url_midia=f"/media/{nome_salvo}",
    )
    return {"status": "enviado"}


@app.post("/api/painel/transferir")
async def transferir_setor(dados: AcaoTicketReq, request: Request):
    obter_usuario_atual(request)
    if not dados.novo_setor:
        raise HTTPException(status_code=400, detail="Informe o novo setor")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE atendimentos SET setor = %s, status = 'EM_ATENDIMENTO', nao_lidas = 1 WHERE jid = %s",
                (dados.novo_setor, dados.jid),
            )
    finally:
        conn.close()

    aviso = f"🔄 Seu atendimento foi transferido para o departamento *{dados.novo_setor}*. Aguarde um instante."
    await enviar_texto_wa(dados.jid, aviso)
    registrar_mensagem(dados.jid, "BOT", aviso)
    return {"status": "transferido"}


@app.post("/api/painel/encerrar")
async def encerrar_atendimento(dados: AcaoTicketReq, request: Request):
    obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE atendimentos SET setor = 'TRIAGEM', status = 'TRIAGEM', nao_lidas = 0 WHERE jid = %s",
                (dados.jid,),
            )
    finally:
        conn.close()

    despedida = "✅ Atendimento encerrado. Caso precise de algo mais, envie *Oi* para iniciar novamente!"
    await enviar_texto_wa(dados.jid, despedida)
    registrar_mensagem(dados.jid, "BOT", despedida)
    return {"status": "encerrado"}


# ==============================================================================
# INTERFACE ENTERPRISE OMNICHANNEL + CRM + ANTI-BAN (/painel)
# ==============================================================================
HTML_PAINEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tottem OmniDesk — CRM & Atendimento</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Inter', sans-serif; }
    .chat-bg {
      background-color: #efeae2;
      background-image: radial-gradient(#d4cdc3 0.75px, transparent 0.75px);
      background-size: 16px 16px;
    }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 9999px; }
  </style>
</head>
<body class="bg-slate-950 h-screen flex overflow-hidden select-none">

  <!-- LOGIN -->
  <div id="loginModal" class="fixed inset-0 bg-slate-950 flex items-center justify-center z-50 p-4">
    <div class="bg-white rounded-2xl shadow-2xl w-full max-w-4xl grid grid-cols-1 md:grid-cols-2 overflow-hidden">
      <div class="bg-gradient-to-br from-slate-900 via-slate-800 to-emerald-950 text-white p-10 flex flex-col justify-between">
        <div>
          <div class="inline-flex items-center gap-2 bg-emerald-500/20 border border-emerald-400/30 text-emerald-300 text-xs font-semibold px-3 py-1 rounded-full mb-6">
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Tottem OmniDesk CRM
          </div>
          <h1 class="text-3xl font-bold tracking-tight leading-tight">Central de Atendimento & Prospecção</h1>
          <p class="text-slate-300 text-sm mt-4 leading-relaxed">
            Gestão omnichannel por departamentos, CRM de leads B2B, disparos inteligentes com proteção anti-banimento e áudio nativo.
          </p>
        </div>
        <div class="text-xs text-slate-400 border-t border-slate-700/80 pt-4">
          Tottem Tecnologia • Plataforma Corporativa
        </div>
      </div>

      <div class="p-10 flex flex-col justify-center space-y-5">
        <div>
          <h2 class="text-xl font-bold text-slate-900">Acesso ao Painel</h2>
          <p class="text-xs text-slate-500 mt-1">Insira suas credenciais corporativas.</p>
        </div>
        <div>
          <label class="block text-xs font-semibold uppercase tracking-wider text-slate-600 mb-1.5">E-mail</label>
          <input id="loginEmail" type="email" placeholder="admin@tottemtecnologia.com.br"
                 class="w-full border border-slate-300 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" />
        </div>
        <div>
          <label class="block text-xs font-semibold uppercase tracking-wider text-slate-600 mb-1.5">Senha</label>
          <input id="loginSenha" type="password" placeholder="••••••••"
                 onkeydown="if(event.key==='Enter') fazerLogin()"
                 class="w-full border border-slate-300 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500" />
        </div>
        <div id="loginErro" class="hidden bg-rose-50 border border-rose-200 text-rose-700 text-xs px-3 py-2.5 rounded-lg">
          E-mail ou senha inválidos.
        </div>
        <button onclick="fazerLogin()" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-3 rounded-xl shadow-lg shadow-emerald-600/20 transition text-sm">
          Entrar na Plataforma
        </button>
      </div>
    </div>
  </div>

  <!-- SIDEBAR NAVIGATION RAIL -->
  <nav class="w-16 bg-slate-900 border-r border-slate-800 flex flex-col items-center py-4 justify-between z-20">
    <div class="flex flex-col items-center gap-3">
      <div class="w-10 h-10 rounded-xl bg-emerald-600 text-white font-bold flex items-center justify-center shadow-lg shadow-emerald-600/30 text-lg">
        T
      </div>
      <div class="h-px w-8 bg-slate-800 my-1"></div>

      <button onclick="mudarAba('atendimento')" id="railAtendimento" title="Central de Atendimento"
              class="w-11 h-11 rounded-xl flex items-center justify-center text-white bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
      </button>

      <button onclick="mudarAba('crm')" id="railCrm" title="Leads CRM & Disparos em Massa"
              class="hidden w-11 h-11 rounded-xl flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>
      </button>

      <button onclick="mudarAba('antiban')" id="railAntiban" title="Proteção Anti-Banimento"
              class="hidden w-11 h-11 rounded-xl flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
      </button>

      <button onclick="mudarAba('perfis')" id="railPerfis" title="Perfis e Departamentos"
              class="hidden w-11 h-11 rounded-xl flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
      </button>

      <button onclick="mudarAba('usuarios')" id="railUsuarios" title="Equipe e Vínculos"
              class="hidden w-11 h-11 rounded-xl flex items-center justify-center text-slate-400 hover:text-white hover:bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
      </button>
    </div>

    <div class="flex flex-col items-center gap-3">
      <div class="relative">
        <div id="avatarOperador" class="w-9 h-9 rounded-full bg-slate-700 text-xs font-bold text-white flex items-center justify-center">OP</div>
        <span class="absolute bottom-0 right-0 w-2.5 h-2.5 bg-emerald-500 border-2 border-slate-900 rounded-full"></span>
      </div>
      <button onclick="sair()" title="Sair" class="w-10 h-10 rounded-xl flex items-center justify-center text-slate-400 hover:text-rose-400 hover:bg-slate-800 transition">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
      </button>
    </div>
  </nav>

  <!-- ÁREA PRINCIPAL -->
  <div class="flex-1 flex flex-col bg-slate-50 overflow-hidden">

    <!-- TOPBAR -->
    <header class="h-14 bg-white border-b border-slate-200 px-6 flex items-center justify-between shrink-0">
      <div class="flex items-center gap-3">
        <span id="tituloPagina" class="font-bold text-slate-800 text-base">Atendimentos</span>
        <span id="badgeSetorAtual" class="bg-emerald-50 text-emerald-700 border border-emerald-200 text-xs font-semibold px-2.5 py-0.5 rounded-full"></span>
      </div>
      <div class="flex items-center gap-4 text-xs text-slate-500">
        <span class="inline-flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-emerald-500"></span> WhatsApp Conectado
        </span>
        <span id="nomeOperadorTop" class="font-semibold text-slate-700"></span>
      </div>
    </header>

    <!-- =================================================================== -->
    <!-- ABA 1: WORKSPACE DE ATENDIMENTO                                     -->
    <!-- =================================================================== -->
    <div id="abaAtendimento" class="flex-1 flex overflow-hidden">
      <aside class="w-80 bg-white border-r border-slate-200 flex flex-col">
        <div class="p-3 border-b border-slate-100 space-y-2.5">
          <div class="relative">
            <input id="buscaContato" oninput="renderizarListaChats()" type="text" placeholder="Buscar cliente, CNPJ ou número..."
                   class="w-full bg-slate-100 text-xs rounded-lg pl-8 pr-3 py-2 focus:outline-none focus:bg-white focus:ring-1 focus:ring-emerald-500" />
            <svg class="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
          </div>
          <div class="flex items-center justify-between gap-2">
            <select id="filtroAdminSetor" onchange="atualizarFila()" class="hidden flex-1 text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-slate-50 text-slate-700 font-medium">
              <option value="TODOS">Todos os Departamentos</option>
            </select>
            <span class="text-[11px] font-semibold text-slate-500 whitespace-nowrap">Fila: <strong id="contadorChats" class="text-slate-800">0</strong></span>
          </div>
        </div>
        <div id="listaChats" class="flex-1 overflow-y-auto divide-y divide-slate-100"></div>
      </aside>

      <main class="flex-1 flex flex-col chat-bg relative">
        <div id="chatVazio" class="flex-1 flex flex-col items-center justify-center text-slate-400 bg-slate-50/90">
          <p class="text-sm font-medium text-slate-600">Nenhuma conversa selecionada</p>
          <p class="text-xs text-slate-400 mt-1">Escolha um cliente na fila à esquerda ou inicie uma conversa pela aba CRM.</p>
        </div>

        <div id="chatAtivo" class="hidden flex-1 flex flex-col h-full">
          <div class="h-14 bg-white px-5 border-b border-slate-200 flex items-center justify-between shadow-sm z-10">
            <div class="flex items-center gap-3">
              <div id="avatarClienteChat" class="w-9 h-9 rounded-full bg-emerald-600 text-white font-bold text-xs flex items-center justify-center">CL</div>
              <div>
                <h2 id="contatoNome" class="font-bold text-slate-800 text-sm leading-tight"></h2>
                <p id="contatoNumero" class="text-[11px] text-slate-500"></p>
              </div>
            </div>
            <span id="badgeSetorChat" class="text-[11px] font-bold px-2.5 py-1 rounded-md bg-slate-100 text-slate-700 border border-slate-200"></span>
          </div>

          <div id="areaMensagens" class="flex-1 overflow-y-auto px-6 py-4 space-y-3"></div>

          <div class="bg-white px-4 py-3 border-t border-slate-200 flex items-center gap-2.5">
            <input type="file" id="inputArquivo" class="hidden" onchange="enviarAnexoSelecionado()" />
            <button type="button" onclick="document.getElementById('inputArquivo').click()" title="Anexar Arquivo"
                    class="w-10 h-10 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-600 flex items-center justify-center transition">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"/></svg>
            </button>

            <button type="button" id="btnGravarAudio" onclick="alternarGravacaoAudio()" title="Gravar Áudio"
                    class="w-10 h-10 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-600 flex items-center justify-center transition">
              <svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"/></svg>
            </button>

            <span id="statusGravacao" class="hidden text-xs font-semibold text-rose-600 animate-pulse">Gravando...</span>

            <form onsubmit="enviarTexto(event)" class="flex-1 flex gap-2">
              <input id="inputMensagem" type="text" placeholder="Escreva uma mensagem..." autocomplete="off"
                     class="flex-1 bg-slate-100 border border-transparent focus:bg-white focus:border-emerald-500 rounded-xl px-4 py-2.5 text-sm focus:outline-none transition" />
              <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-5 py-2.5 rounded-xl text-sm shadow-sm transition">
                Enviar
              </button>
            </form>
          </div>
        </div>
      </main>

      <!-- PAINEL DIREITO: DETALHES CRM -->
      <aside id="painelDireito" class="hidden w-72 bg-white border-l border-slate-200 flex flex-col p-4 space-y-5 overflow-y-auto">
        <div id="blocoDadosEmpresa" class="hidden bg-emerald-50/60 border border-emerald-200/80 rounded-xl p-3 space-y-1.5">
          <div class="text-[10px] font-bold uppercase tracking-wider text-emerald-700">Dados Cadastrais (CRM)</div>
          <div class="text-xs font-bold text-slate-800" id="crmRazaoSocial"></div>
          <div class="text-[11px] font-mono text-slate-600" id="crmCnpj"></div>
        </div>

        <div>
          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">Controle do Chamado</h3>
          <div class="space-y-3 bg-slate-50 p-3.5 rounded-xl border border-slate-200/80">
            <div>
              <label class="block text-[11px] font-semibold text-slate-600 mb-1">Transferir Departamento</label>
              <div class="flex gap-1.5">
                <select id="selectTransferir" class="flex-1 text-xs border border-slate-300 rounded-lg px-2 py-1.5 bg-white"></select>
                <button onclick="transferir()" class="bg-amber-500 hover:bg-amber-600 text-white text-xs font-semibold px-2.5 py-1.5 rounded-lg transition">
                  Mover
                </button>
              </div>
            </div>
            <div class="pt-2 border-t border-slate-200">
              <button onclick="encerrar()" class="w-full bg-rose-50 hover:bg-rose-600 text-rose-700 hover:text-white border border-rose-200 text-xs font-semibold py-2 rounded-lg transition">
                Encerrar Atendimento
              </button>
            </div>
          </div>
        </div>

        <div class="flex-1 flex flex-col">
          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">Anotações Internas (CRM)</h3>
          <textarea id="notasCrmInput" rows="5" placeholder="Registre observações sobre este cliente..."
                    class="w-full border border-slate-200 rounded-xl p-2.5 text-xs focus:outline-none focus:border-emerald-500 bg-slate-50"></textarea>
          <button onclick="salvarNotas()" class="mt-2 bg-slate-800 hover:bg-slate-900 text-white text-xs font-semibold py-2 rounded-lg transition">
            Salvar Anotação
          </button>
        </div>
      </aside>
    </div>

    <!-- =================================================================== -->
    <!-- ABA 2: CRM DE LEADS, IMPORTAÇÃO PLANILHA E DISPARO EM MASSA         -->
    <!-- =================================================================== -->
    <div id="abaCrm" class="hidden flex-1 overflow-y-auto p-6">
      <div class="max-w-7xl mx-auto space-y-6">

        <!-- GRID SUPERIOR: IMPORTAR PLANILHA + TEMPLATES + DISPARADOR -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

          <!-- CARD 1: IMPORTAR PLANILHA -->
          <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
              <span class="text-[11px] font-bold uppercase tracking-wider text-emerald-600">Passo 1 • Base de Leads</span>
              <h3 class="text-base font-bold text-slate-800 mt-1">Importar Planilha de Contatos</h3>
              <p class="text-xs text-slate-500 mt-1 leading-relaxed">
                Envie um arquivo <strong>.xlsx</strong> ou <strong>.csv</strong> contendo as colunas:<br>
                <code class="bg-slate-100 px-1.5 py-0.5 rounded text-slate-700">CNPJ, Nome, Razao Social, Telefone, Celular</code>
              </p>
            </div>
            <div class="mt-4 space-y-3">
              <input type="file" id="inputPlanilhaLeads" accept=".xlsx,.csv" class="block w-full text-xs text-slate-500 file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-semibold file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200" />
              <button onclick="importarPlanilha()" class="w-full bg-slate-900 hover:bg-slate-800 text-white font-semibold py-2.5 rounded-xl text-xs transition">
                Importar para Tabela CRM
              </button>
              <p id="resumoImportacao" class="text-xs text-emerald-700 font-medium hidden"></p>
            </div>
          </div>

          <!-- CARD 2: CADASTRAR MENSAGEM / TEMPLATE -->
          <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
              <span class="text-[11px] font-bold uppercase tracking-wider text-blue-600">Passo 2 • Mensagem Cadastrada</span>
              <h3 class="text-base font-bold text-slate-800 mt-1">Templates de Abordagem</h3>
              <p class="text-[11px] text-slate-500 mt-1">
                Variáveis disponíveis: <code>{nome}</code>, <code>{razao_social}</code>, <code>{cnpj}</code>, <code>{celular}</code>
              </p>
              <div class="mt-3 space-y-2">
                <input id="tplTitulo" type="text" placeholder="Título da campanha (ex: Prospecção Edital)"
                       class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs" />
                <textarea id="tplConteudo" rows="3" placeholder="Olá {nome} da {razao_social}..."
                          class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs"></textarea>
              </div>
            </div>
            <button onclick="salvarTemplate()" class="mt-3 w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-xl text-xs transition">
              + Salvar Mensagem Cadastrada
            </button>
          </div>

          <!-- CARD 3: EXECUTAR DISPARO EM MASSA -->
          <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
              <span class="text-[11px] font-bold uppercase tracking-wider text-amber-600">Passo 3 • Campanha Ativa</span>
              <h3 class="text-base font-bold text-slate-800 mt-1">Disparar e Criar Conversas</h3>
              <div class="mt-3 space-y-2.5">
                <div>
                  <label class="block text-[11px] font-semibold text-slate-600 mb-1">Mensagem Cadastrada</label>
                  <select id="selectTemplateDisparo" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs bg-white"></select>
                </div>
                <div>
                  <label class="block text-[11px] font-semibold text-slate-600 mb-1">Departamento que receberá as respostas</label>
                  <select id="selectSetorDisparo" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs bg-white"></select>
                </div>
                <label class="flex items-center gap-2 text-xs text-slate-700 pt-1">
                  <input type="checkbox" id="chkApenasNovos" checked class="rounded text-emerald-600" />
                  Enviar apenas para leads com status <strong>NOVO</strong>
                </label>
              </div>
            </div>
            <div class="mt-3 space-y-2">
              <button onclick="iniciarDisparoMassa()" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2.5 rounded-xl text-xs shadow-sm transition">
                Iniciar Envio em Massa
              </button>
              <div id="boxProgressoCampanha" class="hidden bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-[11px] text-slate-700"></div>
            </div>
          </div>
        </div>

        <!-- TABELA DE LEADS CRM -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
          <div class="px-6 py-4 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-sm font-bold text-slate-800">Base de Leads Cadastrados (CRM)</h3>
              <p class="text-xs text-slate-500">Total de empresas/contatos importados: <strong id="totalLeadsBadge">0</strong></p>
            </div>
            <input id="buscaLeadsCrm" oninput="renderizarTabelaLeads()" type="text" placeholder="Filtrar por CNPJ, Razão Social, Nome ou Telefone..."
                   class="w-80 border border-slate-300 rounded-xl px-3.5 py-2 text-xs focus:outline-none focus:border-emerald-500" />
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="bg-slate-50 border-b border-slate-200 uppercase text-slate-500 font-semibold">
                <tr>
                  <th class="px-4 py-3">CNPJ</th>
                  <th class="px-4 py-3">Razão Social</th>
                  <th class="px-4 py-3">Contato / Nome</th>
                  <th class="px-4 py-3">Celular / Telefone</th>
                  <th class="px-4 py-3">Status CRM</th>
                  <th class="px-4 py-3 text-right">Ações</th>
                </tr>
              </thead>
              <tbody id="tabelaLeadsBody" class="divide-y divide-slate-100"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- =================================================================== -->
    <!-- ABA 3: PROTEÇÃO ANTI-BANIMENTO (BETA)                               -->
    <!-- =================================================================== -->
    <div id="abaAntiban" class="hidden flex-1 overflow-y-auto p-8 bg-white">
      <div class="max-w-4xl mx-auto space-y-6">
        <div>
          <div class="flex items-center gap-2.5">
            <span class="w-6 h-6 rounded-full border-2 border-slate-800 text-slate-800 font-bold text-xs flex items-center justify-center">!</span>
            <h2 class="text-xl font-bold text-slate-900">Proteção Anti-Banimento (Beta)</h2>
          </div>
          <p class="text-sm text-slate-600 mt-2 leading-relaxed">
            Evite que o seu número do WhatsApp seja detectado como spam ou banido adicionando atrasos aleatórios inteligentes entre as mensagens enviadas. Isso se aplica a todas as ações: respostas do bot, respostas automáticas, transmissões, mensagens agendadas e chamadas de API para esta sessão.
          </p>
        </div>

        <!-- CARD TOGGLE LARANJA SUAVE -->
        <div class="bg-orange-50/70 border border-orange-200/80 rounded-2xl p-5 flex items-center justify-between gap-4">
          <div class="flex-1 text-center">
            <h3 class="text-sm font-bold text-orange-700">Habilitar atraso anti-spam</h3>
            <p class="text-xs text-slate-600 mt-1">
              Quando ativado, as mensagens serão enfileiradas e enviadas com um atraso aleatório se o limite de taxa for atingido. As mensagens nunca são rejeitadas — apenas atrasadas.
            </p>
          </div>
          <label class="relative inline-flex items-center cursor-pointer shrink-0">
            <input type="checkbox" id="abHabilitado" class="sr-only peer" checked>
            <div class="w-12 h-6 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-6 peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-emerald-600"></div>
          </label>
        </div>

        <!-- CARD COMO FUNCIONA AZUL SUAVE -->
        <div class="bg-sky-50/70 border border-sky-200/80 rounded-2xl p-5">
          <div class="flex items-center gap-2 text-sm font-bold text-slate-800">
            <span>💡</span>
            <span>Como funciona</span>
          </div>
          <p class="text-xs text-slate-700 mt-2 leading-relaxed">
            O sistema monitora quantas mensagens esta sessão envia dentro de uma janela de tempo. Se o número de mensagens exceder o limite dentro da janela de tempo, cada mensagem subsequente será atrasada por um valor aleatório entre o atraso mínimo e máximo. Assim que a janela de tempo é reiniciada, as mensagens voltam à velocidade normal.
          </p>
        </div>

        <!-- INPUTS DE CONFIGURAÇÃO -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 pt-2">
          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Limite de mensagens</label>
            <input id="abLimite" type="number" min="1" value="5"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
            <p class="text-xs text-slate-500 mt-1.5">
              Número de mensagens permitidas na velocidade máxima antes do início do atraso. <span class="text-orange-600 font-medium">Menor = mais seguro, porém mais lento.</span>
            </p>
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Janela de tempo (segundos)</label>
            <input id="abJanela" type="number" min="1" value="10"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
            <p class="text-xs text-slate-500 mt-1.5">
              A janela contínua para contar mensagens. Após esse tempo, o contador é reiniciado naturalmente. <span class="text-orange-600 font-medium">Mais longo = mais conservador.</span>
            </p>
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Atraso Mínimo (ms)</label>
            <input id="abMinMs" type="number" min="0" step="100" value="1000"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
            <p class="text-xs text-slate-500 mt-1.5">
              Atraso aleatório mínimo aplicado. 1000ms = 1 segundo.
            </p>
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Atraso Máximo (ms)</label>
            <input id="abMaxMs" type="number" min="0" step="100" value="3000"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
            <p class="text-xs text-slate-500 mt-1.5">
              Atraso aleatório máximo aplicado. 3000ms = 3 segundos.
            </p>
          </div>
        </div>

        <!-- ALERTA RECOMENDAÇÃO -->
        <div class="bg-amber-50/80 border border-amber-200 rounded-xl px-4 py-3 text-xs text-slate-700 flex items-center gap-2">
          <span>⚠️</span>
          <span>Configurações seguras recomendadas: Limite 5, Janela 10s, Atraso 1000–3000ms. Para transmissões de grande volume, use Limite 3 com Atraso 2000–5000ms.</span>
        </div>

        <div class="flex items-center gap-4">
          <button onclick="salvarConfigAntiBan()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-6 py-3 rounded-xl text-sm shadow-sm transition inline-flex items-center gap-2">
            <span>💾</span>
            <span>Salvar configurações de proteção</span>
          </button>
          <span id="msgSalvoAntiban" class="hidden text-xs font-semibold text-emerald-700">Configurações salvas com sucesso!</span>
        </div>
      </div>
    </div>

    <!-- =================================================================== -->
    <!-- ABA 4: GESTÃO DE PERFIS / DEPARTAMENTOS                             -->
    <!-- =================================================================== -->
    <div id="abaPerfis" class="hidden flex-1 overflow-y-auto p-8">
      <div class="max-w-5xl mx-auto space-y-6">
        <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200">
          <h2 class="text-base font-bold text-slate-800">Novo Perfil / Departamento</h2>
          <p class="text-xs text-slate-500 mt-0.5 mb-4">Os perfis aparecem automaticamente no menu inicial do WhatsApp.</p>
          <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
            <input id="perfCodigo" type="text" placeholder="Código (ex: FINANCEIRO)" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm uppercase" />
            <input id="perfNome" type="text" placeholder="Nome (ex: Financeiro)" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm" />
            <input id="perfOpcao" type="text" placeholder="Tecla Menu (ex: 3)" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm" />
            <button onclick="criarPerfil()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded-xl py-2.5 text-sm transition">
              + Adicionar Perfil
            </button>
          </div>
        </div>

        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
          <table class="w-full text-left text-sm">
            <thead class="bg-slate-50 border-b border-slate-200 text-xs uppercase text-slate-500">
              <tr>
                <th class="px-5 py-3.5">Tecla Bot</th>
                <th class="px-5 py-3.5">Identificador</th>
                <th class="px-5 py-3.5">Nome do Departamento</th>
                <th class="px-5 py-3.5 text-right">Ações</th>
              </tr>
            </thead>
            <tbody id="tabelaPerfis" class="divide-y divide-slate-100"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- =================================================================== -->
    <!-- ABA 5: GESTÃO DE USUÁRIOS E VÍNCULOS                                -->
    <!-- =================================================================== -->
    <div id="abaUsuarios" class="hidden flex-1 overflow-y-auto p-8">
      <div class="max-w-5xl mx-auto space-y-6">
        <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200">
          <h2 class="text-base font-bold text-slate-800">Cadastrar Operador e Vincular Perfil</h2>
          <div class="grid grid-cols-1 md:grid-cols-5 gap-3 mt-4">
            <input id="userNome" type="text" placeholder="Nome completo" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm" />
            <input id="userEmail" type="email" placeholder="E-mail de login" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm" />
            <input id="userSenha" type="password" placeholder="Senha" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm" />
            <select id="userPerfilSelect" class="border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm bg-white"></select>
            <button onclick="criarUsuario()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded-xl py-2.5 text-sm transition">
              + Criar Acesso
            </button>
          </div>
        </div>

        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
          <table class="w-full text-left text-sm">
            <thead class="bg-slate-50 border-b border-slate-200 text-xs uppercase text-slate-500">
              <tr>
                <th class="px-5 py-3.5">Operador</th>
                <th class="px-5 py-3.5">E-mail</th>
                <th class="px-5 py-3.5">Nível</th>
                <th class="px-5 py-3.5">Perfil Vinculado</th>
                <th class="px-5 py-3.5 text-right">Ações</th>
              </tr>
            </thead>
            <tbody id="tabelaUsuarios" class="divide-y divide-slate-100"></tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

  <script>
    let TOKEN = localStorage.getItem("tottem_token") || "";
    let USUARIO = JSON.parse(localStorage.getItem("tottem_user") || "null");
    let PERFIS = [];
    let CHATS_CACHE = [];
    let LEADS_CACHE = [];
    let JID_ATUAL = null;
    let intervalo = null;
    let mediaRecorder = null;
    let audioChunks = [];

    if (TOKEN && USUARIO) {
      iniciarPainel();
    }

    function authHeaders(json = true) {
      const h = { "Authorization": "Bearer " + TOKEN };
      if (json) h["Content-Type"] = "application/json";
      return h;
    }

    function iniciais(nome) {
      if (!nome) return "CL";
      const partes = nome.trim().split(" ");
      return (partes[0][0] + (partes[1] ? partes[1][0] : "")).toUpperCase();
    }

    async function fazerLogin() {
      const email = document.getElementById("loginEmail").value;
      const senha = document.getElementById("loginSenha").value;
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, senha })
      });
      if (res.ok) {
        const data = await res.json();
        TOKEN = data.token;
        USUARIO = data.usuario;
        localStorage.setItem("tottem_token", TOKEN);
        localStorage.setItem("tottem_user", JSON.stringify(USUARIO));
        iniciarPainel();
      } else {
        document.getElementById("loginErro").classList.remove("hidden");
      }
    }

    function sair() {
      localStorage.clear();
      location.reload();
    }

    async function iniciarPainel() {
      document.getElementById("loginModal").classList.add("hidden");
      document.getElementById("badgeSetorAtual").innerText = USUARIO.perfil_nome;
      document.getElementById("nomeOperadorTop").innerText = USUARIO.nome;
      document.getElementById("avatarOperador").innerText = iniciais(USUARIO.nome);

      if (USUARIO.role === "ADMIN") {
        ["railCrm", "railAntiban", "railPerfis", "railUsuarios", "filtroAdminSetor"].forEach(id => {
          document.getElementById(id).classList.remove("hidden");
        });
      }

      await carregarPerfis();
      atualizarFila();
      if (intervalo) clearInterval(intervalo);
      intervalo = setInterval(() => {
        atualizarFila();
        if (JID_ATUAL) carregarMensagens(JID_ATUAL, false);
        if (USUARIO.role === "ADMIN") verificarProgressoCampanha();
      }, 3000);
    }

    function mudarAba(aba) {
      ["abaAtendimento", "abaCrm", "abaAntiban", "abaPerfis", "abaUsuarios"].forEach(id => document.getElementById(id).classList.add("hidden"));
      ["railAtendimento", "railCrm", "railAntiban", "railPerfis", "railUsuarios"].forEach(id => {
        const el = document.getElementById(id);
        el.classList.remove("bg-slate-800", "text-white");
        el.classList.add("text-slate-400");
      });

      const mapa = {
        atendimento: ["abaAtendimento", "railAtendimento", "Atendimentos"],
        crm: ["abaCrm", "railCrm", "CRM de Leads & Disparos em Massa"],
        antiban: ["abaAntiban", "railAntiban", "Proteção Anti-Banimento"],
        perfis: ["abaPerfis", "railPerfis", "Perfis & Departamentos"],
        usuarios: ["abaUsuarios", "railUsuarios", "Equipe & Vínculos"],
      };
      const [divId, railId, titulo] = mapa[aba];
      document.getElementById(divId).classList.remove("hidden");
      document.getElementById(railId).classList.add("bg-slate-800", "text-white");
      document.getElementById("tituloPagina").innerText = titulo;

      if (aba === "crm") {
        carregarLeads();
        carregarTemplates();
      } else if (aba === "antiban") {
        carregarConfigAntiBan();
      } else if (aba === "perfis") {
        carregarPerfis();
      } else if (aba === "usuarios") {
        carregarUsuarios();
      }
    }

    // --- PERFIS E USUÁRIOS ---
    async function carregarPerfis() {
      const res = await fetch("/api/admin/perfis", { headers: authHeaders() });
      if (!res.ok) return;
      PERFIS = await res.json();

      const optsSetor = PERFIS.map(p => `<option value="${p.codigo}">${p.nome}</option>`).join("");
      document.getElementById("selectTransferir").innerHTML = optsSetor;
      document.getElementById("selectSetorDisparo").innerHTML = optsSetor;
      document.getElementById("filtroAdminSetor").innerHTML = `<option value="TODOS">Todos os Departamentos</option>` + optsSetor;
      document.getElementById("userPerfilSelect").innerHTML = `<option value="">-- Admin (Acesso Global) --</option>` +
        PERFIS.map(p => `<option value="${p.id}">${p.nome}</option>`).join("");

      document.getElementById("tabelaPerfis").innerHTML = PERFIS.map(p => `
        <tr class="hover:bg-slate-50">
          <td class="px-5 py-3.5"><span class="w-7 h-7 rounded-lg bg-emerald-100 text-emerald-800 font-bold inline-flex items-center justify-center text-xs">${p.opcao_menu}</span></td>
          <td class="px-5 py-3.5"><span class="bg-slate-100 border border-slate-200 px-2.5 py-1 rounded-md text-xs font-mono font-semibold text-slate-700">${p.codigo}</span></td>
          <td class="px-5 py-3.5 font-medium text-slate-800">${p.nome}</td>
          <td class="px-5 py-3.5 text-right">
            <button onclick="excluirPerfil(${p.id})" class="text-rose-600 hover:bg-rose-50 px-3 py-1 rounded-lg text-xs font-medium">Remover</button>
          </td>
        </tr>
      `).join("");
    }

    async function criarPerfil() {
      const codigo = document.getElementById("perfCodigo").value;
      const nome = document.getElementById("perfNome").value;
      const opcao_menu = document.getElementById("perfOpcao").value;
      if (!codigo || !nome || !opcao_menu) return alert("Preencha todos os campos.");
      const res = await fetch("/api/admin/perfis", {
        method: "POST", headers: authHeaders(), body: JSON.stringify({ codigo, nome, opcao_menu })
      });
      if (res.ok) {
        document.getElementById("perfCodigo").value = "";
        document.getElementById("perfNome").value = "";
        document.getElementById("perfOpcao").value = "";
        carregarPerfis();
      }
    }

    async function excluirPerfil(id) {
      if (!confirm("Remover este perfil?")) return;
      await fetch(`/api/admin/perfis/${id}`, { method: "DELETE", headers: authHeaders() });
      carregarPerfis();
    }

    async function carregarUsuarios() {
      const res = await fetch("/api/admin/usuarios", { headers: authHeaders() });
      if (!res.ok) return;
      const users = await res.json();
      document.getElementById("tabelaUsuarios").innerHTML = users.map(u => `
        <tr class="hover:bg-slate-50">
          <td class="px-5 py-3.5 font-semibold text-slate-800">${u.nome}</td>
          <td class="px-5 py-3.5 text-slate-500">${u.email}</td>
          <td class="px-5 py-3.5"><span class="text-[11px] font-bold px-2.5 py-0.5 rounded-full ${u.role==='ADMIN' ? 'bg-purple-100 text-purple-800' : 'bg-blue-100 text-blue-800'}">${u.role}</span></td>
          <td class="px-5 py-3.5">
            <select onchange="alterarVinculoUsuario(${u.id}, '${u.nome}', '${u.email}', this.value)" class="border border-slate-300 rounded-lg px-2.5 py-1.5 text-xs bg-white font-medium">
              <option value="" ${!u.perfil_id ? 'selected' : ''}>Supervisão / Admin (Global)</option>
              ${PERFIS.map(p => `<option value="${p.id}" ${u.perfil_id === p.id ? 'selected' : ''}>${p.nome}</option>`).join("")}
            </select>
          </td>
          <td class="px-5 py-3.5 text-right">
            <button onclick="excluirUsuario(${u.id})" class="text-rose-600 hover:bg-rose-50 px-3 py-1 rounded-lg text-xs font-medium">Excluir</button>
          </td>
        </tr>
      `).join("");
    }

    async function criarUsuario() {
      const nome = document.getElementById("userNome").value;
      const email = document.getElementById("userEmail").value;
      const senha = document.getElementById("userSenha").value;
      const perfilVal = document.getElementById("userPerfilSelect").value;
      if (!nome || !email || !senha) return alert("Preencha nome, e-mail e senha.");
      const res = await fetch("/api/admin/usuarios", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ nome, email, senha, role: perfilVal ? "ATENDENTE" : "ADMIN", perfil_id: perfilVal ? parseInt(perfilVal) : null })
      });
      if (res.ok) {
        document.getElementById("userNome").value = "";
        document.getElementById("userEmail").value = "";
        document.getElementById("userSenha").value = "";
        carregarUsuarios();
      }
    }

    async function alterarVinculoUsuario(id, nome, email, perfilVal) {
      await fetch(`/api/admin/usuarios/${id}/vincular`, {
        method: "PUT", headers: authHeaders(),
        body: JSON.stringify({ nome, email, role: perfilVal ? "ATENDENTE" : "ADMIN", perfil_id: perfilVal ? parseInt(perfilVal) : null })
      });
      carregarUsuarios();
    }

    async function excluirUsuario(id) {
      if (!confirm("Excluir este usuário?")) return;
      await fetch(`/api/admin/usuarios/${id}`, { method: "DELETE", headers: authHeaders() });
      carregarUsuarios();
    }

    // --- CRM LEADS, TEMPLATES E DISPARO ---
    async function importarPlanilha() {
      const input = document.getElementById("inputPlanilhaLeads");
      if (!input.files.length) return alert("Selecione uma planilha .xlsx ou .csv");
      const formData = new FormData();
      formData.append("arquivo", input.files[0]);

      const res = await fetch("/api/admin/leads/importar", {
        method: "POST", headers: authHeaders(false), body: formData
      });
      if (res.ok) {
        const data = await res.json();
        const resumo = document.getElementById("resumoImportacao");
        resumo.innerText = `✅ Importados: ${data.importados} contatos | Ignorados (sem telefone válido): ${data.ignorados}`;
        resumo.classList.remove("hidden");
        input.value = "";
        carregarLeads();
      } else {
        alert("Erro ao processar planilha.");
      }
    }

    async function carregarLeads() {
      const res = await fetch("/api/admin/leads", { headers: authHeaders() });
      if (!res.ok) return;
      LEADS_CACHE = await res.json();
      renderizarTabelaLeads();
    }

    function renderizarTabelaLeads() {
      const termo = (document.getElementById("buscaLeadsCrm").value || "").toLowerCase();
      const filtrados = LEADS_CACHE.filter(l =>
        (l.cnpj || "").toLowerCase().includes(termo) ||
        (l.razao_social || "").toLowerCase().includes(termo) ||
        (l.nome || "").toLowerCase().includes(termo) ||
        (l.celular || "").includes(termo) ||
        (l.telefone || "").includes(termo)
      );
      document.getElementById("totalLeadsBadge").innerText = filtrados.length;

      const corStatus = {
        NOVO: "bg-sky-100 text-sky-800",
        CONTATADO: "bg-amber-100 text-amber-800",
        EM_ATENDIMENTO: "bg-emerald-100 text-emerald-800",
        FALHA_ENVIO: "bg-rose-100 text-rose-800",
      };

      document.getElementById("tabelaLeadsBody").innerHTML = filtrados.map(l => `
        <tr class="hover:bg-slate-50">
          <td class="px-4 py-3 font-mono text-slate-600">${l.cnpj || '-'}</td>
          <td class="px-4 py-3 font-semibold text-slate-800">${l.razao_social || '-'}</td>
          <td class="px-4 py-3 text-slate-700">${l.nome || '-'}</td>
          <td class="px-4 py-3 font-mono text-slate-700">${l.celular || l.telefone}</td>
          <td class="px-4 py-3"><span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold ${corStatus[l.status_lead] || 'bg-slate-100 text-slate-700'}">${l.status_lead}</span></td>
          <td class="px-4 py-3 text-right space-x-2">
            <button onclick="abrirConversaDoLead(${l.id})" class="bg-emerald-50 hover:bg-emerald-600 text-emerald-700 hover:text-white border border-emerald-200 px-2.5 py-1 rounded-lg font-semibold transition">
              Abrir Chat
            </button>
            <button onclick="excluirLead(${l.id})" class="text-rose-600 hover:underline">Excluir</button>
          </td>
        </tr>
      `).join("");
    }

    async function abrirConversaDoLead(leadId) {
      const setor = document.getElementById("selectSetorDisparo").value || "COMERCIAL";
      const res = await fetch("/api/admin/leads/abrir-conversa", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ lead_id: leadId, setor_destino: setor })
      });
      if (res.ok) {
        const data = await res.json();
        mudarAba("atendimento");
        await atualizarFila();
        selecionarChat(data.jid);
      }
    }

    async function excluirLead(id) {
      if (!confirm("Remover este lead do CRM?")) return;
      await fetch(`/api/admin/leads/${id}`, { method: "DELETE", headers: authHeaders() });
      carregarLeads();
    }

    async function carregarTemplates() {
      const res = await fetch("/api/admin/templates", { headers: authHeaders() });
      if (!res.ok) return;
      const tpls = await res.json();
      document.getElementById("selectTemplateDisparo").innerHTML = tpls.map(t =>
        `<option value="${t.id}">${t.titulo}</option>`
      ).join("");
    }

    async function salvarTemplate() {
      const titulo = document.getElementById("tplTitulo").value;
      const conteudo = document.getElementById("tplConteudo").value;
      if (!titulo || !conteudo) return alert("Informe título e mensagem.");
      const res = await fetch("/api/admin/templates", {
        method: "POST", headers: authHeaders(), body: JSON.stringify({ titulo, conteudo })
      });
      if (res.ok) {
        document.getElementById("tplTitulo").value = "";
        document.getElementById("tplConteudo").value = "";
        carregarTemplates();
        alert("Mensagem cadastrada com sucesso!");
      }
    }

    async function iniciarDisparoMassa() {
      const template_id = parseInt(document.getElementById("selectTemplateDisparo").value);
      const setor_destino = document.getElementById("selectSetorDisparo").value;
      const apenas_novos = document.getElementById("chkApenasNovos").checked;
      if (!template_id) return alert("Cadastre ou selecione uma mensagem primeiro.");
      if (!confirm("Deseja iniciar o envio em massa respeitando as regras Anti-Banimento?")) return;

      const res = await fetch("/api/admin/disparo-massa", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ template_id, setor_destino, apenas_novos })
      });
      if (res.ok) {
        verificarProgressoCampanha();
      } else {
        const err = await res.json();
        alert(err.detail || "Erro ao iniciar disparo.");
      }
    }

    async function verificarProgressoCampanha() {
      const res = await fetch("/api/admin/disparo-massa/status", { headers: authHeaders() });
      if (!res.ok) return;
      const st = await res.json();
      const box = document.getElementById("boxProgressoCampanha");
      if (st.total > 0) {
        box.classList.remove("hidden");
        box.innerHTML = `<strong>${st.mensagem_status}</strong><br>Progresso: ${st.enviados + st.falhas}/${st.total} (Falhas: ${st.falhas})`;
      }
    }

    // --- ANTI-BANIMENTO ---
    async function carregarConfigAntiBan() {
      const res = await fetch("/api/admin/antiban", { headers: authHeaders() });
      if (!res.ok) return;
      const cfg = await res.json();
      document.getElementById("abHabilitado").checked = !!cfg.habilitado;
      document.getElementById("abLimite").value = cfg.limite_mensagens;
      document.getElementById("abJanela").value = cfg.janela_segundos;
      document.getElementById("abMinMs").value = cfg.atraso_min_ms;
      document.getElementById("abMaxMs").value = cfg.atraso_max_ms;
    }

    async function salvarConfigAntiBan() {
      const payload = {
        habilitado: document.getElementById("abHabilitado").checked,
        limite_mensagens: parseInt(document.getElementById("abLimite").value) || 5,
        janela_segundos: parseInt(document.getElementById("abJanela").value) || 10,
        atraso_min_ms: parseInt(document.getElementById("abMinMs").value) || 1000,
        atraso_max_ms: parseInt(document.getElementById("abMaxMs").value) || 3000,
      };
      const res = await fetch("/api/admin/antiban", {
        method: "POST", headers: authHeaders(), body: JSON.stringify(payload)
      });
      if (res.ok) {
        const msg = document.getElementById("msgSalvoAntiban");
        msg.classList.remove("hidden");
        setTimeout(() => msg.classList.add("hidden"), 3500);
      }
    }

    // --- ATENDIMENTO ---
    async function atualizarFila() {
      const filtro = document.getElementById("filtroAdminSetor").value || "TODOS";
      const res = await fetch(`/api/painel/chats?filtro_setor=${filtro}`, { headers: authHeaders() });
      if (!res.ok) return;
      CHATS_CACHE = await res.json();
      renderizarListaChats();
    }

    function renderizarListaChats() {
      const termo = (document.getElementById("buscaContato").value || "").toLowerCase();
      const filtrados = CHATS_CACHE.filter(c =>
        (c.nome || "").toLowerCase().includes(termo) ||
        (c.razao_social || "").toLowerCase().includes(termo) ||
        (c.cnpj || "").includes(termo) ||
        (c.jid || "").includes(termo)
      );
      document.getElementById("contadorChats").innerText = filtrados.length;

      document.getElementById("listaChats").innerHTML = filtrados.map(c => {
        const ativo = JID_ATUAL === c.jid;
        return `
          <div onclick="selecionarChat('${c.jid}')"
               class="p-3.5 cursor-pointer transition flex items-start gap-3 ${ativo ? 'bg-emerald-50/80 border-l-4 border-emerald-600' : 'hover:bg-slate-50'}">
            <div class="w-10 h-10 rounded-full bg-slate-800 text-white font-bold text-xs flex items-center justify-center shrink-0">
              ${iniciais(c.nome || c.razao_social || c.jid)}
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex justify-between items-center">
                <span class="font-semibold text-slate-800 text-xs truncate">${c.nome || c.razao_social || c.jid.split('@')[0]}</span>
                <span class="text-[10px] text-slate-400">${c.hora_fmt || ''}</span>
              </div>
              <p class="text-xs text-slate-500 truncate mt-0.5">${c.ultima_mensagem || ''}</p>
              <div class="flex items-center justify-between mt-1.5">
                <span class="text-[10px] font-bold px-2 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200">${c.setor}</span>
                ${c.nao_lidas > 0 ? `<span class="bg-emerald-600 text-white text-[10px] font-bold px-1.5 py-0.5 rounded-full">${c.nao_lidas}</span>` : ''}
              </div>
            </div>
          </div>
        `;
      }).join("");
    }

    function selecionarChat(jid) {
      JID_ATUAL = jid;
      const chat = CHATS_CACHE.find(c => c.jid === jid) || {};
      document.getElementById("chatVazio").classList.add("hidden");
      document.getElementById("chatAtivo").classList.remove("hidden");
      document.getElementById("painelDireito").classList.remove("hidden");

      const nome = chat.nome || chat.razao_social || jid.split("@")[0];
      document.getElementById("contatoNome").innerText = nome;
      document.getElementById("contatoNumero").innerText = "+" + jid.split("@")[0];
      document.getElementById("avatarClienteChat").innerText = iniciais(nome);
      document.getElementById("badgeSetorChat").innerText = chat.setor || "ATENDIMENTO";
      document.getElementById("notasCrmInput").value = chat.notas_crm || "";

      const blocoEmp = document.getElementById("blocoDadosEmpresa");
      if (chat.cnpj || chat.razao_social) {
        blocoEmp.classList.remove("hidden");
        document.getElementById("crmRazaoSocial").innerText = chat.razao_social || nome;
        document.getElementById("crmCnpj").innerText = chat.cnpj ? `CNPJ: ${chat.cnpj}` : "";
      } else {
        blocoEmp.classList.add("hidden");
      }

      carregarMensagens(jid, true);
      renderizarListaChats();
    }

    async function salvarNotas() {
      if (!JID_ATUAL) return;
      const notas = document.getElementById("notasCrmInput").value;
      await fetch("/api/painel/notas", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL, notas_crm: notas })
      });
      atualizarFila();
    }

    function renderizarMidia(m) {
      if (!m.url_midia) return "";
      const tipo = (m.tipo_midia || "").toUpperCase();
      if (tipo === "IMAGE" || tipo === "STICKER") {
        return `<img src="${m.url_midia}" onclick="window.open('${m.url_midia}')" class="max-w-xs rounded-lg my-1.5 border cursor-pointer" />`;
      }
      if (tipo === "AUDIO" || tipo === "VOICE") {
        return `<audio controls src="${m.url_midia}" class="my-1.5 max-w-xs h-9"></audio>`;
      }
      if (tipo === "VIDEO") {
        return `<video controls src="${m.url_midia}" class="max-w-xs rounded-lg my-1.5"></video>`;
      }
      return `<a href="${m.url_midia}" target="_blank" class="inline-flex items-center gap-1.5 my-1.5 text-xs bg-slate-100 border border-slate-300 px-3 py-2 rounded-lg font-semibold text-slate-800">📄 Baixar Documento</a>`;
    }

    async function carregarMensagens(jid, rolarFim) {
      const res = await fetch(`/api/painel/mensagens/${encodeURIComponent(jid)}`, { headers: authHeaders() });
      if (!res.ok) return;
      const msgs = await res.json();
      const area = document.getElementById("areaMensagens");
      area.innerHTML = msgs.map(m => {
        const isCliente = m.remetente === "CLIENTE";
        const isBot = m.remetente === "BOT";
        return `
          <div class="flex ${isCliente ? 'justify-start' : 'justify-end'}">
            <div class="max-w-md rounded-2xl px-4 py-2.5 shadow-sm text-sm ${isCliente ? 'bg-white text-slate-800 rounded-tl-none' : (isBot ? 'bg-slate-200/90 text-slate-700' : 'bg-[#d9fdd3] text-slate-900 rounded-tr-none')}">
              <div class="text-[10px] font-bold ${isCliente ? 'text-emerald-700' : 'text-slate-500'} mb-0.5">${m.remetente}</div>
              ${renderizarMidia(m)}
              <div class="whitespace-pre-line leading-relaxed">${m.conteudo}</div>
              <div class="text-[10px] text-right text-slate-400 mt-1">${m.hora}</div>
            </div>
          </div>
        `;
      }).join("");
      if (rolarFim) area.scrollTop = area.scrollHeight;
    }

    async function enviarTexto(e) {
      e.preventDefault();
      const input = document.getElementById("inputMensagem");
      const texto = input.value.trim();
      if (!texto || !JID_ATUAL) return;
      input.value = "";
      await fetch("/api/painel/enviar", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL, texto })
      });
      carregarMensagens(JID_ATUAL, true);
      atualizarFila();
    }

    async function enviarAnexoSelecionado() {
      const input = document.getElementById("inputArquivo");
      if (!input.files.length || !JID_ATUAL) return;
      const file = input.files[0];
      let tipo = "document";
      if (file.type.startsWith("image/")) tipo = "image";
      else if (file.type.startsWith("audio/")) tipo = "audio";
      else if (file.type.startsWith("video/")) tipo = "video";

      const formData = new FormData();
      formData.append("jid", JID_ATUAL);
      formData.append("tipo", tipo);
      formData.append("legenda", "");
      formData.append("arquivo", file);
      input.value = "";

      await fetch("/api/painel/enviar-midia", {
        method: "POST", headers: authHeaders(false), body: formData
      });
      carregarMensagens(JID_ATUAL, true);
      atualizarFila();
    }

    async function alternarGravacaoAudio() {
      const btn = document.getElementById("btnGravarAudio");
      const status = document.getElementById("statusGravacao");
      if (!mediaRecorder || mediaRecorder.state === "inactive") {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          mediaRecorder = new MediaRecorder(stream);
          audioChunks = [];
          mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
          mediaRecorder.onstop = async () => {
            const blob = new Blob(audioChunks, { type: "audio/webm" });
            const formData = new FormData();
            formData.append("jid", JID_ATUAL);
            formData.append("tipo", "voice");
            formData.append("legenda", "");
            formData.append("arquivo", blob, "voz.webm");

            await fetch("/api/painel/enviar-midia", {
              method: "POST", headers: authHeaders(false), body: formData
            });
            carregarMensagens(JID_ATUAL, true);
            atualizarFila();
          };
          mediaRecorder.start();
          btn.classList.replace("bg-slate-100", "bg-rose-600");
          btn.classList.replace("text-slate-600", "text-white");
          status.classList.remove("hidden");
        } catch (err) {
          alert("Permita o acesso ao microfone.");
        }
      } else {
        mediaRecorder.stop();
        btn.classList.replace("bg-rose-600", "bg-slate-100");
        btn.classList.replace("text-white", "text-slate-600");
        status.classList.add("hidden");
      }
    }

    async function transferir() {
      const destino = document.getElementById("selectTransferir").value;
      if (!JID_ATUAL || !destino) return;
      await fetch("/api/painel/transferir", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL, novo_setor: destino })
      });
      JID_ATUAL = null;
      document.getElementById("chatAtivo").classList.add("hidden");
      document.getElementById("painelDireito").classList.add("hidden");
      document.getElementById("chatVazio").classList.remove("hidden");
      atualizarFila();
    }

    async function encerrar() {
      if (!JID_ATUAL || !confirm("Deseja encerrar este atendimento?")) return;
      await fetch("/api/painel/encerrar", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL })
      });
      JID_ATUAL = null;
      document.getElementById("chatAtivo").classList.add("hidden");
      document.getElementById("painelDireito").classList.add("hidden");
      document.getElementById("chatVazio").classList.remove("hidden");
      atualizarFila();
    }
  </script>
</body>
</html>
"""


@app.get("/painel", response_class=HTMLResponse)
async def abrir_painel():
    return HTML_PAINEL
