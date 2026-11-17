import os
import ssl
import uuid
import hashlib
import secrets
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import httpx
import pymysql
import imageio_ffmpeg
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tottem-atendimento")

app = FastAPI(title="Tottem Atendimento Pro — Multi-Departamento")

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

# Diretório local para cache/armazenamento de mídias recebidas e enviadas
MEDIA_DIR = Path("media_files")
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")


# ==============================================================================
# UTILITÁRIOS DE SEGURANÇA E CONVERSÃO DE ÁUDIO
# ==============================================================================
def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def converter_para_mp3(origem: Path) -> Path:
    """Converte áudio WebM/OGG do navegador para MP3 compatível com WhatsApp iOS/Android."""
    destino_mp3 = origem.with_suffix(".mp3")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y", "-i", str(origem),
        "-vn", "-ar", "44100", "-ac", "1", "-b:a", "64k",
        str(destino_mp3)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return destino_mp3


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
            # 1. Tabela de Perfis / Departamentos
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

            # 2. Tabela de Usuários e Vínculo com Perfil
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

            # 3. Tabela de Atendimentos (Tickets)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS atendimentos (
                    jid VARCHAR(100) PRIMARY KEY,
                    nome VARCHAR(150) DEFAULT '',
                    setor VARCHAR(30) DEFAULT 'TRIAGEM',
                    status VARCHAR(30) DEFAULT 'TRIAGEM',
                    ultima_mensagem TEXT,
                    nao_lidas INT DEFAULT 0,
                    atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 4. Tabela de Mensagens (com suporte a mídia)
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

            # Migração segura caso as colunas novas ainda não existam
            try:
                cur.execute("ALTER TABLE mensagens_atendimento ADD COLUMN tipo_midia VARCHAR(20) DEFAULT 'TEXT'")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE mensagens_atendimento ADD COLUMN url_midia VARCHAR(255) NULL")
            except Exception:
                pass

            # Perfis padrão iniciais (Suporte e Comercial)
            cur.execute("""
                INSERT IGNORE INTO perfis_atendimento (codigo, nome, opcao_menu, descricao)
                VALUES
                    ('SUPORTE', 'Suporte Técnico', '1', 'Atendimento técnico e chamados'),
                    ('COMERCIAL', 'Comercial e Orçamentos', '2', 'Vendas, propostas e licitações')
            """)

            # Usuário Administrador inicial
            cur.execute("SELECT id FROM usuarios_painel WHERE email = %s", (ADMIN_EMAIL,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO usuarios_painel (nome, email, senha_hash, role, perfil_id)
                    VALUES (%s, %s, %s, 'ADMIN', NULL)
                    """,
                    ("Administrador", ADMIN_EMAIL, hash_senha(ADMIN_SENHA)),
                )
        logger.info("Banco de dados sincronizado com sucesso.")
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    if DATABASE_URL:
        init_db()


# ==============================================================================
# INTEGRAÇÃO WA-AKG (TEXTO, DOWNLOAD DE MÍDIA E ENVIO DE MÍDIA)
# ==============================================================================
async def enviar_texto_wa(jid: str, texto: str) -> bool:
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
    url = f"{WA_BASE_URL}/messages/{SESSION_ID}/{jid}/media"
    mime_map = {
        "audio": "audio/mpeg",
        "voice": "audio/mpeg",
        "image": "image/jpeg",
        "video": "video/mp4",
        "document": "application/pdf",
    }
    mime = mime_map.get(media_type, "application/octet-stream")

    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            with open(file_path, "rb") as f:
                files = {"file": (filename, f, mime)}
                tipo_api = "audio" if media_type in {"audio", "voice"} else media_type
                data = {"type": tipo_api, "caption": caption}
                resp = await client.post(url, headers=HEADERS_WA, files=files, data=data)
                logger.info(f"Envio Mídia WA ({tipo_api}) -> {jid} | Status: {resp.status_code} | Resp: {resp.text}")
                return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Erro envio mídia WA: {e}")
            return False


async def baixar_midia_wa(data: Dict[str, Any], key: Dict[str, Any], tipo: str) -> Optional[str]:
    """Baixa mídia usando fileUrl direto do payload ou a rota /download/{messageId}/media."""
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
                logger.info(f"Tentativa download mídia ({url}) -> Status: {resp.status_code} | Bytes: {len(resp.content)}")
                if resp.status_code == 200 and len(resp.content) > 100:
                    nome_arquivo = f"in_{uuid.uuid4().hex}{ext}"
                    destino = MEDIA_DIR / nome_arquivo
                    destino.write_bytes(resp.content)
                    return f"/media/{nome_arquivo}"
            except Exception as e:
                logger.error(f"Erro ao baixar mídia de {url}: {e}")

    return None


def registrar_mensagem(
    jid: str,
    remetente: str,
    conteudo: str,
    nome: str = "",
    incrementar_nao_lidas: bool = False,
    tipo_midia: str = "TEXT",
    url_midia: Optional[str] = None,
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
            cur.execute("""
                INSERT INTO atendimentos (jid, nome, ultima_mensagem, nao_lidas, atualizado_em)
                VALUES (%s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    nome = IF(%s != '', %s, nome),
                    ultima_mensagem = %s,
                    nao_lidas = nao_lidas + %s,
                    atualizado_em = NOW()
            """, (jid, nome, resumo, inc, nome, nome, resumo, inc))
    finally:
        conn.close()


# ==============================================================================
# WEBHOOK DE RECEBIMENTO (TEXTO, ÁUDIO E ANEXOS + MENU DINÂMICO)
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
                texto = f"Arquivo recebido ({msg_type})"
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
        finally:
            conn.close()

        status_atual = atendimento["status"] if atendimento else "TRIAGEM"

        registrar_mensagem(
            jid=jid,
            remetente="CLIENTE",
            conteudo=texto,
            nome=push_name,
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
                f"Olá, *{push_name}*! Bem-vindo à *Tottem Tecnologia*.\n\n"
                f"Responda apenas com o número do departamento desejado:\n{linhas_opcoes}"
            )
            await enviar_texto_wa(jid, menu)
            registrar_mensagem(jid, "BOT", menu)
    except Exception as e:
        logger.exception(f"Erro ao processar webhook: {e}")


# ==============================================================================
# AUTENTICAÇÃO E MIDDLEWARE DE USUÁRIO
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
                raise HTTPException(status_code=401, detail="Sessão expirada ou inválida")
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
                raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")

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


# ==============================================================================
# ROTAS ADMINISTRATIVAS: PERFIS E USUÁRIOS
# ==============================================================================
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
        raise HTTPException(status_code=400, detail="Código de perfil já existe.")
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
        raise HTTPException(status_code=400, detail="Senha obrigatória para novo usuário")

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
            if dados.senha:
                cur.execute(
                    """
                    UPDATE usuarios_painel
                    SET nome = %s, email = %s, role = %s, perfil_id = %s, senha_hash = %s
                    WHERE id = %s
                    """,
                    (dados.nome.strip(), dados.email.strip().lower(), dados.role, dados.perfil_id, hash_senha(dados.senha), usuario_id),
                )
            else:
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
        raise HTTPException(status_code=400, detail="Você não pode excluir seu próprio usuário.")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM usuarios_painel WHERE id = %s", (usuario_id,))
        return {"status": "excluido"}
    finally:
        conn.close()


# ==============================================================================
# ROTAS DE ATENDIMENTO (FILA ISOLADA POR PERFIL, TEXTO, ÁUDIO E ANEXOS)
# ==============================================================================
@app.get("/")
async def health_check():
    return {"status": "online", "service": "tottem-atendimento-pro"}


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
            if user["role"] == "ADMIN":
                if filtro_setor and filtro_setor != "TODOS":
                    cur.execute(
                        "SELECT * FROM atendimentos WHERE setor = %s AND status = 'EM_ATENDIMENTO' ORDER BY atualizado_em DESC",
                        (filtro_setor,),
                    )
                else:
                    cur.execute("SELECT * FROM atendimentos ORDER BY atualizado_em DESC LIMIT 100")
            else:
                setor_user = user["perfil_codigo"]
                if not setor_user:
                    return []
                cur.execute(
                    "SELECT * FROM atendimentos WHERE setor = %s AND status = 'EM_ATENDIMENTO' ORDER BY atualizado_em DESC",
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


@app.post("/api/painel/enviar")
async def responder_texto(dados: EnvioTextoReq, request: Request):
    user = obter_usuario_atual(request)
    sucesso = await enviar_texto_wa(dados.jid, dados.texto)
    if not sucesso:
        raise HTTPException(status_code=500, detail="Falha ao enviar mensagem pelo WA-AKG")

    assinatura = f"{user['nome']} ({user['perfil_codigo'] or 'ADMIN'})"
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

    if tipo in {"voice", "audio"} and ext.lower() not in {".mp3"}:
        try:
            caminho_mp3 = converter_para_mp3(caminho_salvo)
            caminho_salvo = caminho_mp3
            nome_salvo = caminho_mp3.name
        except Exception as e:
            logger.error(f"Erro na conversão FFmpeg para MP3: {e}")

    sucesso = await enviar_midia_wa(jid, caminho_salvo, nome_salvo, tipo, legenda)
    if not sucesso:
        raise HTTPException(status_code=500, detail="Falha ao enviar mídia pelo WA-AKG")

    assinatura = f"{user['nome']} ({user['perfil_codigo'] or 'ADMIN'})"
    texto_registro = legenda if legenda else f"Arquivo enviado: {nome_salvo}"
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
# INTERFACE WEB COMPLETA (/painel) — ATENDIMENTO + PERFIS + USUÁRIOS
# ==============================================================================
HTML_PAINEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tottem Atendimento Pro</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 h-screen flex flex-col font-sans">

  <!-- MODAL DE LOGIN -->
  <div id="loginModal" class="fixed inset-0 bg-slate-900/90 flex items-center justify-center z-50">
    <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md space-y-5">
      <div>
        <h1 class="text-2xl font-bold text-slate-800">Tottem Atendimento</h1>
        <p class="text-sm text-slate-500">Entre com seu usuário e senha corporativos.</p>
      </div>
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">E-mail</label>
        <input id="loginEmail" type="email" placeholder="seu@email.com" class="w-full border border-slate-300 rounded-lg p-2.5" />
      </div>
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">Senha</label>
        <input id="loginSenha" type="password" placeholder="••••••••" class="w-full border border-slate-300 rounded-lg p-2.5" />
      </div>
      <p id="loginErro" class="text-red-600 text-sm hidden">E-mail ou senha inválidos.</p>
      <button onclick="fazerLogin()" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2.5 rounded-lg transition">
        Entrar no Sistema
      </button>
    </div>
  </div>

  <!-- CABEÇALHO SUPERIOR -->
  <header class="bg-slate-900 text-white px-6 py-3 flex items-center justify-between shadow">
    <div class="flex items-center gap-4">
      <span class="font-bold text-lg tracking-tight">Tottem Atendimento</span>
      <span id="badgeUsuario" class="bg-emerald-600 text-xs font-semibold px-3 py-1 rounded-full"></span>
      <nav class="flex gap-2 ml-4">
        <button onclick="mudarAba('atendimento')" id="tabBtnAtendimento" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-700">💬 Atendimento</button>
        <button onclick="mudarAba('perfis')" id="tabBtnPerfis" class="hidden px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-slate-800">🏷️ Perfis / Setores</button>
        <button onclick="mudarAba('usuarios')" id="tabBtnUsuarios" class="hidden px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-slate-800">👥 Usuários e Vínculos</button>
      </nav>
    </div>
    <button onclick="sair()" class="text-xs bg-rose-600 hover:bg-rose-700 px-3 py-1.5 rounded-lg font-medium">Sair</button>
  </header>

  <!-- ABA 1: ATENDIMENTO -->
  <div id="abaAtendimento" class="flex flex-1 overflow-hidden">
    <aside class="w-80 bg-white border-r border-slate-200 flex flex-col">
      <div class="p-3 border-b border-slate-200 space-y-2">
        <div class="flex justify-between items-center">
          <span class="font-semibold text-slate-700 text-sm">Fila de Atendimento</span>
          <span id="contadorChats" class="text-xs bg-slate-200 text-slate-700 px-2 py-0.5 rounded-full">0</span>
        </div>
        <select id="filtroAdminSetor" onchange="atualizarFila()" class="hidden w-full text-xs border border-slate-300 rounded p-1.5 bg-slate-50">
          <option value="TODOS">Todos os Departamentos</option>
        </select>
      </div>
      <div id="listaChats" class="flex-1 overflow-y-auto divide-y divide-slate-100"></div>
    </aside>

    <main class="flex-1 flex flex-col bg-[#efeae2]">
      <div id="chatVazio" class="flex-1 flex items-center justify-center text-slate-500">
        Selecione um atendimento na fila à esquerda.
      </div>

      <div id="chatAtivo" class="hidden flex-1 flex flex-col h-full">
        <div class="bg-white px-6 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h2 id="contatoNome" class="font-bold text-slate-800"></h2>
            <p id="contatoNumero" class="text-xs text-slate-500"></p>
          </div>
          <div class="flex gap-2 items-center">
            <select id="selectTransferir" class="text-xs border border-slate-300 rounded-lg px-2 py-1.5 bg-white"></select>
            <button onclick="transferir()" class="text-xs bg-amber-500 hover:bg-amber-600 text-white font-medium px-3 py-1.5 rounded-lg">
              Transferir
            </button>
            <button onclick="encerrar()" class="text-xs bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-lg">
              Encerrar
            </button>
          </div>
        </div>

        <div id="areaMensagens" class="flex-1 overflow-y-auto p-6 space-y-3"></div>

        <!-- Barra de Envio: Anexo, Gravação de Áudio e Texto -->
        <div class="bg-white p-3 border-t border-slate-200 flex items-center gap-2">
          <input type="file" id="inputArquivo" class="hidden" onchange="enviarAnexoSelecionado()" />
          <button type="button" onclick="document.getElementById('inputArquivo').click()" title="Enviar Imagem, Documento ou Áudio"
                  class="p-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">
            📎
          </button>
          <button type="button" id="btnGravarAudio" onclick="alternarGravacaoAudio()" title="Gravar Áudio pelo Microfone"
                  class="p-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold">
            🎤
          </button>
          <form onsubmit="enviarTexto(event)" class="flex-1 flex gap-2">
            <input id="inputMensagem" type="text" placeholder="Digite sua mensagem..." autocomplete="off"
                   class="flex-1 border border-slate-300 rounded-xl px-4 py-2.5 focus:outline-none focus:border-emerald-500" />
            <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-6 py-2.5 rounded-xl">
              Enviar
            </button>
          </form>
        </div>
      </div>
    </main>
  </div>

  <!-- ABA 2: CADASTRO DE PERFIS / DEPARTAMENTOS -->
  <div id="abaPerfis" class="hidden flex-1 overflow-y-auto p-8 max-w-4xl mx-auto w-full space-y-6">
    <div class="bg-white p-6 rounded-xl shadow-sm border border-slate-200 space-y-4">
      <h2 class="text-lg font-bold text-slate-800">Cadastrar Novo Perfil / Departamento</h2>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
        <input id="perfCodigo" type="text" placeholder="Código (ex: FINANCEIRO)" class="border rounded-lg p-2.5 text-sm uppercase" />
        <input id="perfNome" type="text" placeholder="Nome (ex: Financeiro)" class="border rounded-lg p-2.5 text-sm" />
        <input id="perfOpcao" type="text" placeholder="Nº Menu (ex: 3)" class="border rounded-lg p-2.5 text-sm" />
        <button onclick="criarPerfil()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded-lg py-2.5 text-sm">
          + Salvar Perfil
        </button>
      </div>
    </div>

    <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
      <table class="w-full text-left text-sm">
        <thead class="bg-slate-50 border-b text-slate-600">
          <tr>
            <th class="p-3">Opção Menu</th>
            <th class="p-3">Código</th>
            <th class="p-3">Nome do Perfil</th>
            <th class="p-3 text-right">Ações</th>
          </tr>
        </thead>
        <tbody id="tabelaPerfis" class="divide-y"></tbody>
      </table>
    </div>
  </div>

  <!-- ABA 3: CADASTRO DE USUÁRIOS E VÍNCULO COM PERFIL -->
  <div id="abaUsuarios" class="hidden flex-1 overflow-y-auto p-8 max-w-5xl mx-auto w-full space-y-6">
    <div class="bg-white p-6 rounded-xl shadow-sm border border-slate-200 space-y-4">
      <h2 class="text-lg font-bold text-slate-800">Cadastrar Usuário e Vincular Perfil</h2>
      <div class="grid grid-cols-1 md:grid-cols-5 gap-3">
        <input id="userNome" type="text" placeholder="Nome do Atendente" class="border rounded-lg p-2.5 text-sm" />
        <input id="userEmail" type="email" placeholder="E-mail de acesso" class="border rounded-lg p-2.5 text-sm" />
        <input id="userSenha" type="password" placeholder="Senha" class="border rounded-lg p-2.5 text-sm" />
        <select id="userPerfilSelect" class="border rounded-lg p-2.5 text-sm bg-white"></select>
        <button onclick="criarUsuario()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded-lg py-2.5 text-sm">
          + Cadastrar Usuário
        </button>
      </div>
    </div>

    <div class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
      <table class="w-full text-left text-sm">
        <thead class="bg-slate-50 border-b text-slate-600">
          <tr>
            <th class="p-3">Nome</th>
            <th class="p-3">E-mail</th>
            <th class="p-3">Tipo</th>
            <th class="p-3">Perfil Vinculado</th>
            <th class="p-3 text-right">Ações</th>
          </tr>
        </thead>
        <tbody id="tabelaUsuarios" class="divide-y"></tbody>
      </table>
    </div>
  </div>

  <script>
    let TOKEN = localStorage.getItem("tottem_token") || "";
    let USUARIO = JSON.parse(localStorage.getItem("tottem_user") || "null");
    let PERFIS = [];
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
      document.getElementById("badgeUsuario").innerText = `${USUARIO.nome} • ${USUARIO.perfil_nome}`;

      if (USUARIO.role === "ADMIN") {
        document.getElementById("tabBtnPerfis").classList.remove("hidden");
        document.getElementById("tabBtnUsuarios").classList.remove("hidden");
        document.getElementById("filtroAdminSetor").classList.remove("hidden");
      }

      await carregarPerfis();
      atualizarFila();
      if (intervalo) clearInterval(intervalo);
      intervalo = setInterval(() => {
        atualizarFila();
        if (JID_ATUAL) carregarMensagens(JID_ATUAL, false);
      }, 3000);
    }

    function mudarAba(aba) {
      ["abaAtendimento", "abaPerfis", "abaUsuarios"].forEach(id => document.getElementById(id).classList.add("hidden"));
      ["tabBtnAtendimento", "tabBtnPerfis", "tabBtnUsuarios"].forEach(id => document.getElementById(id).classList.remove("bg-slate-700"));

      if (aba === "atendimento") {
        document.getElementById("abaAtendimento").classList.remove("hidden");
        document.getElementById("tabBtnAtendimento").classList.add("bg-slate-700");
      } else if (aba === "perfis") {
        document.getElementById("abaPerfis").classList.remove("hidden");
        document.getElementById("tabBtnPerfis").classList.add("bg-slate-700");
        carregarPerfis();
      } else if (aba === "usuarios") {
        document.getElementById("abaUsuarios").classList.remove("hidden");
        document.getElementById("tabBtnUsuarios").classList.add("bg-slate-700");
        carregarUsuarios();
      }
    }

    async function carregarPerfis() {
      const res = await fetch("/api/admin/perfis", { headers: authHeaders() });
      if (!res.ok) return;
      PERFIS = await res.json();

      const selTransf = document.getElementById("selectTransferir");
      selTransf.innerHTML = PERFIS.map(p => `<option value="${p.codigo}">${p.nome}</option>`).join("");

      const selFiltro = document.getElementById("filtroAdminSetor");
      selFiltro.innerHTML = `<option value="TODOS">Todos os Departamentos</option>` +
        PERFIS.map(p => `<option value="${p.codigo}">${p.nome}</option>`).join("");

      const selUserPerf = document.getElementById("userPerfilSelect");
      selUserPerf.innerHTML = `<option value="">-- Admin (Acesso Global) --</option>` +
        PERFIS.map(p => `<option value="${p.id}">${p.nome}</option>`).join("");

      const tbody = document.getElementById("tabelaPerfis");
      tbody.innerHTML = PERFIS.map(p => `
        <tr>
          <td class="p-3 font-bold">${p.opcao_menu}</td>
          <td class="p-3"><span class="bg-slate-200 px-2 py-0.5 rounded text-xs font-mono">${p.codigo}</span></td>
          <td class="p-3">${p.nome}</td>
          <td class="p-3 text-right">
            <button onclick="excluirPerfil(${p.id})" class="text-rose-600 hover:underline text-xs">Excluir</button>
          </td>
        </tr>
      `).join("");
    }

    async function criarPerfil() {
      const codigo = document.getElementById("perfCodigo").value;
      const nome = document.getElementById("perfNome").value;
      const opcao_menu = document.getElementById("perfOpcao").value;
      if (!codigo || !nome || !opcao_menu) return alert("Preencha todos os campos do perfil.");

      const res = await fetch("/api/admin/perfis", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ codigo, nome, opcao_menu })
      });
      if (res.ok) {
        document.getElementById("perfCodigo").value = "";
        document.getElementById("perfNome").value = "";
        document.getElementById("perfOpcao").value = "";
        carregarPerfis();
      } else {
        const err = await res.json();
        alert(err.detail || "Erro ao criar perfil");
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
      const tbody = document.getElementById("tabelaUsuarios");
      tbody.innerHTML = users.map(u => `
        <tr>
          <td class="p-3 font-medium">${u.nome}</td>
          <td class="p-3 text-slate-600">${u.email}</td>
          <td class="p-3"><span class="text-xs font-bold px-2 py-0.5 rounded bg-slate-200">${u.role}</span></td>
          <td class="p-3">
            <select onchange="alterarVinculoUsuario(${u.id}, '${u.nome}', '${u.email}', this.value)" class="border rounded p-1 text-xs bg-white">
              <option value="" ${!u.perfil_id ? 'selected' : ''}>Supervisão / Admin (Global)</option>
              ${PERFIS.map(p => `<option value="${p.id}" ${u.perfil_id === p.id ? 'selected' : ''}>${p.nome}</option>`).join("")}
            </select>
          </td>
          <td class="p-3 text-right">
            <button onclick="excluirUsuario(${u.id})" class="text-rose-600 hover:underline text-xs">Excluir</button>
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

      const payload = {
        nome,
        email,
        senha,
        role: perfilVal ? "ATENDENTE" : "ADMIN",
        perfil_id: perfilVal ? parseInt(perfilVal) : null
      };

      const res = await fetch("/api/admin/usuarios", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        document.getElementById("userNome").value = "";
        document.getElementById("userEmail").value = "";
        document.getElementById("userSenha").value = "";
        carregarUsuarios();
      } else {
        const err = await res.json();
        alert(err.detail || "Erro ao cadastrar usuário");
      }
    }

    async function alterarVinculoUsuario(id, nome, email, perfilVal) {
      const payload = {
        nome,
        email,
        role: perfilVal ? "ATENDENTE" : "ADMIN",
        perfil_id: perfilVal ? parseInt(perfilVal) : null
      };
      await fetch(`/api/admin/usuarios/${id}/vincular`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify(payload)
      });
      carregarUsuarios();
    }

    async function excluirUsuario(id) {
      if (!confirm("Excluir este usuário?")) return;
      await fetch(`/api/admin/usuarios/${id}`, { method: "DELETE", headers: authHeaders() });
      carregarUsuarios();
    }

    async function atualizarFila() {
      const filtro = document.getElementById("filtroAdminSetor").value || "TODOS";
      const res = await fetch(`/api/painel/chats?filtro_setor=${filtro}`, { headers: authHeaders() });
      if (!res.ok) return;
      const chats = await res.json();
      document.getElementById("contadorChats").innerText = chats.length;

      document.getElementById("listaChats").innerHTML = chats.map(c => `
        <div onclick="selecionarChat('${c.jid}', '${(c.nome || '').replace(/'/g, '')}')"
             class="p-3.5 cursor-pointer hover:bg-slate-50 ${JID_ATUAL === c.jid ? 'bg-emerald-50 border-l-4 border-emerald-600' : ''}">
          <div class="flex justify-between items-center">
            <span class="font-semibold text-slate-800 text-sm">${c.nome || c.jid.split('@')[0]}</span>
            ${c.nao_lidas > 0 ? `<span class="bg-emerald-600 text-white text-xs font-bold px-2 py-0.5 rounded-full">${c.nao_lidas}</span>` : ''}
          </div>
          <p class="text-xs text-slate-500 truncate mt-1">${c.ultima_mensagem || ''}</p>
          <span class="inline-block mt-1 text-[10px] font-bold px-2 py-0.5 rounded bg-slate-200 text-slate-700">${c.setor}</span>
        </div>
      `).join("");
    }

    function selecionarChat(jid, nome) {
      JID_ATUAL = jid;
      document.getElementById("chatVazio").classList.add("hidden");
      document.getElementById("chatAtivo").classList.remove("hidden");
      document.getElementById("contatoNome").innerText = nome || jid.split("@")[0];
      document.getElementById("contatoNumero").innerText = "+" + jid.split("@")[0];
      carregarMensagens(jid, true);
      atualizarFila();
    }

    function renderizarMidia(m) {
      if (!m.url_midia) return "";
      const tipo = (m.tipo_midia || "").toUpperCase();
      if (tipo === "IMAGE" || tipo === "STICKER") {
        return `<img src="${m.url_midia}" class="max-w-xs rounded-lg my-1 border" />`;
      }
      if (tipo === "AUDIO" || tipo === "VOICE") {
        return `<audio controls src="${m.url_midia}" class="my-1 max-w-xs"></audio>`;
      }
      if (tipo === "VIDEO") {
        return `<video controls src="${m.url_midia}" class="max-w-xs rounded-lg my-1"></video>`;
      }
      return `<a href="${m.url_midia}" target="_blank" class="inline-block my-1 text-xs bg-slate-200 hover:bg-slate-300 px-3 py-1.5 rounded font-semibold text-slate-800">📄 Baixar Anexo</a>`;
    }

    async function carregarMensagens(jid, rolarFim) {
      const res = await fetch(`/api/painel/mensagens/${encodeURIComponent(jid)}`, { headers: authHeaders() });
      if (!res.ok) return;
      const msgs = await res.json();
      const area = document.getElementById("areaMensagens");
      area.innerHTML = msgs.map(m => {
        const isCliente = m.remetente === "CLIENTE";
        return `
          <div class="flex ${isCliente ? 'justify-start' : 'justify-end'}">
            <div class="max-w-md rounded-xl px-4 py-2 shadow-sm text-sm ${isCliente ? 'bg-white text-slate-800' : 'bg-emerald-100 text-slate-900'}">
              <div class="text-[10px] font-bold text-slate-500 mb-0.5">${m.remetente}</div>
              ${renderizarMidia(m)}
              <div class="whitespace-pre-line">${m.conteudo}</div>
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
        method: "POST",
        headers: authHeaders(),
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
        method: "POST",
        headers: authHeaders(false),
        body: formData
      });
      carregarMensagens(JID_ATUAL, true);
      atualizarFila();
    }

    async function alternarGravacaoAudio() {
      const btn = document.getElementById("btnGravarAudio");
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
            formData.append("arquivo", blob, "audio_gravado.webm");

            await fetch("/api/painel/enviar-midia", {
              method: "POST",
              headers: authHeaders(false),
              body: formData
            });
            carregarMensagens(JID_ATUAL, true);
            atualizarFila();
          };
          mediaRecorder.start();
          btn.classList.replace("bg-slate-100", "bg-rose-600");
          btn.classList.add("text-white");
          btn.innerText = "⏹️";
        } catch (err) {
          alert("Permita o acesso ao microfone no navegador para gravar áudios.");
        }
      } else {
        mediaRecorder.stop();
        btn.classList.replace("bg-rose-600", "bg-slate-100");
        btn.classList.remove("text-white");
        btn.innerText = "🎤";
      }
    }

    async function transferir() {
      const destino = document.getElementById("selectTransferir").value;
      if (!JID_ATUAL || !destino) return;
      await fetch("/api/painel/transferir", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL, novo_setor: destino })
      });
      JID_ATUAL = null;
      document.getElementById("chatAtivo").classList.add("hidden");
      document.getElementById("chatVazio").classList.remove("hidden");
      atualizarFila();
    }

    async function encerrar() {
      if (!JID_ATUAL || !confirm("Encerrar este atendimento?")) return;
      await fetch("/api/painel/encerrar", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ jid: JID_ATUAL })
      });
      JID_ATUAL = null;
      document.getElementById("chatAtivo").classList.add("hidden");
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
