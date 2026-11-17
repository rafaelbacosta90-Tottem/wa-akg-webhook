import os
import re
import io
import csv
import uuid
import hashlib
import logging
import asyncio
import random
import subprocess
from collections import deque
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx
import psycopg2
import psycopg2.extras
import openpyxl
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tottem-omnidesk")

app = FastAPI(title="Tottem OmniDesk — Atendimento & Prospecção (Supabase)")

# ==============================================================================
# DIRETÓRIO DE MÍDIAS LOCAIS
# ==============================================================================
MEDIA_DIR = Path("media_files")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

# ==============================================================================
# CONFIGURAÇÕES (VARIÁVEIS DE AMBIENTE)
# ==============================================================================
WA_BASE_URL = os.getenv("WA_BASE_URL", "https://wa-akg-v13p.onrender.com").rstrip("/")
SESSION_ID = os.getenv("SESSION_ID", "sessao_principal")
API_KEY = os.getenv("API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_EMAIL_DEFAULT = os.getenv("ADMIN_EMAIL", "admin@tottemtecnologia.com.br")
ADMIN_SENHA_DEFAULT = os.getenv("ADMIN_SENHA", "tottem2026")

# Controle em memória do Anti-Banimento e Campanha de Disparo em Massa
HISTORICO_ENVIOS_TS = deque()
CAMPANHA_STATUS = {
    "em_execucao": False,
    "cancelar": False,
    "total": 0,
    "enviados": 0,
    "falhas": 0,
    "atual": "",
    "mensagem_status": "Pronto para iniciar disparos.",
}

# ==============================================================================
# MODELOS PYDANTIC
# ==============================================================================
class LoginReq(BaseModel):
    email: str
    senha: str


class PerfilReq(BaseModel):
    codigo: str
    nome: str
    opcao_menu: str


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


class ConfigAntiBanReq(BaseModel):
    habilitado: bool
    limite_mensagens: int
    janela_segundos: int
    atraso_min_ms: int
    atraso_max_ms: int


class TemplateReq(BaseModel):
    titulo: str
    conteudo: str


class DisparoMassaReq(BaseModel):
    template_id: int
    setor_destino: str
    status_filtro: str = "nao_enviado"
    enviar_ambos_numeros: bool = False
    contato_ids: Optional[List[int]] = None


class AbrirConversaContatoReq(BaseModel):
    contato_id: int
    setor_destino: str = "COMERCIAL"


class ExcluirMassaContatosReq(BaseModel):
    ids: List[int]


class AtualizarStatusContatoReq(BaseModel):
    status_contato: str


# ==============================================================================
# UTILITÁRIOS DE TELEFONE, DOCUMENTO E HASH
# ==============================================================================
def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def limpar_digitos(valor: Any) -> str:
    if valor is None:
        return ""
    return re.sub(r"\D", "", str(valor))


def normalizar_numero_wa(celular: str, telefone: str = "") -> str:
    """Prioriza o celular e fallback para telefone, formatando para JID do WhatsApp."""
    for bruto in (celular, telefone):
        dig = limpar_digitos(bruto)
        if not dig:
            continue
        if len(dig) in (10, 11):
            dig = "55" + dig
        if len(dig) >= 12:
            return f"{dig}@s.whatsapp.net"
    return ""


def extrair_jids_contato(contato: Dict[str, Any], enviar_ambos: bool = False) -> List[str]:
    """Retorna o(s) JID(s) do contato sem duplicar quando Celular e Telefone são iguais."""
    jids: List[str] = []
    jid_cel = normalizar_numero_wa(contato.get("celular") or "", "")
    jid_tel = normalizar_numero_wa(contato.get("telefone") or "", "")

    if jid_cel:
        jids.append(jid_cel)
        if enviar_ambos and jid_tel and jid_tel != jid_cel:
            jids.append(jid_tel)
    elif jid_tel:
        jids.append(jid_tel)
    elif contato.get("jid"):
        jids.append(contato["jid"])

    return jids


def aplicar_variaveis_template(texto: str, contato: Dict[str, Any]) -> str:
    nome = contato.get("nome") or contato.get("razao_social") or "Cliente"
    razao = contato.get("razao_social") or nome
    cnpj = contato.get("cnpj") or ""
    celular = contato.get("celular") or contato.get("telefone") or ""

    resultado = texto
    resultado = resultado.replace("{nome}", nome)
    resultado = resultado.replace("{razao_social}", razao)
    resultado = resultado.replace("{cnpj}", cnpj)
    resultado = resultado.replace("{celular}", celular)
    return resultado


# ==============================================================================
# CONEXÃO SUPABASE (POSTGRESQL) E TABELAS EXCLUSIVAS DO OMNIDESK
# ==============================================================================
def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("A variável de ambiente DATABASE_URL (Supabase PostgreSQL) não está configurada.")
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    conn.autocommit = True
    return conn


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Perfis / Departamentos
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perfis_atendimento (
                    id SERIAL PRIMARY KEY,
                    codigo VARCHAR(50) UNIQUE NOT NULL,
                    nome VARCHAR(100) NOT NULL,
                    opcao_menu VARCHAR(10) NOT NULL,
                    ativo BOOLEAN DEFAULT TRUE
                );
            """)

            # 2. Usuários do Painel OmniDesk
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios_painel (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(120) NOT NULL,
                    email VARCHAR(150) UNIQUE NOT NULL,
                    senha_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) NOT NULL DEFAULT 'ATENDENTE',
                    perfil_id INTEGER NULL REFERENCES perfis_atendimento(id) ON DELETE SET NULL,
                    token_sessao VARCHAR(120) NULL,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 3. Contatos Importados (Isolada das tabelas 'clientes' e 'pessoas')
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contatos_importados (
                    id SERIAL PRIMARY KEY,
                    cnpj VARCHAR(30) NULL,
                    nome VARCHAR(160) NULL,
                    razao_social VARCHAR(200) NULL,
                    telefone VARCHAR(40) NULL,
                    celular VARCHAR(40) NULL,
                    jid VARCHAR(100) UNIQUE NOT NULL,
                    status_contato VARCHAR(30) NOT NULL DEFAULT 'nao_enviado',
                    origem VARCHAR(50) DEFAULT 'PLANILHA',
                    ultimo_disparo TIMESTAMP NULL,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 4. Atendimentos / Tickets WhatsApp
            cur.execute("""
                CREATE TABLE IF NOT EXISTS atendimentos (
                    jid VARCHAR(100) PRIMARY KEY,
                    nome VARCHAR(150),
                    setor VARCHAR(50) DEFAULT 'TRIAGEM',
                    status VARCHAR(30) DEFAULT 'TRIAGEM',
                    ultima_mensagem TEXT,
                    nao_lidas INTEGER DEFAULT 0,
                    notas_crm TEXT NULL,
                    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 5. Histórico de Mensagens
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mensagens_atendimento (
                    id SERIAL PRIMARY KEY,
                    jid VARCHAR(100) NOT NULL,
                    remetente VARCHAR(80) NOT NULL,
                    conteudo TEXT NOT NULL,
                    tipo_midia VARCHAR(20) DEFAULT 'TEXT',
                    url_midia VARCHAR(255) NULL,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 6. Configuração Anti-Banimento
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config_antiban (
                    id INTEGER PRIMARY KEY,
                    habilitado BOOLEAN DEFAULT TRUE,
                    limite_mensagens INTEGER DEFAULT 5,
                    janela_segundos INTEGER DEFAULT 10,
                    atraso_min_ms INTEGER DEFAULT 1000,
                    atraso_max_ms INTEGER DEFAULT 3000
                );
            """)

            # 7. Templates de Campanha
            cur.execute("""
                CREATE TABLE IF NOT EXISTS templates_disparo (
                    id SERIAL PRIMARY KEY,
                    titulo VARCHAR(150) NOT NULL,
                    conteudo TEXT NOT NULL,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Seeds iniciais idempotentes
            cur.execute("SELECT COUNT(*) AS total FROM perfis_atendimento")
            if cur.fetchone()["total"] == 0:
                cur.execute(
                    "INSERT INTO perfis_atendimento (codigo, nome, opcao_menu) VALUES (%s, %s, %s)",
                    ("COMERCIAL", "Comercial & Vendas", "1"),
                )
                cur.execute(
                    "INSERT INTO perfis_atendimento (codigo, nome, opcao_menu) VALUES (%s, %s, %s)",
                    ("SUPORTE", "Suporte Técnico", "2"),
                )

            cur.execute("SELECT COUNT(*) AS total FROM usuarios_painel WHERE role = 'ADMIN'")
            if cur.fetchone()["total"] == 0:
                cur.execute(
                    """
                    INSERT INTO usuarios_painel (nome, email, senha_hash, role, perfil_id)
                    VALUES (%s, %s, %s, 'ADMIN', NULL)
                    """,
                    ("Administrador Geral", ADMIN_EMAIL_DEFAULT, hash_senha(ADMIN_SENHA_DEFAULT)),
                )

            cur.execute("SELECT COUNT(*) AS total FROM config_antiban WHERE id = 1")
            if cur.fetchone()["total"] == 0:
                cur.execute("""
                    INSERT INTO config_antiban (id, habilitado, limite_mensagens, janela_segundos, atraso_min_ms, atraso_max_ms)
                    VALUES (1, TRUE, 5, 10, 1000, 3000)
                """)

            cur.execute("SELECT COUNT(*) AS total FROM templates_disparo")
            if cur.fetchone()["total"] == 0:
                cur.execute(
                    "INSERT INTO templates_disparo (titulo, conteudo) VALUES (%s, %s)",
                    (
                        "Apresentação Comercial — Automação de Licitações",
                        "Olá *{nome}* da *{razao_social}*, tudo bem? 👋\n\nAqui é da *Tottem Tecnologia*. Vimos que vocês atuam com vendas para o governo e gostaríamos de mostrar como automatizar lances e propostas no Compras.gov e PNCP.\n\nPosso te enviar uma demonstração rápida?",
                    ),
                )

        logger.info("Banco de dados Supabase (OmniDesk) sincronizado sem alterar tabelas existentes.")
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


# ==============================================================================
# AUTENTICAÇÃO E PERMISSÕES
# ==============================================================================
def obter_usuario_atual(request: Request) -> Dict[str, Any]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autenticado")
    token = auth.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token inválido")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.nome, u.email, u.role, u.perfil_id,
                       p.codigo AS perfil_codigo, p.nome AS perfil_nome
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                WHERE u.token_sessao = %s
                """,
                (token,),
            )
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="Sessão expirada")
            return dict(user)
    finally:
        conn.close()


def exigir_admin(user: Dict[str, Any]):
    if user["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")


# ==============================================================================
# MOTOR ANTI-BANIMENTO + INTEGRAÇÃO COM WA-AKG
# ==============================================================================
def headers_wa():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["x-api-key"] = API_KEY
    return h


def obter_config_antiban_db() -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM config_antiban WHERE id = 1")
            cfg = cur.fetchone()
            return dict(cfg) if cfg else {
                "habilitado": True,
                "limite_mensagens": 5,
                "janela_segundos": 10,
                "atraso_min_ms": 1000,
                "atraso_max_ms": 3000,
            }
    finally:
        conn.close()


async def aplicar_protecao_antiban():
    """Enfileira e aplica atraso aleatório inteligente quando o limite de taxa é atingido."""
    cfg = obter_config_antiban_db()
    if not cfg.get("habilitado"):
        return

    limite = max(1, int(cfg.get("limite_mensagens", 5)))
    janela = max(1, int(cfg.get("janela_segundos", 10)))
    min_ms = max(0, int(cfg.get("atraso_min_ms", 1000)))
    max_ms = max(min_ms, int(cfg.get("atraso_max_ms", 3000)))

    loop = asyncio.get_running_loop()
    agora = loop.time()

    while HISTORICO_ENVIOS_TS and (agora - HISTORICO_ENVIOS_TS[0]) > janela:
        HISTORICO_ENVIOS_TS.popleft()

    if len(HISTORICO_ENVIOS_TS) >= limite:
        atraso_seg = random.uniform(min_ms / 1000.0, max_ms / 1000.0)
        tempo_restante_janela = janela - (agora - HISTORICO_ENVIOS_TS[0])
        espera_final = max(atraso_seg, min(tempo_restante_janela, max_ms / 1000.0))
        logger.info(f"[Anti-Banimento] Limite de {limite} msgs/{janela}s atingido. Aguardando {espera_final:.2f}s...")
        await asyncio.sleep(espera_final)

    HISTORICO_ENVIOS_TS.append(loop.time())


async def enviar_texto_wa(jid: str, texto: str) -> bool:
    await aplicar_protecao_antiban()
    url = f"{WA_BASE_URL}/api/sessions/{SESSION_ID}/messages/send"
    payload = {"jid": jid, "type": "text", "message": {"text": texto}}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=headers_wa())
            return resp.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Erro ao enviar texto WA: {e}")
        return False


def converter_para_ogg(caminho_entrada: Path) -> Path:
    caminho_saida = caminho_entrada.with_suffix(".ogg")
    cmd = [
        "ffmpeg", "-y", "-i", str(caminho_entrada),
        "-c:a", "libopus", "-b:a", "32k", "-vbr", "on", "-ar", "16000", "-ac", "1",
        str(caminho_saida),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return caminho_saida


async def enviar_midia_wa(jid: str, caminho_arquivo: Path, nome_arquivo: str, tipo: str, legenda: str = "") -> bool:
    await aplicar_protecao_antiban()
    url = f"{WA_BASE_URL}/api/sessions/{SESSION_ID}/messages/send-media"
    headers = {}
    if API_KEY:
        headers["x-api-key"] = API_KEY

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            with open(caminho_arquivo, "rb") as f:
                files = {"file": (nome_arquivo, f)}
                data = {"jid": jid, "type": tipo, "caption": legenda}
                resp = await client.post(url, data=data, files=files, headers=headers)
                return resp.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Erro ao enviar mídia WA: {e}")
        return False


async def baixar_midia_recebida(message_id: str, extensao: str = ".bin") -> Optional[str]:
    url = f"{WA_BASE_URL}/api/sessions/{SESSION_ID}/messages/{message_id}/media"
    headers = {}
    if API_KEY:
        headers["x-api-key"] = API_KEY
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and resp.content:
                nome_local = f"{uuid.uuid4().hex}{extensao}"
                destino = MEDIA_DIR / nome_local
                destino.write_bytes(resp.content)
                return f"/media/{nome_local}"
    except Exception as e:
        logger.error(f"Erro ao baixar mídia recebida: {e}")
    return None


def registrar_mensagem(
    jid: str,
    remetente: str,
    conteudo: str,
    nome: Optional[str] = None,
    incrementar_nao_lidas: bool = False,
    tipo_midia: str = "TEXT",
    url_midia: Optional[str] = None,
    setor_padrao: str = "TRIAGEM",
    status_padrao: str = "TRIAGEM",
):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT jid, nome FROM atendimentos WHERE jid = %s", (jid,))
            existe = cur.fetchone()

            if not existe:
                nome_final = nome or jid.split("@")[0]
                cur.execute(
                    """
                    INSERT INTO atendimentos (jid, nome, setor, status, ultima_mensagem, nao_lidas, atualizado_em)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (jid, nome_final, setor_padrao, status_padrao, conteudo[:250], 1 if incrementar_nao_lidas else 0),
                )
            else:
                inc = 1 if incrementar_nao_lidas else 0
                if nome and (not existe["nome"] or existe["nome"] == jid.split("@")[0]):
                    cur.execute(
                        """
                        UPDATE atendimentos
                        SET nome = %s, ultima_mensagem = %s, nao_lidas = nao_lidas + %s, atualizado_em = NOW()
                        WHERE jid = %s
                        """,
                        (nome, conteudo[:250], inc, jid),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE atendimentos
                        SET ultima_mensagem = %s, nao_lidas = nao_lidas + %s, atualizado_em = NOW()
                        WHERE jid = %s
                        """,
                        (conteudo[:250], inc, jid),
                    )

            cur.execute(
                """
                INSERT INTO mensagens_atendimento (jid, remetente, conteudo, tipo_midia, url_midia)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (jid, remetente, conteudo, tipo_midia, url_midia),
            )
    finally:
        conn.close()


# ==============================================================================
# LÓGICA DO WEBHOOK E ROTEAMENTO DINÂMICO POR PERFIL
# ==============================================================================
def gerar_menu_dinamico() -> str:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT opcao_menu, nome FROM perfis_atendimento WHERE ativo = TRUE ORDER BY opcao_menu ASC")
            perfis = cur.fetchall()
            linhas = "\n".join([f"*{p['opcao_menu']}* - {p['nome']}" for p in perfis])
            return (
                "Olá! Bem-vindo ao atendimento da *Tottem Tecnologia* 👋\n\n"
                "Para agilizar seu atendimento, responda com o número do departamento desejado:\n\n"
                f"{linhas}"
            )
    finally:
        conn.close()


async def processar_webhook(payload: dict):
    event = payload.get("event")
    if event != "messages.upsert":
        return

    data = payload.get("data", {})
    key = data.get("key", {})
    if key.get("fromMe") is True:
        return

    jid = key.get("remoteJid", "")
    if not jid or "@g.us" in jid or "status@broadcast" in jid:
        return

    msg_id = key.get("id", "")
    nome_contato = data.get("pushName") or jid.split("@")[0]
    msg = data.get("message", {})

    tipo_midia = "TEXT"
    url_midia = None
    texto = ""

    if "conversation" in msg:
        texto = msg["conversation"]
    elif "extendedTextMessage" in msg:
        texto = msg["extendedTextMessage"].get("text", "")
    elif "imageMessage" in msg:
        tipo_midia = "IMAGE"
        texto = msg["imageMessage"].get("caption") or "📷 Imagem recebida"
        if msg_id:
            url_midia = await baixar_midia_recebida(msg_id, ".jpg")
    elif "audioMessage" in msg:
        tipo_midia = "AUDIO"
        texto = "🎵 Áudio recebido"
        if msg_id:
            url_midia = await baixar_midia_recebida(msg_id, ".ogg")
    elif "videoMessage" in msg:
        tipo_midia = "VIDEO"
        texto = msg["videoMessage"].get("caption") or "🎥 Vídeo recebido"
        if msg_id:
            url_midia = await baixar_midia_recebida(msg_id, ".mp4")
    elif "documentMessage" in msg:
        tipo_midia = "DOCUMENT"
        nome_doc = msg["documentMessage"].get("fileName", "documento.pdf")
        ext = Path(nome_doc).suffix or ".pdf"
        texto = f"📄 Documento: {nome_doc}"
        if msg_id:
            url_midia = await baixar_midia_recebida(msg_id, ext)
    else:
        texto = "[Mensagem recebida]"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM atendimentos WHERE jid = %s", (jid,))
            atendimento = cur.fetchone()

            if not atendimento:
                cur.execute("SELECT nome, razao_social FROM contatos_importados WHERE jid = %s LIMIT 1", (jid,))
                contato_imp = cur.fetchone()
                if contato_imp:
                    nome_contato = contato_imp.get("nome") or contato_imp.get("razao_social") or nome_contato

            registrar_mensagem(
                jid, "CLIENTE", texto, nome=nome_contato,
                incrementar_nao_lidas=True, tipo_midia=tipo_midia, url_midia=url_midia
            )

            if atendimento and atendimento["status"] == "EM_ATENDIMENTO":
                return

            opcao = texto.strip()
            cur.execute(
                "SELECT codigo, nome FROM perfis_atendimento WHERE ativo = TRUE AND opcao_menu = %s",
                (opcao,),
            )
            perfil_escolhido = cur.fetchone()

            if perfil_escolhido:
                cur.execute(
                    "UPDATE atendimentos SET setor = %s, status = 'EM_ATENDIMENTO', nao_lidas = 1 WHERE jid = %s",
                    (perfil_escolhido["codigo"], jid),
                )
                resposta = f"✅ Perfeito! Encaminhei sua solicitação para *{perfil_escolhido['nome']}*. Um especialista já vai te atender."
                await enviar_texto_wa(jid, resposta)
                registrar_mensagem(jid, "BOT", resposta)
            else:
                menu = gerar_menu_dinamico()
                await enviar_texto_wa(jid, menu)
                registrar_mensagem(jid, "BOT", menu)
    finally:
        conn.close()


# ==============================================================================
# ROTAS DE AUTENTICAÇÃO E ADMINISTRAÇÃO
# ==============================================================================
@app.post("/api/auth/login")
def login(dados: LoginReq):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.nome, u.email, u.role, u.perfil_id,
                       p.codigo AS perfil_codigo, p.nome AS perfil_nome
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                WHERE u.email = %s AND u.senha_hash = %s
                """,
                (dados.email.strip().lower(), hash_senha(dados.senha)),
            )
            user = cur.fetchone()
            if not user:
                raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")

            token = uuid.uuid4().hex + uuid.uuid4().hex
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


@app.get("/api/admin/perfis")
def listar_perfis(request: Request):
    obter_usuario_atual(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM perfis_atendimento WHERE ativo = TRUE ORDER BY opcao_menu ASC")
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
                INSERT INTO perfis_atendimento (codigo, nome, opcao_menu, ativo)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (codigo) DO UPDATE SET nome = EXCLUDED.nome, opcao_menu = EXCLUDED.opcao_menu, ativo = TRUE
                """,
                (dados.codigo.strip().upper(), dados.nome.strip(), dados.opcao_menu.strip()),
            )
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/admin/perfis/{perfil_id}")
def excluir_perfil(perfil_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE perfis_atendimento SET ativo = FALSE WHERE id = %s", (perfil_id,))
        return {"status": "ok"}
    finally:
        conn.close()


@app.get("/api/admin/usuarios")
def listar_usuarios(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.nome, u.email, u.role, u.perfil_id,
                       p.nome AS perfil_nome, p.codigo AS perfil_codigo
                FROM usuarios_painel u
                LEFT JOIN perfis_atendimento p ON u.perfil_id = p.id
                ORDER BY u.id ASC
                """
            )
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
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=400, detail="E-mail já cadastrado")
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
                "UPDATE usuarios_painel SET perfil_id = %s, role = %s WHERE id = %s",
                (dados.perfil_id, dados.role, usuario_id),
            )
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/admin/usuarios/{usuario_id}")
def excluir_usuario(usuario_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    if user["id"] == usuario_id:
        raise HTTPException(status_code=400, detail="Você não pode excluir seu próprio usuário")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM usuarios_painel WHERE id = %s", (usuario_id,))
        return {"status": "ok"}
    finally:
        conn.close()


# --- ROTAS DE CONFIGURAÇÃO ANTI-BANIMENTO ---
@app.get("/api/admin/antiban")
def get_antiban(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    return obter_config_antiban_db()


@app.post("/api/admin/antiban")
def salvar_antiban(dados: ConfigAntiBanReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE config_antiban
                SET habilitado = %s, limite_mensagens = %s, janela_segundos = %s,
                    atraso_min_ms = %s, atraso_max_ms = %s
                WHERE id = 1
                """,
                (
                    dados.habilitado,
                    max(1, dados.limite_mensagens),
                    max(1, dados.janela_segundos),
                    max(0, dados.atraso_min_ms),
                    max(dados.atraso_min_ms, dados.atraso_max_ms),
                ),
            )
        return {"status": "ok"}
    finally:
        conn.close()


# --- FUNÇÃO AUXILIAR DE LEITURA DE PLANILHA (.XLSX / .CSV) ---
def ler_linhas_planilha(conteudo_bytes: bytes, filename: str) -> List[List[str]]:
    nome_arq = (filename or "").lower()
    linhas_raw: List[List[str]] = []

    if nome_arq.endswith((".xlsx", ".xlsm")):
        wb = openpyxl.load_workbook(io.BytesIO(conteudo_bytes), read_only=True, data_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            linhas_raw.append([str(c).strip() if c is not None else "" for c in row])
    else:
        texto_csv = conteudo_bytes.decode("utf-8-sig", errors="replace")
        delimitador = ";" if texto_csv.count(";") > texto_csv.count(",") else ","
        reader = csv.reader(io.StringIO(texto_csv), delimiter=delimitador)
        for row in reader:
            linhas_raw.append([c.strip() for c in row])

    return [r for r in linhas_raw if any(cell != "" for cell in r)]


# --- PREVIEW DA PLANILHA ANTES DA IMPORTAÇÃO ---
@app.post("/api/admin/contatos/preview")
async def preview_planilha_contatos(request: Request, arquivo: UploadFile = File(...)):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    conteudo_bytes = await arquivo.read()
    linhas = ler_linhas_planilha(conteudo_bytes, arquivo.filename or "")

    if not linhas:
        raise HTTPException(status_code=400, detail="O arquivo está vazio ou não pôde ser lido.")

    cabecalho_raw = linhas[0]
    num_cols = max(len(r) for r in linhas[:20])
    colunas = [
        cabecalho_raw[i] if i < len(cabecalho_raw) and cabecalho_raw[i] else f"Coluna {i + 1}"
        for i in range(num_cols)
    ]

    preview_linhas = []
    for row in linhas[1:11]:
        linha_pad = [row[i] if i < len(row) else "" for i in range(num_cols)]
        preview_linhas.append(linha_pad)

    return {
        "filename": arquivo.filename,
        "colunas": colunas,
        "preview": preview_linhas,
        "total_linhas": max(0, len(linhas) - 1),
    }


# --- IMPORTAÇÃO COM MAPEAMENTO CONFIRMADO ---
@app.post("/api/admin/contatos/importar-mapeado")
async def importar_planilha_mapeada(
    request: Request,
    arquivo: UploadFile = File(...),
    col_cnpj: int = Form(-1),
    col_razao: int = Form(-1),
    col_nome: int = Form(-1),
    col_celular: int = Form(-1),
    col_telefone: int = Form(-1),
):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    if col_celular < 0 and col_telefone < 0:
        raise HTTPException(status_code=400, detail="Selecione pelo menos a coluna de Celular ou Telefone.")

    conteudo_bytes = await arquivo.read()
    linhas = ler_linhas_planilha(conteudo_bytes, arquivo.filename or "")
    if len(linhas) < 2:
        raise HTTPException(status_code=400, detail="A planilha não possui linhas de dados.")

    def pegar_valor(row: List[str], idx: int) -> str:
        if idx < 0 or idx >= len(row):
            return ""
        return row[idx].strip()

    conn = get_db_connection()
    importados = 0
    ignorados_sem_tel = 0
    ignorados_ja_comprou = 0

    try:
        with conn.cursor() as cur:
            # Verifica CNPJs que já compraram na tabela 'pessoas' do Supabase
            cnpjs_comprados = set()
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'pessoas'
                ) AS existe;
            """)
            if cur.fetchone()["existe"]:
                cur.execute("SELECT cnpj, status_venda FROM pessoas")
                for p in cur.fetchall():
                    st = (p.get("status_venda") or "").strip().upper()
                    if ("COMPROU" in st or "CONVERTIDO" in st) and "NAO" not in st and "NÃO" not in st:
                        c_limpo = limpar_digitos(p.get("cnpj"))
                        if c_limpo:
                            cnpjs_comprados.add(c_limpo)

            for row in linhas[1:]:
                cnpj = pegar_valor(row, col_cnpj)
                razao = pegar_valor(row, col_razao)
                nome = pegar_valor(row, col_nome)
                celular = pegar_valor(row, col_celular)
                telefone = pegar_valor(row, col_telefone)

                if cnpj and limpar_digitos(cnpj) in cnpjs_comprados:
                    ignorados_ja_comprou += 1
                    continue

                jid = normalizar_numero_wa(celular, telefone)
                if not jid:
                    ignorados_sem_tel += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO contatos_importados
                        (cnpj, nome, razao_social, telefone, celular, jid, status_contato, origem)
                    VALUES (%s, %s, %s, %s, %s, %s, 'nao_enviado', 'PLANILHA')
                    ON CONFLICT (jid) DO UPDATE SET
                        cnpj = CASE WHEN EXCLUDED.cnpj != '' THEN EXCLUDED.cnpj ELSE contatos_importados.cnpj END,
                        nome = CASE WHEN EXCLUDED.nome != '' THEN EXCLUDED.nome ELSE contatos_importados.nome END,
                        razao_social = CASE WHEN EXCLUDED.razao_social != '' THEN EXCLUDED.razao_social ELSE contatos_importados.razao_social END,
                        telefone = CASE WHEN EXCLUDED.telefone != '' THEN EXCLUDED.telefone ELSE contatos_importados.telefone END,
                        celular = CASE WHEN EXCLUDED.celular != '' THEN EXCLUDED.celular ELSE contatos_importados.celular END
                    """,
                    (cnpj, nome, razao, telefone, celular, jid),
                )
                importados += 1

        return {
            "status": "ok",
            "importados": importados,
            "ignorados": ignorados_sem_tel,
            "ignorados_ja_comprou": ignorados_ja_comprou,
        }
    finally:
        conn.close()


# --- SINCRONIZAÇÃO COM A TABELA 'pessoas' DO SUPABASE ---
@app.post("/api/admin/contatos/sincronizar-pessoas")
def sincronizar_com_tabela_pessoas(request: Request):
    """
    Sincroniza a tabela 'contatos_importados' com a tabela 'pessoas' do Supabase:
    1. Se o registro em 'pessoas' estiver como COMPROU -> remove de 'contatos_importados'.
    2. Se o registro em 'pessoas' estiver como NAO_COMPROU (ou NÃO COMPROU) ->
       marca como 'nao_comprou' em 'contatos_importados' (ou importa se ainda não existir).
    """
    user = obter_usuario_atual(request)
    exigir_admin(user)

    conn = get_db_connection()
    removidos_comprou = 0
    marcados_nao_comprou = 0
    novos_nao_comprou = 0
    total_lido_pessoas = 0

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'pessoas'
                ) AS existe;
            """)
            if not cur.fetchone()["existe"]:
                raise HTTPException(status_code=404, detail="Tabela 'pessoas' não encontrada neste banco Supabase.")

            cur.execute("SELECT cnpj, razao_social, nome_fantasia, telefone, status_venda FROM pessoas")
            pessoas_rows = cur.fetchall()
            total_lido_pessoas = len(pessoas_rows)

            cur.execute("SELECT id, cnpj, jid, status_contato FROM contatos_importados")
            contatos_rows = cur.fetchall()

            mapa_contatos_por_cnpj: Dict[str, List[Dict[str, Any]]] = {}
            for c in contatos_rows:
                cnpj_limpo = limpar_digitos(c.get("cnpj"))
                if cnpj_limpo:
                    mapa_contatos_por_cnpj.setdefault(cnpj_limpo, []).append(c)

            for p in pessoas_rows:
                cnpj_limpo = limpar_digitos(p.get("cnpj"))
                if not cnpj_limpo:
                    continue

                status_raw = (p.get("status_venda") or "").strip().upper()
                status_norm = (
                    status_raw.replace("Ã", "A")
                    .replace("Á", "A")
                    .replace("É", "E")
                    .replace("Í", "I")
                    .replace("Ó", "O")
                    .replace("Ú", "U")
                )

                existentes = mapa_contatos_por_cnpj.get(cnpj_limpo, [])

                eh_nao_comprou = (
                    ("NAO" in status_norm and "COMPROU" in status_norm)
                    or "PERDIDO" in status_norm
                    or "CANCELADO" in status_norm
                )
                eh_comprou = (
                    not eh_nao_comprou
                    and (
                        "COMPROU" in status_norm
                        or "CONVERTIDO" in status_norm
                        or "PAGO" in status_norm
                        or "ATIVO" in status_norm
                    )
                )

                if eh_comprou:
                    if existentes:
                        ids_remover = [item["id"] for item in existentes]
                        cur.execute("DELETE FROM contatos_importados WHERE id = ANY(%s)", (ids_remover,))
                        removidos_comprou += cur.rowcount

                elif eh_nao_comprou:
                    if existentes:
                        for item in existentes:
                            if item.get("status_contato") != "nao_comprou":
                                cur.execute(
                                    "UPDATE contatos_importados SET status_contato = 'nao_comprou' WHERE id = %s",
                                    (item["id"],),
                                )
                                marcados_nao_comprou += 1
                    else:
                        tel_p = p.get("telefone") or ""
                        jid_p = normalizar_numero_wa(tel_p, tel_p)
                        if jid_p:
                            dig = limpar_digitos(tel_p)
                            is_cel = len(dig) in (11, 13)
                            cur.execute("""
                                INSERT INTO contatos_importados
                                    (cnpj, nome, razao_social, telefone, celular, jid, status_contato, origem)
                                VALUES (%s, %s, %s, %s, %s, %s, 'nao_comprou', 'TABELA_PESSOAS')
                                ON CONFLICT (jid) DO UPDATE SET
                                    cnpj = CASE WHEN EXCLUDED.cnpj != '' THEN EXCLUDED.cnpj ELSE contatos_importados.cnpj END,
                                    status_contato = 'nao_comprou'
                            """, (
                                p.get("cnpj") or "",
                                p.get("nome_fantasia") or p.get("razao_social") or "",
                                p.get("razao_social") or "",
                                "" if is_cel else tel_p,
                                tel_p if is_cel else "",
                                jid_p,
                            ))
                            novos_nao_comprou += 1

        return {
            "status": "ok",
            "total_lido_pessoas": total_lido_pessoas,
            "removidos_comprou": removidos_comprou,
            "marcados_nao_comprou": marcados_nao_comprou,
            "novos_nao_comprou": novos_nao_comprou,
        }
    finally:
        conn.close()


@app.get("/api/admin/contatos")
def listar_contatos_importados(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contatos_importados ORDER BY id DESC LIMIT 1000")
            return cur.fetchall()
    finally:
        conn.close()


@app.put("/api/admin/contatos/{contato_id}/status")
def atualizar_status_contato(contato_id: int, dados: AtualizarStatusContatoReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    status_val = (dados.status_contato or "").strip().lower()
    if status_val not in {"nao_enviado", "enviou", "falha", "nao_comprou"}:
        raise HTTPException(status_code=400, detail="Status inválido.")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contatos_importados SET status_contato = %s WHERE id = %s",
                (status_val, contato_id),
            )
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/admin/contatos/{contato_id}")
def excluir_contato(contato_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contatos_importados WHERE id = %s", (contato_id,))
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/api/admin/contatos/excluir-massa")
def excluir_contatos_em_massa(dados: ExcluirMassaContatosReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    if not dados.ids:
        return {"status": "ok", "excluidos": 0}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contatos_importados WHERE id = ANY(%s)", (dados.ids,))
            excluidos = cur.rowcount
        return {"status": "ok", "excluidos": excluidos}
    finally:
        conn.close()


@app.post("/api/admin/contatos/abrir-conversa")
def abrir_conversa_contato(dados: AbrirConversaContatoReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contatos_importados WHERE id = %s", (dados.contato_id,))
            contato = cur.fetchone()
            if not contato:
                raise HTTPException(status_code=404, detail="Contato não encontrado")

            jid = contato["jid"]
            nome_exibicao = contato.get("nome") or contato.get("razao_social") or jid.split("@")[0]

            cur.execute("SELECT jid FROM atendimentos WHERE jid = %s", (jid,))
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO atendimentos (jid, nome, setor, status, ultima_mensagem, nao_lidas, atualizado_em)
                    VALUES (%s, %s, %s, 'EM_ATENDIMENTO', 'Conversa iniciada via Contatos Importados', 0, NOW())
                    """,
                    (jid, nome_exibicao, dados.setor_destino),
                )
            else:
                cur.execute(
                    "UPDATE atendimentos SET setor = %s, status = 'EM_ATENDIMENTO', atualizado_em = NOW() WHERE jid = %s",
                    (dados.setor_destino, jid),
                )
        return {"status": "ok", "jid": jid}
    finally:
        conn.close()


# --- TEMPLATES E DISPARO EM MASSA ---
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
                "INSERT INTO templates_disparo (titulo, conteudo) VALUES (%s, %s) RETURNING id",
                (dados.titulo.strip(), dados.conteudo.strip()),
            )
            novo_id = cur.fetchone()["id"]
        return {"status": "ok", "id": novo_id}
    finally:
        conn.close()


@app.put("/api/admin/templates/{template_id}")
def editar_template(template_id: int, dados: TemplateReq, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE templates_disparo SET titulo = %s, conteudo = %s WHERE id = %s",
                (dados.titulo.strip(), dados.conteudo.strip(), template_id),
            )
        return {"status": "ok"}
    finally:
        conn.close()


@app.delete("/api/admin/templates/{template_id}")
def excluir_template(template_id: int, request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM templates_disparo WHERE id = %s", (template_id,))
        return {"status": "ok"}
    finally:
        conn.close()


async def executar_campanha_background(
    contatos: List[Dict[str, Any]],
    template_texto: str,
    setor_destino: str,
    autor: str,
    enviar_ambos_numeros: bool = False,
):
    fila_envios = []
    for contato in contatos:
        jids = extrair_jids_contato(contato, enviar_ambos=enviar_ambos_numeros)
        if jids:
            fila_envios.append((contato, jids))

    total_mensagens = sum(len(jids) for _, jids in fila_envios)
    CAMPANHA_STATUS["em_execucao"] = True
    CAMPANHA_STATUS["cancelar"] = False
    CAMPANHA_STATUS["total"] = total_mensagens
    CAMPANHA_STATUS["enviados"] = 0
    CAMPANHA_STATUS["falhas"] = 0
    interrompido = False

    try:
        for contato, jids in fila_envios:
            if CAMPANHA_STATUS.get("cancelar"):
                interrompido = True
                break

            nome_contato = contato.get("nome") or contato.get("razao_social") or jids[0].split("@")[0]
            mensagem_final = aplicar_variaveis_template(template_texto, contato)
            algum_sucesso = False
            tentou_algum = False

            for idx, jid in enumerate(jids):
                if CAMPANHA_STATUS.get("cancelar"):
                    interrompido = True
                    break

                tentou_algum = True
                tag_num = f" [{idx + 1}/{len(jids)}]" if len(jids) > 1 else ""
                CAMPANHA_STATUS["atual"] = f"{nome_contato} ({jid.split('@')[0]}){tag_num}"
                CAMPANHA_STATUS["mensagem_status"] = f"Enviando para {nome_contato} ({jid.split('@')[0]}){tag_num}..."

                ok = await enviar_texto_wa(jid, mensagem_final)
                if ok:
                    CAMPANHA_STATUS["enviados"] += 1
                    algum_sucesso = True
                    registrar_mensagem(
                        jid=jid,
                        remetente=f"CAMPANHA ({autor})",
                        conteudo=mensagem_final,
                        nome=nome_contato,
                        incrementar_nao_lidas=False,
                        tipo_midia="TEXT",
                        setor_padrao=setor_destino,
                        status_padrao="EM_ATENDIMENTO",
                    )
                else:
                    CAMPANHA_STATUS["falhas"] += 1

            if tentou_algum and (algum_sucesso or not interrompido):
                conn = get_db_connection()
                try:
                    with conn.cursor() as cur:
                        if algum_sucesso:
                            cur.execute(
                                "UPDATE contatos_importados SET status_contato = 'enviou', ultimo_disparo = NOW() WHERE id = %s",
                                (contato["id"],),
                            )
                        else:
                            cur.execute(
                                "UPDATE contatos_importados SET status_contato = 'falha' WHERE id = %s",
                                (contato["id"],),
                            )
                finally:
                    conn.close()

            if interrompido:
                break
    finally:
        CAMPANHA_STATUS["em_execucao"] = False
        CAMPANHA_STATUS["cancelar"] = False
        CAMPANHA_STATUS["atual"] = ""
        prefixo = "⏹️ Envio interrompido pelo usuário" if interrompido else "✅ Concluído"
        CAMPANHA_STATUS["mensagem_status"] = (
            f"{prefixo}: {CAMPANHA_STATUS['enviados']} enviados, {CAMPANHA_STATUS['falhas']} falhas."
        )


@app.post("/api/admin/disparo-massa")
async def iniciar_disparo_massa(dados: DisparoMassaReq, request: Request, background_tasks: BackgroundTasks):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    if CAMPANHA_STATUS["em_execucao"]:
        raise HTTPException(status_code=400, detail="Já existe um disparo em massa em andamento.")

    status_filtro = (dados.status_filtro or "nao_enviado").strip().lower()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM templates_disparo WHERE id = %s", (dados.template_id,))
            tpl = cur.fetchone()
            if not tpl:
                raise HTTPException(status_code=404, detail="Template de mensagem não encontrado.")

            if status_filtro == "selecionados":
                if not dados.contato_ids:
                    raise HTTPException(status_code=400, detail="Marque pelo menos um contato no grid antes de disparar.")
                cur.execute(
                    "SELECT * FROM contatos_importados WHERE id = ANY(%s) ORDER BY id ASC",
                    (dados.contato_ids,),
                )
            elif status_filtro == "todos":
                cur.execute("SELECT * FROM contatos_importados ORDER BY id ASC")
            else:
                cur.execute("SELECT * FROM contatos_importados WHERE status_contato = %s ORDER BY id ASC", (status_filtro,))
            contatos = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not contatos:
        raise HTTPException(status_code=400, detail=f"Nenhum contato encontrado para o filtro '{status_filtro}'.")

    total_destinos = sum(len(extrair_jids_contato(c, dados.enviar_ambos_numeros)) for c in contatos)

    background_tasks.add_task(
        executar_campanha_background,
        contatos,
        tpl["conteudo"],
        dados.setor_destino,
        user["nome"],
        dados.enviar_ambos_numeros,
    )
    return {"status": "iniciado", "total": total_destinos, "contatos": len(contatos)}


@app.post("/api/admin/disparo-massa/parar")
def parar_disparo_massa(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)

    if not CAMPANHA_STATUS["em_execucao"]:
        raise HTTPException(status_code=400, detail="Nenhum disparo em massa está em execução no momento.")

    CAMPANHA_STATUS["cancelar"] = True
    CAMPANHA_STATUS["mensagem_status"] = "Interrompendo disparo após a mensagem atual..."
    return {"status": "interrompendo"}


@app.get("/api/admin/disparo-massa/status")
def status_disparo_massa(request: Request):
    user = obter_usuario_atual(request)
    exigir_admin(user)
    return CAMPANHA_STATUS


# --- ROTAS DE ATENDIMENTO ---
@app.get("/")
async def health_check():
    return {"status": "online", "service": "tottem-omnidesk-supabase"}


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
                SELECT a.*, TO_CHAR(a.atualizado_em, 'HH24:MI') as hora_fmt,
                       c.cnpj, c.razao_social, c.telefone AS contato_telefone, c.celular AS contato_celular
                FROM atendimentos a
                LEFT JOIN contatos_importados c ON a.jid = c.jid
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
                       TO_CHAR(criado_em, 'HH24:MI') as hora
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
# INTERFACE ENTERPRISE OMNICHANNEL + CONTATOS IMPORTADOS (/painel)
# ==============================================================================
HTML_PAINEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tottem OmniDesk — Atendimento & Prospecção</title>
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
            <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Tottem OmniDesk
          </div>
          <h1 class="text-3xl font-bold tracking-tight leading-tight">Central de Atendimento & Prospecção</h1>
          <p class="text-slate-300 text-sm mt-4 leading-relaxed">
            Gestão omnichannel por departamentos, gerenciamento de contatos importados, sincronização com cadastro de testes/vendas e disparos inteligentes.
          </p>
        </div>
        <div class="text-xs text-slate-400 border-t border-slate-700/80 pt-4">
          Tottem Tecnologia • Conectado ao Supabase
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

  <!-- MODAL DE PREVIEW E MAPEAMENTO DA PLANILHA -->
  <div id="modalPreviewPlanilha" class="hidden fixed inset-0 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
    <div class="bg-white rounded-2xl shadow-2xl w-full max-w-6xl max-h-[90vh] flex flex-col overflow-hidden">
      <div class="px-6 py-4 bg-slate-900 text-white flex items-center justify-between">
        <div>
          <h3 class="text-base font-bold">Mapeamento de Colunas e Prévia da Importação</h3>
          <p class="text-xs text-slate-300 mt-0.5">
            Arquivo: <strong id="prevNomeArquivo" class="text-emerald-400"></strong> • Total de registros detectados: <strong id="prevTotalLinhas">0</strong>
          </p>
        </div>
        <button onclick="fecharModalPreview()" class="text-slate-400 hover:text-white text-xl font-bold px-2">&times;</button>
      </div>

      <div class="p-6 bg-slate-50 border-b border-slate-200">
        <div class="text-xs font-bold uppercase tracking-wider text-slate-500 mb-3">
          Vincule cada campo à coluna correspondente da sua planilha:
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-5 gap-4">
          <div class="bg-white p-3 rounded-xl border border-slate-200 shadow-sm">
            <label class="block text-xs font-bold text-slate-700 mb-1">CNPJ</label>
            <select id="mapColCnpj" onchange="atualizarHighlightPreview()" class="w-full border border-slate-300 rounded-lg px-2.5 py-2 text-xs bg-white"></select>
          </div>
          <div class="bg-white p-3 rounded-xl border border-slate-200 shadow-sm">
            <label class="block text-xs font-bold text-slate-700 mb-1">Razão Social</label>
            <select id="mapColRazao" onchange="atualizarHighlightPreview()" class="w-full border border-slate-300 rounded-lg px-2.5 py-2 text-xs bg-white"></select>
          </div>
          <div class="bg-white p-3 rounded-xl border border-slate-200 shadow-sm">
            <label class="block text-xs font-bold text-slate-700 mb-1">Nome / Contato</label>
            <select id="mapColNome" onchange="atualizarHighlightPreview()" class="w-full border border-slate-300 rounded-lg px-2.5 py-2 text-xs bg-white"></select>
          </div>
          <div class="bg-white p-3 rounded-xl border border-emerald-300 shadow-sm">
            <label class="block text-xs font-bold text-emerald-700 mb-1">Celular (WhatsApp) *</label>
            <select id="mapColCelular" onchange="atualizarHighlightPreview()" class="w-full border border-emerald-400 rounded-lg px-2.5 py-2 text-xs bg-white"></select>
          </div>
          <div class="bg-white p-3 rounded-xl border border-slate-200 shadow-sm">
            <label class="block text-xs font-bold text-slate-700 mb-1">Telefone Fixo / Alt.</label>
            <select id="mapColTelefone" onchange="atualizarHighlightPreview()" class="w-full border border-slate-300 rounded-lg px-2.5 py-2 text-xs bg-white"></select>
          </div>
        </div>
      </div>

      <div class="flex-1 overflow-auto p-6">
        <div class="text-xs font-semibold text-slate-600 mb-2">
          Pré-visualização das primeiras linhas da planilha:
        </div>
        <div class="border border-slate-200 rounded-xl overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead id="prevTableHead" class="bg-slate-100 border-b border-slate-200 text-slate-700 font-bold"></thead>
            <tbody id="prevTableBody" class="divide-y divide-slate-100"></tbody>
          </table>
        </div>
      </div>

      <div class="px-6 py-4 bg-slate-50 border-t border-slate-200 flex items-center justify-between">
        <span class="text-xs text-slate-500">
          * Registros cujo CNPJ já esteja marcado como <strong>COMPROU</strong> na tabela <code>pessoas</code> serão ignorados automaticamente.
        </span>
        <div class="flex items-center gap-3">
          <button onclick="fecharModalPreview()" class="px-4 py-2.5 rounded-xl border border-slate-300 text-slate-700 font-semibold text-xs hover:bg-slate-100">
            Cancelar
          </button>
          <button id="btnConfirmarImportacao" onclick="confirmarImportacaoMapeada()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-6 py-2.5 rounded-xl text-xs shadow-sm transition">
            Confirmar e Importar Contatos
          </button>
        </div>
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

      <button onclick="mudarAba('contatos')" id="railContatos" title="Contatos Importados & Disparos em Massa"
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
          <span class="w-2 h-2 rounded-full bg-emerald-500"></span> Supabase + WhatsApp Conectados
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
          <p class="text-xs text-slate-400 mt-1">Escolha um cliente na fila à esquerda ou inicie uma conversa pela aba Contatos Importados.</p>
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

      <aside id="painelDireito" class="hidden w-72 bg-white border-l border-slate-200 flex flex-col p-4 space-y-5 overflow-y-auto">
        <div id="blocoDadosEmpresa" class="hidden bg-emerald-50/60 border border-emerald-200/80 rounded-xl p-3 space-y-1.5">
          <div class="text-[10px] font-bold uppercase tracking-wider text-emerald-700">Dados Cadastrais</div>
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
          <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">Anotações Internas</h3>
          <textarea id="notasCrmInput" rows="5" placeholder="Registre observações sobre este cliente..."
                    class="w-full border border-slate-200 rounded-xl p-2.5 text-xs focus:outline-none focus:border-emerald-500 bg-slate-50"></textarea>
          <button onclick="salvarNotas()" class="mt-2 bg-slate-800 hover:bg-slate-900 text-white text-xs font-semibold py-2 rounded-lg transition">
            Salvar Anotação
          </button>
        </div>
      </aside>
    </div>

    <!-- =================================================================== -->
    <!-- ABA 2: CONTATOS IMPORTADOS, SINCRONIZAÇÃO PESSOAS E DISPAROS        -->
    <!-- =================================================================== -->
    <div id="abaContatos" class="hidden flex-1 overflow-y-auto p-6">
      <div class="max-w-7xl mx-auto space-y-6">

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

          <!-- CARD 1: IMPORTAR PLANILHA COM PREVIEW -->
          <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
              <span class="text-[11px] font-bold uppercase tracking-wider text-emerald-600">Passo 1 • Contatos Importados</span>
              <h3 class="text-base font-bold text-slate-800 mt-1">Importar Planilha de Contatos</h3>
              <p class="text-xs text-slate-500 mt-1 leading-relaxed">
                Importe <strong>.xlsx</strong> ou <strong>.csv</strong> para a tabela <code>contatos_importados</code> (sem tocar em <code>clientes</code> ou <code>pessoas</code>).
              </p>
            </div>
            <div class="mt-4 space-y-3">
              <input type="file" id="inputPlanilhaContatos" accept=".xlsx,.xlsm,.csv,.txt"
                     onchange="abrirPreviewPlanilha()"
                     class="block w-full text-xs text-slate-500 file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-semibold file:bg-emerald-50 file:text-emerald-700 hover:file:bg-emerald-100 cursor-pointer" />
              <button onclick="abrirPreviewPlanilha()" class="w-full bg-slate-900 hover:bg-slate-800 text-white font-semibold py-2.5 rounded-xl text-xs transition">
                Visualizar Planilha e Mapear Colunas
              </button>
              <div id="resumoImportacao" class="hidden bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-xl p-2.5 text-xs font-medium"></div>
            </div>
          </div>

          <!-- CARD 2: SELECIONAR, EDITAR E CADASTRAR MENSAGENS -->
          <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
              <span class="text-[11px] font-bold uppercase tracking-wider text-blue-600">Passo 2 • Gerenciar Mensagens</span>
              <h3 class="text-base font-bold text-slate-800 mt-1">Templates de Abordagem</h3>
              <p class="text-[11px] text-slate-500 mt-1">
                Variáveis: <code>{nome}</code>, <code>{razao_social}</code>, <code>{cnpj}</code>, <code>{celular}</code>
              </p>
              <div class="mt-3 space-y-2">
                <div class="flex gap-1.5">
                  <select id="selectTemplateGerenciar" onchange="carregarTemplateParaEdicao()" class="flex-1 border border-slate-300 rounded-xl px-2.5 py-2 text-xs bg-slate-50 font-medium">
                    <option value="">-- Nova Mensagem --</option>
                  </select>
                  <button type="button" onclick="novoTemplateForm()" title="Limpar para cadastrar nova mensagem" class="bg-slate-200 hover:bg-slate-300 text-slate-800 font-semibold px-3 py-2 rounded-xl text-xs transition">
                    + Nova
                  </button>
                </div>
                <input id="tplTitulo" type="text" placeholder="Título da campanha (ex: Prospecção Edital)"
                       class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs" />
                <textarea id="tplConteudo" rows="3" placeholder="Olá {nome} da {razao_social}..."
                          class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs"></textarea>
              </div>
            </div>
            <div class="mt-3 flex gap-2">
              <button onclick="salvarTemplate()" id="btnSalvarTemplate" class="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-xl text-xs transition">
                Salvar Mensagem
              </button>
              <button onclick="excluirTemplateAtual()" id="btnExcluirTemplate" class="hidden bg-rose-50 hover:bg-rose-600 text-rose-700 hover:text-white border border-rose-200 font-semibold px-3.5 py-2.5 rounded-xl text-xs transition">
                Excluir
              </button>
            </div>
          </div>

          <!-- CARD 3: EXECUTAR E PARAR DISPARO EM MASSA -->
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
                <div>
                  <label class="block text-[11px] font-semibold text-slate-600 mb-1">Público / Status dos Contatos para Envio</label>
                  <select id="selectStatusDisparo" class="w-full border border-slate-300 rounded-xl px-3 py-2 text-xs bg-white font-medium text-slate-700">
                    <option value="selecionados" id="optStatusSelecionados">selecionados (0 marcados no grid)</option>
                    <option value="nao_enviado" selected>nao_enviado (Apenas não contatados)</option>
                    <option value="nao_comprou">nao_comprou (Repescagem pós-teste)</option>
                    <option value="enviou">enviou (Follow-up)</option>
                    <option value="falha">falha (Tentar novamente)</option>
                    <option value="todos">todos (Toda a lista importada)</option>
                  </select>
                </div>
                <label class="flex items-center gap-2 text-xs text-slate-700 pt-1 cursor-pointer">
                  <input type="checkbox" id="chkEnviarAmbosNumeros" class="rounded text-emerald-600" />
                  <span>Enviar também para o <strong>Telefone</strong> (se for diferente do Celular)</span>
                </label>
              </div>
            </div>
            <div class="mt-3 space-y-2">
              <button id="btnIniciarDisparo" onclick="iniciarDisparoMassa()" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2.5 rounded-xl text-xs shadow-sm transition">
                Iniciar Envio em Massa
              </button>
              <button id="btnPararDisparo" onclick="pararDisparoMassa()" class="hidden w-full bg-rose-600 hover:bg-rose-700 text-white font-semibold py-2.5 rounded-xl text-xs shadow-sm transition">
                ⏹️ Parar Envio em Massa
              </button>
              <div id="boxProgressoCampanha" class="hidden bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-[11px] text-slate-700"></div>
            </div>
          </div>
        </div>

        <!-- TABELA DE CONTATOS IMPORTADOS + BOTÃO SINCRONIZAR PESSOAS -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
          <div class="px-6 py-4 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-sm font-bold text-slate-800">Contatos Importados para Prospecção</h3>
              <p class="text-xs text-slate-500">
                Exibindo: <strong id="totalContatosBadge">0</strong> contatos •
                <span class="text-slate-400">Registros marcados como <strong>COMPROU</strong> na tabela <code>pessoas</code> são removidos ao sincronizar.</span>
              </p>
            </div>
            <div class="flex flex-wrap items-center gap-2.5">
              <button id="btnSincronizarPessoas" onclick="sincronizarComTabelaPessoas()"
                      class="bg-indigo-600 hover:bg-indigo-700 text-white font-semibold px-4 py-2 rounded-xl text-xs shadow-sm transition inline-flex items-center gap-1.5">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                <span>Sincronizar com Tabela Pessoas</span>
              </button>

              <button id="btnExcluirMassaContatos" onclick="excluirContatosEmMassa()" class="hidden bg-rose-600 hover:bg-rose-700 text-white font-semibold px-3.5 py-2 rounded-xl text-xs shadow-sm transition">
                Excluir Selecionados (<span id="qtdContatosSelecionados">0</span>)
              </button>

              <select id="filtroStatusTabela" onchange="renderizarTabelaContatos()" class="border border-slate-300 rounded-xl px-3 py-2 text-xs bg-slate-50 font-medium text-slate-700">
                <option value="TODOS">Status: Todos</option>
                <option value="nao_enviado">nao_enviado</option>
                <option value="enviou">enviou</option>
                <option value="falha">falha</option>
                <option value="nao_comprou">nao_comprou</option>
              </select>

              <input id="buscaContatos" oninput="renderizarTabelaContatos()" type="text" placeholder="Filtrar CNPJ, Razão Social, Nome ou Telefone..."
                     class="w-72 border border-slate-300 rounded-xl px-3.5 py-2 text-xs focus:outline-none focus:border-emerald-500" />
            </div>
          </div>

          <div id="bannerSincronizacao" class="hidden px-6 py-3 bg-indigo-50 border-b border-indigo-200 text-indigo-900 text-xs flex items-center justify-between">
            <span id="textoSincronizacao"></span>
            <button onclick="document.getElementById('bannerSincronizacao').classList.add('hidden')" class="font-bold text-indigo-600 hover:text-indigo-900">&times;</button>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="bg-slate-50 border-b border-slate-200 uppercase text-slate-500 font-semibold">
                <tr>
                  <th class="px-4 py-3 w-10">
                    <input type="checkbox" id="chkSelecionarTodosContatos" onclick="alternarSelecionarTodosContatos(this)" class="rounded text-emerald-600 cursor-pointer" title="Selecionar todos visíveis" />
                  </th>
                  <th class="px-4 py-3">CNPJ</th>
                  <th class="px-4 py-3">Razão Social</th>
                  <th class="px-4 py-3">Contato / Nome</th>
                  <th class="px-4 py-3">Celular / Telefone</th>
                  <th class="px-4 py-3">Status</th>
                  <th class="px-4 py-3 text-right">Ações</th>
                </tr>
              </thead>
              <tbody id="tabelaContatosBody" class="divide-y divide-slate-100"></tbody>
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
            Evite que o seu número do WhatsApp seja detectado como spam ou banido adicionando atrasos aleatórios inteligentes entre as mensagens enviadas.
          </p>
        </div>

        <div class="bg-orange-50/70 border border-orange-200/80 rounded-2xl p-5 flex items-center justify-between gap-4">
          <div class="flex-1 text-center">
            <h3 class="text-sm font-bold text-orange-700">Habilitar atraso anti-spam</h3>
            <p class="text-xs text-slate-600 mt-1">
              Quando ativado, as mensagens serão enfileiradas e enviadas com um atraso aleatório se o limite de taxa for atingido.
            </p>
          </div>
          <label class="relative inline-flex items-center cursor-pointer shrink-0">
            <input type="checkbox" id="abHabilitado" class="sr-only peer" checked>
            <div class="w-12 h-6 bg-slate-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-6 peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-emerald-600"></div>
          </label>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 pt-2">
          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Limite de mensagens</label>
            <input id="abLimite" type="number" min="1" value="5"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Janela de tempo (segundos)</label>
            <input id="abJanela" type="number" min="1" value="10"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Atraso Mínimo (ms)</label>
            <input id="abMinMs" type="number" min="0" step="100" value="1000"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
          </div>

          <div>
            <label class="block text-sm font-bold text-slate-800 mb-1.5">Atraso Máximo (ms)</label>
            <input id="abMaxMs" type="number" min="0" step="100" value="3000"
                   class="w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:border-emerald-600" />
          </div>
        </div>

        <div class="flex items-center gap-4">
          <button onclick="salvarConfigAntiBan()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-6 py-3 rounded-xl text-sm shadow-sm transition">
            Salvar configurações de proteção
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
    let CONTATOS_CACHE = [];
    let CONTATOS_SELECIONADOS = new Set();
    let TEMPLATES_CACHE = [];
    let PREVIEW_DATA = null;
    let JID_ATUAL = null;
    let intervalo = null;
    let mediaRecorder = null;
    let audioChunks = [];
    let CAMPANHA_ESTAVA_RODANDO = false;

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
        ["railContatos", "railAntiban", "railPerfis", "railUsuarios", "filtroAdminSetor"].forEach(id => {
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
      ["abaAtendimento", "abaContatos", "abaAntiban", "abaPerfis", "abaUsuarios"].forEach(id => document.getElementById(id).classList.add("hidden"));
      ["railAtendimento", "railContatos", "railAntiban", "railPerfis", "railUsuarios"].forEach(id => {
        const el = document.getElementById(id);
        el.classList.remove("bg-slate-800", "text-white");
        el.classList.add("text-slate-400");
      });

      const mapa = {
        atendimento: ["abaAtendimento", "railAtendimento", "Atendimentos"],
        contatos: ["abaContatos", "railContatos", "Contatos Importados & Disparos em Massa"],
        antiban: ["abaAntiban", "railAntiban", "Proteção Anti-Banimento"],
        perfis: ["abaPerfis", "railPerfis", "Perfis & Departamentos"],
        usuarios: ["abaUsuarios", "railUsuarios", "Equipe & Vínculos"],
      };
      const [divId, railId, titulo] = mapa[aba];
      document.getElementById(divId).classList.remove("hidden");
      document.getElementById(railId).classList.add("bg-slate-800", "text-white");
      document.getElementById("tituloPagina").innerText = titulo;

      if (aba === "contatos") {
        carregarContatos();
        carregarTemplates();
        verificarProgressoCampanha();
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
      if (res.status === 401) return sair();
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

    // --- SINCRONIZAÇÃO COM TABELA PESSOAS ---
    async function sincronizarComTabelaPessoas() {
      const btn = document.getElementById("btnSincronizarPessoas");
      btn.disabled = true;
      btn.classList.add("opacity-60");

      try {
        const res = await fetch("/api/admin/contatos/sincronizar-pessoas", {
          method: "POST",
          headers: authHeaders()
        });
        const data = await res.json();
        if (res.ok) {
          const banner = document.getElementById("bannerSincronizacao");
          document.getElementById("textoSincronizacao").innerHTML =
            `✅ Sincronização com <code>pessoas</code> (${data.total_lido_pessoas} lidos): <strong>${data.removidos_comprou}</strong> removido(s) por status <strong>COMPROU</strong> • <strong>${data.marcados_nao_comprou}</strong> atualizado(s) e <strong>${data.novos_nao_comprou}</strong> importado(s) como <strong>nao_comprou</strong>.`;
          banner.classList.remove("hidden");
          await carregarContatos();
        } else {
          alert(data.detail || "Erro ao sincronizar com a tabela pessoas.");
        }
      } finally {
        btn.disabled = false;
        btn.classList.remove("opacity-60");
      }
    }

    // --- PREVIEW E MAPEAMENTO DE PLANILHA ---
    function autoDetectarColuna(colunas, palavrasChave) {
      for (let i = 0; i < colunas.length; i++) {
        const norm = colunas[i].toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim();
        if (palavrasChave.some(p => norm === p || norm.includes(p))) {
          return String(i);
        }
      }
      return "-1";
    }

    async function abrirPreviewPlanilha() {
      const input = document.getElementById("inputPlanilhaContatos");
      if (!input.files.length) return alert("Selecione uma planilha .xlsx ou .csv primeiro.");

      const formData = new FormData();
      formData.append("arquivo", input.files[0]);

      try {
        const res = await fetch("/api/admin/contatos/preview", {
          method: "POST",
          headers: authHeaders(false),
          body: formData
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          return alert(err.detail || "Não foi possível ler a planilha. Verifique o formato.");
        }

        PREVIEW_DATA = await res.json();
        document.getElementById("prevNomeArquivo").innerText = PREVIEW_DATA.filename;
        document.getElementById("prevTotalLinhas").innerText = PREVIEW_DATA.total_linhas;

        const opcoes = `<option value="-1">-- Não importar --</option>` +
          PREVIEW_DATA.colunas.map((c, idx) => `<option value="${idx}">${c}</option>`).join("");

        ["mapColCnpj", "mapColRazao", "mapColNome", "mapColCelular", "mapColTelefone"].forEach(id => {
          document.getElementById(id).innerHTML = opcoes;
        });

        document.getElementById("mapColCnpj").value = autoDetectarColuna(PREVIEW_DATA.colunas, ["cnpj", "documento"]);
        document.getElementById("mapColRazao").value = autoDetectarColuna(PREVIEW_DATA.colunas, ["razao", "empresa", "fantasia"]);
        document.getElementById("mapColNome").value = autoDetectarColuna(PREVIEW_DATA.colunas, ["nome", "contato", "socio", "responsavel"]);
        document.getElementById("mapColCelular").value = autoDetectarColuna(PREVIEW_DATA.colunas, ["celular", "whatsapp", "cel", "movel"]);
        document.getElementById("mapColTelefone").value = autoDetectarColuna(PREVIEW_DATA.colunas, ["telefone", "fone", "tel"]);

        atualizarHighlightPreview();
        document.getElementById("modalPreviewPlanilha").classList.remove("hidden");
      } catch (e) {
        alert("Erro de comunicação ao ler a planilha: " + e.message);
      }
    }

    function atualizarHighlightPreview() {
      if (!PREVIEW_DATA) return;
      const mapaSelecionado = {
        [document.getElementById("mapColCnpj").value]: "CNPJ",
        [document.getElementById("mapColRazao").value]: "RAZÃO SOCIAL",
        [document.getElementById("mapColNome").value]: "NOME",
        [document.getElementById("mapColCelular").value]: "CELULAR",
        [document.getElementById("mapColTelefone").value]: "TELEFONE",
      };

      document.getElementById("prevTableHead").innerHTML = `<tr>` +
        PREVIEW_DATA.colunas.map((col, idx) => {
          const tag = mapaSelecionado[String(idx)];
          return `
            <th class="px-4 py-3 ${tag ? 'bg-emerald-50 text-emerald-900 border-b-2 border-emerald-500' : ''}">
              <div>${col}</div>
              ${tag ? `<span class="inline-block mt-1 text-[10px] bg-emerald-600 text-white px-2 py-0.5 rounded-full">→ ${tag}</span>` : `<span class="inline-block mt-1 text-[10px] text-slate-400">Ignorada</span>`}
            </th>
          `;
        }).join("") + `</tr>`;

      document.getElementById("prevTableBody").innerHTML = PREVIEW_DATA.preview.map(row => `
        <tr class="hover:bg-slate-50">
          ${PREVIEW_DATA.colunas.map((_, idx) => {
            const vinculado = Boolean(mapaSelecionado[String(idx)]);
            return `<td class="px-4 py-2.5 ${vinculado ? 'bg-emerald-50/30 font-medium text-slate-900' : 'text-slate-400'}">${row[idx] || '-'}</td>`;
          }).join("")}
        </tr>
      `).join("");
    }

    function fecharModalPreview() {
      document.getElementById("modalPreviewPlanilha").classList.add("hidden");
    }

    async function confirmarImportacaoMapeada() {
      const input = document.getElementById("inputPlanilhaContatos");
      if (!input.files.length) return;

      const colCel = document.getElementById("mapColCelular").value;
      const colTel = document.getElementById("mapColTelefone").value;
      if (colCel === "-1" && colTel === "-1") {
        return alert("Vincule pelo menos a coluna de Celular ou Telefone antes de importar.");
      }

      const btn = document.getElementById("btnConfirmarImportacao");
      btn.disabled = true;
      btn.innerText = "Importando registros...";

      const formData = new FormData();
      formData.append("arquivo", input.files[0]);
      formData.append("col_cnpj", document.getElementById("mapColCnpj").value);
      formData.append("col_razao", document.getElementById("mapColRazao").value);
      formData.append("col_nome", document.getElementById("mapColNome").value);
      formData.append("col_celular", colCel);
      formData.append("col_telefone", colTel);

      try {
        const res = await fetch("/api/admin/contatos/importar-mapeado", {
          method: "POST",
          headers: authHeaders(false),
          body: formData
        });
        const data = await res.json();
        if (res.ok) {
          fecharModalPreview();
          input.value = "";
          const resumo = document.getElementById("resumoImportacao");
          resumo.innerHTML = `✅ Importação concluída! <strong>${data.importados}</strong> gravados (${data.ignorados_ja_comprou} já compraram, ${data.ignorados} sem telefone válido).`;
          resumo.classList.remove("hidden");
          await carregarContatos();
        } else {
          alert(data.detail || "Erro ao importar os dados.");
        }
      } finally {
        btn.disabled = false;
        btn.innerText = "Confirmar e Importar Contatos";
      }
    }

    // --- CONTATOS IMPORTADOS, TEMPLATES E DISPARO ---
    async function carregarContatos() {
      const res = await fetch("/api/admin/contatos", { headers: authHeaders() });
      if (!res.ok) return;
      CONTATOS_CACHE = await res.json();
      const idsValidos = new Set(CONTATOS_CACHE.map(c => c.id));
      CONTATOS_SELECIONADOS = new Set([...CONTATOS_SELECIONADOS].filter(id => idsValidos.has(id)));
      renderizarTabelaContatos();
    }

    function obterContatosFiltrados() {
      const termo = (document.getElementById("buscaContatos").value || "").toLowerCase();
      const statusFiltro = document.getElementById("filtroStatusTabela").value;
      return CONTATOS_CACHE.filter(c => {
        const bateStatus = statusFiltro === "TODOS" || (c.status_contato || "") === statusFiltro;
        const bateTexto =
          (c.cnpj || "").toLowerCase().includes(termo) ||
          (c.razao_social || "").toLowerCase().includes(termo) ||
          (c.nome || "").toLowerCase().includes(termo) ||
          (c.status_contato || "").toLowerCase().includes(termo) ||
          (c.celular || "").includes(termo) ||
          (c.telefone || "").includes(termo);
        return bateStatus && bateTexto;
      });
    }

    function atualizarBarraExclusaoMassa(filtrados) {
      const btnMassa = document.getElementById("btnExcluirMassaContatos");
      const spanQtd = document.getElementById("qtdContatosSelecionados");
      const chkTodos = document.getElementById("chkSelecionarTodosContatos");
      const optSel = document.getElementById("optStatusSelecionados");

      spanQtd.innerText = CONTATOS_SELECIONADOS.size;
      if (optSel) {
        optSel.innerText = `selecionados (${CONTATOS_SELECIONADOS.size} marcados no grid)`;
      }
      if (CONTATOS_SELECIONADOS.size > 0) {
        btnMassa.classList.remove("hidden");
      } else {
        btnMassa.classList.add("hidden");
      }

      if (filtrados.length > 0 && filtrados.every(c => CONTATOS_SELECIONADOS.has(c.id))) {
        chkTodos.checked = true;
      } else {
        chkTodos.checked = false;
      }
    }

    function alternarSelecaoContato(id, checked) {
      if (checked) CONTATOS_SELECIONADOS.add(id);
      else CONTATOS_SELECIONADOS.delete(id);
      atualizarBarraExclusaoMassa(obterContatosFiltrados());
    }

    function alternarSelecionarTodosContatos(checkbox) {
      const filtrados = obterContatosFiltrados();
      if (checkbox.checked) {
        filtrados.forEach(c => CONTATOS_SELECIONADOS.add(c.id));
      } else {
        filtrados.forEach(c => CONTATOS_SELECIONADOS.delete(c.id));
      }
      renderizarTabelaContatos();
    }

    async function excluirContatosEmMassa() {
      const ids = Array.from(CONTATOS_SELECIONADOS);
      if (!ids.length) return;
      if (!confirm(`Deseja realmente excluir ${ids.length} contato(s) selecionado(s)?`)) return;

      const res = await fetch("/api/admin/contatos/excluir-massa", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ ids })
      });
      if (res.ok) {
        CONTATOS_SELECIONADOS.clear();
        await carregarContatos();
      } else {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Erro ao excluir contatos em massa.");
      }
    }

    async function alterarStatusContato(id, novoStatus) {
      const res = await fetch(`/api/admin/contatos/${id}/status`, {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify({ status_contato: novoStatus })
      });
      if (res.ok) {
        const item = CONTATOS_CACHE.find(c => c.id === id);
        if (item) item.status_contato = novoStatus;
        renderizarTabelaContatos();
      }
    }

    function renderizarTabelaContatos() {
      const filtrados = obterContatosFiltrados();
      document.getElementById("totalContatosBadge").innerText = filtrados.length;
      atualizarBarraExclusaoMassa(filtrados);

      const corStatus = {
        nao_enviado: "bg-sky-100 text-sky-800 border-sky-200",
        enviou: "bg-emerald-100 text-emerald-800 border-emerald-200",
        falha: "bg-rose-100 text-rose-800 border-rose-200",
        nao_comprou: "bg-amber-100 text-amber-800 border-amber-200",
      };

      document.getElementById("tabelaContatosBody").innerHTML = filtrados.map(c => {
        const st = c.status_contato || "nao_enviado";
        return `
          <tr class="hover:bg-slate-50 ${CONTATOS_SELECIONADOS.has(c.id) ? 'bg-rose-50/40' : ''}">
            <td class="px-4 py-3">
              <input type="checkbox" ${CONTATOS_SELECIONADOS.has(c.id) ? 'checked' : ''}
                     onchange="alternarSelecaoContato(${c.id}, this.checked)"
                     class="rounded text-emerald-600 cursor-pointer" />
            </td>
            <td class="px-4 py-3 font-mono text-slate-600">${c.cnpj || '-'}</td>
            <td class="px-4 py-3 font-semibold text-slate-800">${c.razao_social || '-'}</td>
            <td class="px-4 py-3 text-slate-700">${c.nome || '-'}</td>
            <td class="px-4 py-3 font-mono text-slate-700">${[c.celular, c.telefone].filter(Boolean).join(" / ") || '-'}</td>
            <td class="px-4 py-3">
              <select onchange="alterarStatusContato(${c.id}, this.value)"
                      class="px-2 py-1 rounded-full text-[11px] font-bold border cursor-pointer ${corStatus[st] || 'bg-slate-100 text-slate-700 border-slate-200'}">
                <option value="nao_enviado" ${st === 'nao_enviado' ? 'selected' : ''}>nao_enviado</option>
                <option value="enviou" ${st === 'enviou' ? 'selected' : ''}>enviou</option>
                <option value="falha" ${st === 'falha' ? 'selected' : ''}>falha</option>
                <option value="nao_comprou" ${st === 'nao_comprou' ? 'selected' : ''}>nao_comprou</option>
              </select>
            </td>
            <td class="px-4 py-3 text-right space-x-2">
              <button onclick="abrirConversaDoContato(${c.id})" class="bg-emerald-50 hover:bg-emerald-600 text-emerald-700 hover:text-white border border-emerald-200 px-2.5 py-1 rounded-lg font-semibold transition">
                Abrir Chat
              </button>
              <button onclick="excluirContato(${c.id})" class="text-rose-600 hover:underline">Excluir</button>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function abrirConversaDoContato(contatoId) {
      const setor = document.getElementById("selectSetorDisparo").value || "COMERCIAL";
      const res = await fetch("/api/admin/contatos/abrir-conversa", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ contato_id: contatoId, setor_destino: setor })
      });
      if (res.ok) {
        const data = await res.json();
        mudarAba("atendimento");
        await atualizarFila();
        selecionarChat(data.jid);
      }
    }

    async function excluirContato(id) {
      if (!confirm("Remover este contato da lista importada?")) return;
      await fetch(`/api/admin/contatos/${id}`, { method: "DELETE", headers: authHeaders() });
      CONTATOS_SELECIONADOS.delete(id);
      carregarContatos();
    }

    async function carregarTemplates(idSelecionado = null) {
      const res = await fetch("/api/admin/templates", { headers: authHeaders() });
      if (!res.ok) return;
      TEMPLATES_CACHE = await res.json();

      document.getElementById("selectTemplateDisparo").innerHTML = TEMPLATES_CACHE.map(t =>
        `<option value="${t.id}">${t.titulo}</option>`
      ).join("");

      const selGerenciar = document.getElementById("selectTemplateGerenciar");
      const valorAtual = idSelecionado !== null ? String(idSelecionado) : selGerenciar.value;
      selGerenciar.innerHTML = `<option value="">-- Cadastrar Nova Mensagem --</option>` +
        TEMPLATES_CACHE.map(t => `<option value="${t.id}">${t.titulo}</option>`).join("");

      if (valorAtual && TEMPLATES_CACHE.some(t => String(t.id) === String(valorAtual))) {
        selGerenciar.value = String(valorAtual);
        carregarTemplateParaEdicao();
      } else {
        novoTemplateForm();
      }
    }

    function carregarTemplateParaEdicao() {
      const tplId = document.getElementById("selectTemplateGerenciar").value;
      if (!tplId) {
        novoTemplateForm();
        return;
      }
      const tpl = TEMPLATES_CACHE.find(t => String(t.id) === String(tplId));
      if (!tpl) return;

      document.getElementById("tplTitulo").value = tpl.titulo;
      document.getElementById("tplConteudo").value = tpl.conteudo;
      document.getElementById("btnSalvarTemplate").innerText = "Atualizar Mensagem";
      document.getElementById("btnExcluirTemplate").classList.remove("hidden");
      document.getElementById("selectTemplateDisparo").value = String(tpl.id);
    }

    function novoTemplateForm() {
      document.getElementById("selectTemplateGerenciar").value = "";
      document.getElementById("tplTitulo").value = "";
      document.getElementById("tplConteudo").value = "";
      document.getElementById("btnSalvarTemplate").innerText = "+ Cadastrar Mensagem";
      document.getElementById("btnExcluirTemplate").classList.add("hidden");
    }

    async function salvarTemplate() {
      const tplId = document.getElementById("selectTemplateGerenciar").value;
      const titulo = document.getElementById("tplTitulo").value.trim();
      const conteudo = document.getElementById("tplConteudo").value.trim();
      if (!titulo || !conteudo) return alert("Informe o título e o conteúdo da mensagem.");

      if (tplId) {
        const res = await fetch(`/api/admin/templates/${tplId}`, {
          method: "PUT", headers: authHeaders(), body: JSON.stringify({ titulo, conteudo })
        });
        if (res.ok) {
          await carregarTemplates(tplId);
          alert("Mensagem atualizada com sucesso!");
        }
      } else {
        const res = await fetch("/api/admin/templates", {
          method: "POST", headers: authHeaders(), body: JSON.stringify({ titulo, conteudo })
        });
        if (res.ok) {
          const data = await res.json();
          await carregarTemplates(data.id || "");
          alert("Mensagem cadastrada com sucesso!");
        }
      }
    }

    async function excluirTemplateAtual() {
      const tplId = document.getElementById("selectTemplateGerenciar").value;
      if (!tplId || !confirm("Deseja realmente excluir esta mensagem cadastrada?")) return;
      const res = await fetch(`/api/admin/templates/${tplId}`, {
        method: "DELETE", headers: authHeaders()
      });
      if (res.ok) {
        await carregarTemplates("");
        alert("Mensagem excluída.");
      }
    }

    async function iniciarDisparoMassa() {
      const template_id = parseInt(document.getElementById("selectTemplateDisparo").value);
      const setor_destino = document.getElementById("selectSetorDisparo").value;
      const status_filtro = document.getElementById("selectStatusDisparo").value;
      const enviar_ambos_numeros = document.getElementById("chkEnviarAmbosNumeros").checked;
      const contato_ids = Array.from(CONTATOS_SELECIONADOS);

      if (!template_id) return alert("Cadastre ou selecione uma mensagem primeiro.");
      if (status_filtro === "selecionados" && contato_ids.length === 0) {
        return alert("Marque pelo menos um contato na tabela abaixo antes de iniciar o envio.");
      }

      const alvoDesc = status_filtro === "selecionados"
        ? `os ${contato_ids.length} contato(s) marcado(s) no grid`
        : `contatos com status '${status_filtro}'`;
      const extraMsg = enviar_ambos_numeros ? " (incluindo Telefone quando diferente do Celular)" : "";
      if (!confirm(`Deseja iniciar o envio em massa para ${alvoDesc}${extraMsg} respeitando as regras Anti-Banimento?`)) return;

      const res = await fetch("/api/admin/disparo-massa", {
        method: "POST", headers: authHeaders(),
        body: JSON.stringify({ template_id, setor_destino, status_filtro, enviar_ambos_numeros, contato_ids })
      });
      if (res.ok) {
        verificarProgressoCampanha();
      } else {
        const err = await res.json();
        alert(err.detail || "Erro ao iniciar disparo.");
      }
    }

    async function pararDisparoMassa() {
      if (!confirm("Deseja realmente interromper o envio em massa agora?")) return;
      const btnParar = document.getElementById("btnPararDisparo");
      btnParar.disabled = true;
      btnParar.innerText = "Interrompendo...";

      try {
        const res = await fetch("/api/admin/disparo-massa/parar", {
          method: "POST",
          headers: authHeaders()
        });
        if (res.ok) {
          verificarProgressoCampanha();
        } else {
          const err = await res.json().catch(() => ({}));
          alert(err.detail || "Não foi possível parar o disparo.");
        }
      } finally {
        btnParar.disabled = false;
        btnParar.innerText = "⏹️ Parar Envio em Massa";
      }
    }

    async function verificarProgressoCampanha() {
      const res = await fetch("/api/admin/disparo-massa/status", { headers: authHeaders() });
      if (!res.ok) return;
      const st = await res.json();

      const btnIniciar = document.getElementById("btnIniciarDisparo");
      const btnParar = document.getElementById("btnPararDisparo");
      const box = document.getElementById("boxProgressoCampanha");

      if (st.em_execucao) {
        btnIniciar.classList.add("hidden");
        btnParar.classList.remove("hidden");
        CAMPANHA_ESTAVA_RODANDO = true;
      } else {
        btnIniciar.classList.remove("hidden");
        btnParar.classList.add("hidden");
        if (CAMPANHA_ESTAVA_RODANDO) {
          CAMPANHA_ESTAVA_RODANDO = false;
          carregarContatos();
        }
      }

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
      if (res.status === 401) return sair();
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
