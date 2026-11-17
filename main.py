import os
import ssl
import logging
from datetime import datetime
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import httpx
import pymysql
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tottem-atendimento")

app = FastAPI(title="Tottem Atendimento - Painel Multi-Setor")

# ==============================================================================
# VARIÁVEIS DE AMBIENTE (RENDER)
# ==============================================================================
WA_BASE_URL = os.getenv("WA_BASE_URL", "https://zap.tottemtecnologia.com.br/api").rstrip("/")
SESSION_ID = os.getenv("SESSION_ID", "")
API_KEY = os.getenv("API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SENHA_PAINEL = os.getenv("SENHA_PAINEL", "tottem2026")  # Altere nas variáveis do Render

HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
}


# ==============================================================================
# CONEXÃO COM MYSQL (AIVEN) E CRIAÇÃO AUTOMÁTICA DAS TABELAS
# ==============================================================================
def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("Variável DATABASE_URL não configurada no Render.")

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mensagens_atendimento (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    jid VARCHAR(100) NOT NULL,
                    remetente VARCHAR(30) NOT NULL,
                    conteudo TEXT NOT NULL,
                    criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_jid (jid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)
        logger.info("Tabelas 'atendimentos' e 'mensagens_atendimento' verificadas com sucesso.")
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    if DATABASE_URL:
        init_db()


# ==============================================================================
# CLIENTE DE ENVIO WA-AKG
# ==============================================================================
async def enviar_texto_wa(jid: str, texto: str) -> bool:
    url = f"{WA_BASE_URL}/messages/{SESSION_ID}/{jid}/send"
    payload = {"message": {"text": texto}}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=HEADERS)
            logger.info(f"Envio WA -> {jid} | Status: {resp.status_code}")
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem WA para {jid}: {e}")
            return False


def registrar_mensagem(jid: str, remetente: str, conteudo: str, nome: str = "", incrementar_nao_lidas: bool = False):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mensagens_atendimento (jid, remetente, conteudo) VALUES (%s, %s, %s)",
                (jid, remetente, conteudo),
            )
            inc = 1 if incrementar_nao_lidas else 0
            cur.execute("""
                INSERT INTO atendimentos (jid, nome, ultima_mensagem, nao_lidas, atualizado_em)
                VALUES (%s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    nome = IF(%s != '', %s, nome),
                    ultima_mensagem = %s,
                    nao_lidas = nao_lidas + %s,
                    atualizado_em = NOW()
            """, (jid, nome, conteudo, inc, nome, nome, conteudo, inc))
    finally:
        conn.close()


# ==============================================================================
# PROCESSAMENTO DO WEBHOOK (MÁQUINA DE ESTADOS: TRIAGEM -> SETOR)
# ==============================================================================
async def processar_webhook(payload: Dict[str, Any]) -> None:
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

    push_name = data.get("pushName") or payload.get("pushName") or jid.split("@")[0]

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

    # Consulta estado atual do cliente no banco
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM atendimentos WHERE jid = %s", (jid,))
            atendimento = cur.fetchone()
    finally:
        conn.close()

    status_atual = atendimento["status"] if atendimento else "TRIAGEM"

    # Salva mensagem recebida do cliente
    registrar_mensagem(
        jid=jid,
        remetente="CLIENTE",
        conteudo=texto,
        nome=push_name,
        incrementar_nao_lidas=(status_atual == "EM_ATENDIMENTO"),
    )

    # Se já está em atendimento humano com um setor, o bot NÃO interfere (exceto se pedir #menu)
    if status_atual == "EM_ATENDIMENTO" and texto.lower() not in {"#menu", "#sair"}:
        return

    # Lógica de Triagem
    if texto == "1":
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE atendimentos SET setor = 'SUPORTE', status = 'EM_ATENDIMENTO', nao_lidas = 1 WHERE jid = %s",
                    (jid,),
                )
        finally:
            conn.close()

        msg = (
            "✅ Você foi direcionado para o *Suporte Técnico*.\n"
            "Descreva sua dúvida ou problema que nossa equipe já vai lhe responder por aqui."
        )
        await enviar_texto_wa(jid, msg)
        registrar_mensagem(jid, "BOT", msg)

    elif texto == "2":
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE atendimentos SET setor = 'COMERCIAL', status = 'EM_ATENDIMENTO', nao_lidas = 1 WHERE jid = %s",
                    (jid,),
                )
        finally:
            conn.close()

        msg = (
            "💼 Você foi direcionado para o setor *Comercial*.\n"
            "Um de nossos consultores já vai dar continuidade ao seu atendimento."
        )
        await enviar_texto_wa(jid, msg)
        registrar_mensagem(jid, "BOT", msg)

    else:
        # Volta para triagem e envia o menu inicial
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE atendimentos SET setor = 'TRIAGEM', status = 'TRIAGEM' WHERE jid = %s",
                    (jid,),
                )
        finally:
            conn.close()

        menu = (
            f"Olá, *{push_name}*! Bem-vindo à *Tottem Tecnologia*.\n\n"
            "Por favor, responda apenas com o número do setor desejado:\n"
            "1️⃣ - *Suporte Técnico*\n"
            "2️⃣ - *Comercial e Orçamentos*"
        )
        await enviar_texto_wa(jid, menu)
        registrar_mensagem(jid, "BOT", menu)


# ==============================================================================
# ROTAS DA API DO PAINEL DE ATENDIMENTO
# ==============================================================================
class EnvioAtendente(BaseModel):
    jid: str
    setor: str
    texto: str


class AcaoTicket(BaseModel):
    jid: str
    novo_setor: Optional[str] = None


def validar_senha(request: Request):
    token = request.headers.get("X-Painel-Senha", "")
    if token != SENHA_PAINEL:
        raise HTTPException(status_code=401, detail="Senha do painel inválida")


@app.get("/")
async def health_check():
    return {"status": "online", "service": "tottem-atendimento"}


@app.post("/webhook/whatsapp")
async def receber_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid_json"}
    background_tasks.add_task(processar_webhook, payload)
    return {"status": "ok"}


@app.get("/api/painel/chats")
def listar_chats(setor: str, request: Request):
    validar_senha(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if setor == "TODOS":
                cur.execute("SELECT * FROM atendimentos ORDER BY atualizado_em DESC LIMIT 100")
            else:
                cur.execute(
                    "SELECT * FROM atendimentos WHERE setor = %s AND status = 'EM_ATENDIMENTO' ORDER BY atualizado_em DESC",
                    (setor,),
                )
            return cur.fetchall()
    finally:
        conn.close()


@app.get("/api/painel/mensagens/{jid}")
def listar_mensagens(jid: str, request: Request):
    validar_senha(request)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE atendimentos SET nao_lidas = 0 WHERE jid = %s", (jid,))
            cur.execute(
                "SELECT id, remetente, conteudo, DATE_FORMAT(criado_em, '%%H:%%i') as hora FROM mensagens_atendimento WHERE jid = %s ORDER BY id ASC LIMIT 200",
                (jid,),
            )
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/api/painel/enviar")
async def responder_cliente(dados: EnvioAtendente, request: Request):
    validar_senha(request)
    sucesso = await enviar_texto_wa(dados.jid, dados.texto)
    if not sucesso:
        raise HTTPException(status_code=500, detail="Falha ao enviar mensagem pelo WA-AKG")

    registrar_mensagem(dados.jid, f"ATENDENTE ({dados.setor})", dados.texto)
    return {"status": "enviado"}


@app.post("/api/painel/transferir")
async def transferir_setor(dados: AcaoTicket, request: Request):
    validar_senha(request)
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

    aviso = f"🔄 Seu atendimento foi transferido para o setor *{dados.novo_setor}*. Aguarde um instante."
    await enviar_texto_wa(dados.jid, aviso)
    registrar_mensagem(dados.jid, "BOT", aviso)
    return {"status": "transferido"}


@app.post("/api/painel/encerrar")
async def encerrar_atendimento(dados: AcaoTicket, request: Request):
    validar_senha(request)
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
# INTERFACE WEB DO PAINEL MULTI-SETOR (/painel)
# ==============================================================================
HTML_PAINEL = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Tottem Atendimento — Multi-Setor</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-100 h-screen flex flex-col font-sans">

  <!-- Tela de Login / Seleção de Setor -->
  <div id="loginModal" class="fixed inset-0 bg-slate-900/80 flex items-center justify-center z-50">
    <div class="bg-white p-8 rounded-2xl shadow-xl w-full max-w-md space-y-5">
      <h1 class="text-2xl font-bold text-slate-800">Tottem Atendimento</h1>
      <p class="text-sm text-slate-500">Selecione seu setor de atuação e informe a senha de acesso.</p>
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">Setor</label>
        <select id="selectSetor" class="w-full border border-slate-300 rounded-lg p-2.5 bg-white">
          <option value="SUPORTE">🛠️ Suporte Técnico</option>
          <option value="COMERCIAL">💼 Comercial e Orçamentos</option>
          <option value="TODOS">👁️ Supervisão (Todos os Setores)</option>
        </select>
      </div>
      <div>
        <label class="block text-sm font-medium text-slate-700 mb-1">Senha do Painel</label>
        <input id="inputSenha" type="password" placeholder="Digite a senha..." class="w-full border border-slate-300 rounded-lg p-2.5" />
      </div>
      <p id="loginErro" class="text-red-600 text-sm hidden">Senha inválida.</p>
      <button onclick="entrar()" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2.5 rounded-lg transition">
        Acessar Fila de Atendimento
      </button>
    </div>
  </div>

  <!-- Barra Superior -->
  <header class="bg-slate-900 text-white px-6 py-3 flex items-center justify-between shadow">
    <div class="flex items-center gap-3">
      <span class="font-bold text-lg">Tottem Tecnologia</span>
      <span id="badgeSetor" class="bg-emerald-600 text-xs font-semibold px-3 py-1 rounded-full"></span>
    </div>
    <button onclick="sair()" class="text-xs bg-slate-700 hover:bg-slate-600 px-3 py-1.5 rounded-lg">Trocar Setor / Sair</button>
  </header>

  <!-- Área Principal -->
  <div class="flex flex-1 overflow-hidden">
    <!-- Lista de Conversas do Setor -->
    <aside class="w-80 bg-white border-r border-slate-200 flex flex-col">
      <div class="p-3 border-b border-slate-200 flex justify-between items-center">
        <span class="font-semibold text-slate-700 text-sm">Fila do Setor</span>
        <span id="contadorChats" class="text-xs bg-slate-200 text-slate-700 px-2 py-0.5 rounded-full">0</span>
      </div>
      <div id="listaChats" class="flex-1 overflow-y-auto divide-y divide-slate-100"></div>
    </aside>

    <!-- Janela de Chat -->
    <main class="flex-1 flex flex-col bg-[#efeae2]">
      <div id="chatVazio" class="flex-1 flex items-center justify-center text-slate-500">
        Selecione um cliente na fila à esquerda para iniciar o atendimento.
      </div>

      <div id="chatAtivo" class="hidden flex-1 flex flex-col h-full">
        <!-- Cabeçalho do Contato -->
        <div class="bg-white px-6 py-3 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h2 id="contatoNome" class="font-bold text-slate-800"></h2>
            <p id="contatoNumero" class="text-xs text-slate-500"></p>
          </div>
          <div class="flex gap-2">
            <button onclick="transferir()" class="text-xs bg-amber-500 hover:bg-amber-600 text-white font-medium px-3 py-1.5 rounded-lg">
              Transferir Setor
            </button>
            <button onclick="encerrar()" class="text-xs bg-rose-600 hover:bg-rose-700 text-white font-medium px-3 py-1.5 rounded-lg">
              Encerrar Atendimento
            </button>
          </div>
        </div>

        <!-- Histórico de Mensagens -->
        <div id="areaMensagens" class="flex-1 overflow-y-auto p-6 space-y-3"></div>

        <!-- Campo de Envio -->
        <form onsubmit="enviarMensagem(event)" class="bg-white p-3 border-t border-slate-200 flex gap-2">
          <input id="inputMensagem" type="text" placeholder="Digite sua resposta para o cliente..." autocomplete="off"
                 class="flex-1 border border-slate-300 rounded-xl px-4 py-2.5 focus:outline-none focus:border-emerald-500" />
          <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-6 py-2.5 rounded-xl">
            Enviar
          </button>
        </form>
      </div>
    </main>
  </div>

  <script>
    let SETOR = localStorage.getItem("tottem_setor") || "";
    let SENHA = localStorage.getItem("tottem_senha") || "";
    let JID_ATUAL = null;
    let intervalo = null;

    if (SETOR && SENHA) {
      iniciarSessao();
    }

    async function entrar() {
      const s = document.getElementById("selectSetor").value;
      const p = document.getElementById("inputSenha").value;
      const res = await fetch(`/api/painel/chats?setor=${s}`, { headers: { "X-Painel-Senha": p } });
      if (res.ok) {
        SETOR = s;
        SENHA = p;
        localStorage.setItem("tottem_setor", SETOR);
        localStorage.setItem("tottem_senha", SENHA);
        iniciarSessao();
      } else {
        document.getElementById("loginErro").classList.remove("hidden");
      }
    }

    function sair() {
      localStorage.removeItem("tottem_setor");
      localStorage.removeItem("tottem_senha");
      location.reload();
    }

    function iniciarSessao() {
      document.getElementById("loginModal").classList.add("hidden");
      document.getElementById("badgeSetor").innerText = "SETOR: " + SETOR;
      atualizarFila();
      if (intervalo) clearInterval(intervalo);
      intervalo = setInterval(() => {
        atualizarFila();
        if (JID_ATUAL) carregarMensagens(JID_ATUAL, false);
      }, 3000);
    }

    async function atualizarFila() {
      const res = await fetch(`/api/painel/chats?setor=${SETOR}`, { headers: { "X-Painel-Senha": SENHA } });
      if (!res.ok) return;
      const chats = await res.json();
      document.getElementById("contadorChats").innerText = chats.length;

      const lista = document.getElementById("listaChats");
      lista.innerHTML = chats.map(c => `
        <div onclick="selecionarChat('${c.jid}', '${(c.nome || '').replace(/'/g, '')}')"
             class="p-3.5 cursor-pointer hover:bg-slate-50 ${JID_ATUAL === c.jid ? 'bg-emerald-50 border-l-4 border-emerald-600' : ''}">
          <div class="flex justify-between items-center">
            <span class="font-semibold text-slate-800 text-sm">${c.nome || c.jid.split('@')[0]}</span>
            ${c.nao_lidas > 0 ? `<span class="bg-emerald-600 text-white text-xs font-bold px-2 py-0.5 rounded-full">${c.nao_lidas}</span>` : ''}
          </div>
          <p class="text-xs text-slate-500 truncate mt-1">${c.ultima_mensagem || ''}</p>
          ${SETOR === 'TODOS' ? `<span class="inline-block mt-1 text-[10px] font-bold px-2 py-0.5 rounded bg-slate-200 text-slate-700">${c.setor}</span>` : ''}
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

    async function carregarMensagens(jid, rolarFim) {
      const res = await fetch(`/api/painel/mensagens/${encodeURIComponent(jid)}`, { headers: { "X-Painel-Senha": SENHA } });
      if (!res.ok) return;
      const msgs = await res.json();
      const area = document.getElementById("areaMensagens");
      area.innerHTML = msgs.map(m => {
        const isCliente = m.remetente === "CLIENTE";
        return `
          <div class="flex ${isCliente ? 'justify-start' : 'justify-end'}">
            <div class="max-w-md rounded-xl px-4 py-2 shadow-sm text-sm ${isCliente ? 'bg-white text-slate-800' : 'bg-emerald-100 text-slate-900'}">
              <div class="text-[10px] font-bold text-slate-500 mb-0.5">${m.remetente}</div>
              <div class="whitespace-pre-line">${m.conteudo}</div>
              <div class="text-[10px] text-right text-slate-400 mt-1">${m.hora}</div>
            </div>
          </div>
        `;
      }).join("");
      if (rolarFim) area.scrollTop = area.scrollHeight;
    }

    async function enviarMensagem(e) {
      e.preventDefault();
      const input = document.getElementById("inputMensagem");
      const texto = input.value.trim();
      if (!texto || !JID_ATUAL) return;

      input.value = "";
      await fetch("/api/painel/enviar", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Painel-Senha": SENHA },
        body: JSON.stringify({ jid: JID_ATUAL, setor: SETOR, texto })
      });
      carregarMensagens(JID_ATUAL, true);
      atualizarFila();
    }

    async function transferir() {
      if (!JID_ATUAL) return;
      const destino = SETOR === "SUPORTE" ? "COMERCIAL" : "SUPORTE";
      if (!confirm(`Transferir este cliente para o setor ${destino}?`)) return;

      await fetch("/api/painel/transferir", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Painel-Senha": SENHA },
        body: JSON.stringify({ jid: JID_ATUAL, novo_setor: destino })
      });
      JID_ATUAL = null;
      document.getElementById("chatAtivo").classList.add("hidden");
      document.getElementById("chatVazio").classList.remove("hidden");
      atualizarFila();
    }

    async function encerrar() {
      if (!JID_ATUAL || !confirm("Encerrar este atendimento e devolver o cliente ao menu inicial?")) return;

      await fetch("/api/painel/encerrar", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Painel-Senha": SENHA },
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
