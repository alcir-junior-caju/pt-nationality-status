"""Consulta o estado de um processo de nacionalidade no portal Meu Registo (IRN).

Abre a página de consulta no Camoufox, resolve o hCaptcha com o
hcaptcha-challenger (Gemini) e faz o POST para a ação ActionGetPedidoData_New
de dentro do próprio navegador, com os cookies da página.

Uso:
    uv run consulta_nacionalidade.py                  # senha de acesso do .env
    uv run consulta_nacionalidade.py 1234-5678-9012
    uv run consulta_nacionalidade.py --headless

A configuração fica no arquivo .env (veja .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

# O hcaptcha-challenger desenha uma grade sobre o print do desafio com o
# matplotlib. No macOS, o backend padrão (macosx) gera a imagem com o dobro de
# pixels numa tela Retina e quebra o pacote ("cannot reshape array ...").
# O Agg desenha sempre em escala 1; por isso é definido aqui, antes de importar
# o hcaptcha-challenger.
matplotlib.use("Agg")

from camoufox.async_api import AsyncCamoufox
from camoufox.fingerprints import Screen
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from hcaptcha_challenger import AgentConfig, AgentV
from hcaptcha_challenger.models import ChallengeSignal
from hcaptcha_challenger.tools.internal.providers.gemini import GeminiProvider
from playwright.async_api import Error as PlaywrightError, Page
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text
from tenacity import RetryError

ORIGEM = "https://meu.registo.justica.gov.pt"
URL_CONSULTA = f"{ORIGEM}/Pedidos/Consultar-estado-do-processo-de-nacionalidade"
SCRIPT_DA_TELA = "/Pedidos/scripts/Pedidos.Consulta.ConsultarEstadoDoProcessoDeNacionalidade.mvc.js"
VIEW_NAME = "Consulta.ConsultarEstadoDoProcessoDeNacionalidade"
# Token CSRF fixo que o runtime do OutSystems usa para visitantes sem login.
CSRF_ANONIMO = "T6C+9iB49TLra4jEsMeSckDMNhQ="

# Impressão digital do mesmo sistema da máquina. Por padrão o Camoufox sorteia
# Windows, macOS ou Linux, e aí as fontes e a escala de tela fingidas não batem
# com as reais.
SISTEMA = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")

# Modelos do Gemini em ordem de preferência (GEMINI_MODELOS no .env). O primeiro
# resolve os desafios; o seguinte entra quando o anterior falha ou não responde
# a tempo. Os padrões do hcaptcha-challenger (gemini-2.5-*) não aceitam mais
# chaves novas da API (erro 404).
MODELOS_PADRAO = "gemini-3.8-flash,gemini-3.1-flash-lite"
CAMPOS_DE_MODELO = (
    "CHALLENGE_CLASSIFIER_MODEL",
    "IMAGE_CLASSIFIER_MODEL",
    "SPATIAL_POINT_REASONER_MODEL",
    "SPATIAL_PATH_REASONER_MODEL",
)
modelos_gemini: list[str] = []  # preenchida em main()

TEMPO_MAXIMO_CHAMADA = 60  # segundos por modelo, somando as 3 tentativas do pacote
TEMPO_MAXIMO_CAPTCHA = 180  # segundos
PERFIL_NAVEGADOR = Path("tmp/camoufox")
PASTA_SAIDA = Path("saida")

# Etapas com os rótulos do site (Submissão e Aguarda resposta não aparecem em
# texto lá) e as situações do componente de etapas do OutSystems UI.
ETAPAS = {
    "Submission": "Submissão",
    "Analysis": "Em análise",
    "AwaitingReply": "Aguarda resposta",
    "ForDecision": "Para decisão",
    "Completed": "Concluído",
}
SITUACOES = {"active": "ativa", "next": "pendente", "past": "concluída"}

# Roda dentro da página, então o POST sai do Camoufox com os cookies dela.
# O CSRF segue a regra do runtime do OutSystems: valor "crf" do cookie
# nr2<provedor> ou, para visitante sem login, o token anônimo.
POST_NA_PAGINA = """
async ({ url, corpo, csrfAnonimo }) => {
  const cookie = document.cookie.split("; ").find((c) => c.startsWith("nr2"));
  const crf = cookie && /crf=([^;]+)/.exec(decodeURIComponent(cookie.slice(cookie.indexOf("=") + 1)));
  const resposta = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json; charset=UTF-8",
      Accept: "application/json",
      "X-CSRFToken": crf ? crf[1] : csrfAnonimo,
    },
    body: JSON.stringify(corpo),
  });
  return { status: resposta.status, texto: await resposta.text() };
}
"""

# O Camoufox roda o page.evaluate num contexto isolado, sem acesso ao
# window.hcaptcha da página. O DOM é compartilhado, então lemos o campo que
# o widget preenche ao ser resolvido.
LER_TOKEN = """() => document.querySelector('[name="h-captcha-response"]')?.value || ''"""


class ErroConsulta(Exception):
    pass


def senha_de_acesso(valor: str) -> str:
    """Aceita a senha com ou sem traços e devolve no formato xxxx-xxxx-xxxx da tela."""
    caracteres = re.sub(r"[^0-9A-Za-z]", "", valor)
    if len(caracteres) != 12:
        raise argparse.ArgumentTypeError("a senha de acesso tem 12 caracteres, ex.: 1234-5678-9012")
    return f"{caracteres[:4]}-{caracteres[4:8]}-{caracteres[8:]}"


async def descobrir_acao(page: Page) -> tuple[str, dict[str, str]]:
    """Lê do site a URL da ação e as versões que o OutSystems exige no corpo do POST.

    As versões mudam a cada publicação do site; com versão desatualizada o
    servidor sinaliza hasApiVersionChanged em vez de responder normalmente.
    """

    async def baixar(caminho: str):
        resposta = await page.request.get(ORIGEM + caminho)
        if not resposta.ok:
            raise ErroConsulta(f"GET {caminho} respondeu HTTP {resposta.status}")
        return resposta

    modulo = await (await baixar("/Pedidos/moduleservices/moduleversioninfo")).json()
    manifesto = await (await baixar("/Pedidos/moduleservices/moduleinfo")).json()
    versao_do_script = manifesto["manifest"]["urlVersions"].get(SCRIPT_DA_TELA)
    if versao_do_script is None:
        raise ErroConsulta("o script da tela de consulta sumiu do manifesto; o site mudou")
    script = await (await baixar(SCRIPT_DA_TELA + versao_do_script)).text()

    achado = re.search(r'callServerAction\("GetPedidoData_New", "([^"]+)", "([^"]+)"', script)
    if achado is None:
        raise ErroConsulta("não achei a ação GetPedidoData_New no script da tela; o site mudou")
    caminho, api_version = achado.groups()
    return f"{ORIGEM}/Pedidos/{caminho}", {
        "moduleVersion": modulo["versionToken"],
        "apiVersion": api_version,
    }


async def esperar_token(page: Page) -> str:
    while not (token := await page.evaluate(LER_TOKEN)):
        await asyncio.sleep(1)
    return token


async def avisar_progresso(headless: bool) -> None:
    dica = "" if headless else " Se quiser, resolva o desafio na janela: o script segue sozinho."
    for decorrido in range(20, TEMPO_MAXIMO_CAPTCHA, 20):
        await asyncio.sleep(20)
        print(f"Ainda resolvendo o hCaptcha ({decorrido}s de {TEMPO_MAXIMO_CAPTCHA}s).{dica}", file=sys.stderr)


async def resolver_hcaptcha(page: Page, agente: AgentV, headless: bool) -> str:
    checkbox = page.frame_locator(agente.robotic_arm.checkbox_selector).locator("#checkbox")
    await checkbox.wait_for(state="visible", timeout=60_000)
    await agente.robotic_arm.click_checkbox()

    # O token aparece no campo da página em dois casos: quando o hCaptcha aprova
    # direto, sem desafio (aí o Gemini nem é chamado), e depois que o agente
    # resolve um desafio. O agente sozinho não trata o primeiro caso: fica
    # esperando um desafio que não vem. Por isso as duas esperas correm juntas.
    token = asyncio.create_task(esperar_token(page))
    agente_resolvendo = asyncio.create_task(agente.wait_for_challenge())
    progresso = asyncio.create_task(avisar_progresso(headless))
    try:
        await asyncio.wait(
            {token, agente_resolvendo},
            timeout=TEMPO_MAXIMO_CAPTCHA,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if token.done():
            return token.result()
        if not agente_resolvendo.done():
            raise ErroConsulta(f"o hCaptcha não foi resolvido em {TEMPO_MAXIMO_CAPTCHA}s")
        try:
            sinal = agente_resolvendo.result()
        except Exception as erro:
            raise ErroConsulta(f"o hcaptcha-challenger falhou: {erro!r}") from erro
        if sinal != ChallengeSignal.SUCCESS:
            raise ErroConsulta(f"o hCaptcha não foi resolvido ({sinal.value})")

        # Desafio resolvido: o widget preenche o campo logo em seguida.
        try:
            return await asyncio.wait_for(token, 10)
        except asyncio.TimeoutError:
            if agente.cr_list and agente.cr_list[-1].generated_pass_UUID:
                return agente.cr_list[-1].generated_pass_UUID
            raise ErroConsulta("o hCaptcha passou, mas o token não apareceu na página") from None
    finally:
        token.cancel()
        agente_resolvendo.cancel()
        progresso.cancel()


async def consultar(page: Page, url: str, versao: dict[str, str], senha: str, token: str) -> dict:
    corpo = {
        "versionInfo": versao,
        "viewName": VIEW_NAME,
        "inputParameters": {"UserGuid": senha, "Token": token},
    }
    resultado = await page.evaluate(
        POST_NA_PAGINA, {"url": url, "corpo": corpo, "csrfAnonimo": CSRF_ANONIMO}
    )
    if resultado["status"] != 200:
        raise ErroConsulta(f"o POST respondeu HTTP {resultado['status']}: {resultado['texto'][:300]}")
    resposta = json.loads(resultado["texto"])

    if excecao := resposta.get("exception"):
        tipo = excecao.get("specificType") or excecao.get("name")
        mensagem = f"o site recusou a consulta ({tipo}: {excecao.get('message')})"
        if tipo == "Pedidos.NotAllowed":
            # É o que o site responde a um token de hCaptcha inválido.
            mensagem += ". Acontece com token do hCaptcha inválido ou expirado; rode de novo."
        raise ErroConsulta(mensagem)
    if resposta.get("versionInfo", {}).get("hasApiVersionChanged"):
        raise ErroConsulta("o site foi atualizado no meio da consulta; rode de novo")
    return resposta["data"]


_gerar_original = GeminiProvider.generate_with_images


async def _gerar_trocando_de_modelo(self: GeminiProvider, **kwargs):
    """generate_with_images do hcaptcha-challenger, trocando de modelo quando ele falha.

    O pacote insiste no mesmo modelo até o desafio estourar o tempo. Aqui, se o
    Gemini devolver erro nas 3 tentativas do pacote (como o 503 de alta demanda)
    ou não responder em TEMPO_MAXIMO_CHAMADA, a chamada passa para o próximo
    modelo de GEMINI_MODELOS, que fica valendo para essa ferramenta até o fim.
    """
    while True:
        try:
            return await asyncio.wait_for(_gerar_original(self, **kwargs), TEMPO_MAXIMO_CHAMADA)
        except (RetryError, TimeoutError) as erro:
            if isinstance(erro, TimeoutError):
                motivo = f"sem resposta em {TEMPO_MAXIMO_CHAMADA}s"
            elif isinstance(causa := erro.last_attempt.exception(), genai_errors.APIError):
                motivo = f"HTTP {causa.code}"
            else:
                raise
            if self._model in modelos_gemini:
                restantes = modelos_gemini[modelos_gemini.index(self._model) + 1 :]
            else:
                restantes = modelos_gemini
            if not restantes:
                raise
            print(f"Gemini: {self._model} falhou ({motivo}); trocando para {restantes[0]}.", file=sys.stderr)
            self._model = restantes[0]


GeminiProvider.generate_with_images = _gerar_trocando_de_modelo


def verificar_gemini(config: AgentConfig) -> None:
    """Testa a chave e os modelos do Gemini antes de abrir o navegador.

    Sem isso, um modelo indisponível só aparece no meio do desafio, e o
    hcaptcha-challenger fica tentando de novo até estourar o tempo.
    """
    client = genai.Client(api_key=config.GEMINI_API_KEY.get_secret_value())
    modelos = {
        config.CHALLENGE_CLASSIFIER_MODEL,
        config.IMAGE_CLASSIFIER_MODEL,
        config.SPATIAL_POINT_REASONER_MODEL,
        config.SPATIAL_PATH_REASONER_MODEL,
        *modelos_gemini,
    }
    for modelo in sorted(modelos):
        try:
            client.models.generate_content(model=modelo, contents="Responda apenas: ok")
        except genai_errors.ServerError:
            pass  # instabilidade momentânea; o hcaptcha-challenger tenta de novo
        except genai_errors.ClientError as erro:
            if erro.code != 429:
                raise ErroConsulta(
                    f"o Gemini recusou o modelo {modelo} (HTTP {erro.code}): {erro.message}"
                ) from erro
            print(f"Aviso: cota do Gemini no limite para {modelo}; o desafio pode falhar.", file=sys.stderr)


async def executar(senha: str, headless: bool, config: AgentConfig) -> dict:
    async with AsyncCamoufox(
        headless=headless,
        os=SISTEMA,
        persistent_context=True,
        user_data_dir=str(PERFIL_NAVEGADOR),
        screen=Screen(max_width=1920, max_height=1080),
        humanize=0.2,
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        # O agente escuta as respostas da API do hCaptcha, então precisa
        # existir antes de a página carregar o widget.
        agente = AgentV(page=page, agent_config=config)

        print("Abrindo a página de consulta...", file=sys.stderr)
        await page.goto(URL_CONSULTA, wait_until="domcontentloaded")
        url, versao = await descobrir_acao(page)

        print("Resolvendo o hCaptcha...", file=sys.stderr)
        token = await resolver_hcaptcha(page, agente, headless)

        print("Consultando o pedido...", file=sys.stderr)
        return await consultar(page, url, versao, senha, token)


def itens(lista) -> list:
    # Listas do OutSystems chegam como {"List": [...]}.
    if isinstance(lista, dict):
        return lista.get("List") or []
    return lista or []


def linhas_do_resumo(dados: dict) -> list[dict[str, str]]:
    """Linhas da tabela, em duas seções: dados do pedido e andamento."""
    # Mesma lógica da tela: sem IsSuccess, ela mostra só os dados da pesquisa
    # (quando Status e Number vêm preenchidos) ou a mensagem de erro.
    if not dados.get("IsSuccess"):
        busca = dados.get("SeachData") or {}
        if busca.get("Status") and busca.get("Number"):
            mensagem = "\n".join(t for t in (busca.get("Notification_Title"), busca.get("Message")) if t)
            return [{"Processo": busca["Number"], "Local do pedido": busca.get("Location", ""), "Mensagem": mensagem}]
        if dados.get("Error"):
            return [{"Resultado": "O site retornou erro para essa senha de acesso."}]
        return [{"Resultado": "Não existem resultados."}]

    mandatarios = [
        ", ".join(p for p in (m.get("Name"), m.get("Number") and f"cédula {m['Number']}", m.get("Email")) if p)
        for m in itens(dados.get("MandatarioList"))
    ]
    notificacoes = []
    for n in itens(dados.get("Notifications")):
        data = str(n.get("Data_Notification") or "")[:10]
        if data.startswith("1900-01-01"):  # data vazia no OutSystems
            data = ""
        notificacoes.append(f"{data} {n.get('Notification_Title') or n.get('Notification', '')}".strip())
    pedido = {
        "Requerente": dados.get("FullName_Requerente", ""),
        "Processo": dados.get("Number", ""),
        "Local do pedido": dados.get("Location", ""),
        "Mandatários": "\n".join(mandatarios) or "nenhum",
        "Notificações": "\n".join(notificacoes) or "nenhuma",
    }

    andamento = {}
    for chave, nome in ETAPAS.items():
        etapa = (dados.get("Timeline") or {}).get(chave) or {}
        if not etapa.get("Status"):
            continue  # sem situação, o site também não mostra a etapa
        texto = SITUACOES.get(etapa["Status"], etapa["Status"])
        andamento[nome] = f"{texto} ({etapa['Date']})" if etapa.get("Date") else texto
    return [pedido, andamento]


def achatar(valor, caminho: str = "") -> dict[str, object]:
    """Transforma a resposta em pares caminho → valor, para comparar campo a campo."""
    if isinstance(valor, dict):
        pares = {}
        for chave, item in valor.items():
            if chave != "EmptyListItem":  # modelo fixo de item vazio do OutSystems
                pares |= achatar(item, f"{caminho}.{chave}" if caminho else chave)
        return pares
    if isinstance(valor, list):
        pares = {}
        for i, item in enumerate(valor):
            pares |= achatar(item, f"{caminho}[{i}]")
        return pares or {caminho: []}
    return {caminho: valor}


def campos_alterados(anterior: dict, atual: dict) -> list[str]:
    antes, depois = achatar(anterior), achatar(atual)
    # Agrupa por item de lista (uma notificação nova vira um caminho só) e tira
    # o "X.List" de lista vazia quando o item que entrou ou saiu já aparece.
    caminhos = {
        re.sub(r"(\[\d+\]).*", r"\1", c) for c in antes.keys() | depois.keys() if antes.get(c) != depois.get(c)
    }
    return sorted(c for c in caminhos if not any(o.startswith(f"{c}[") for o in caminhos))


def ultima_consulta(senha: str) -> tuple[dict, datetime] | None:
    """Resposta e data da consulta mais recente dessa senha salva em PASTA_SAIDA."""
    arquivos = sorted(PASTA_SAIDA.glob(f"{senha}_*.json"))
    if not arquivos:
        return None
    quando = datetime.strptime(arquivos[-1].stem.split("_", 1)[1], "%Y%m%d-%H%M%S")
    return json.loads(arquivos[-1].read_text(encoding="utf-8")), quando


def imprimir_comparativo(senha: str, atual: dict, quando: datetime, anterior: tuple[dict, datetime] | None) -> None:
    console = Console()
    tabela = Table(title=f"Pedido {senha}", title_justify="left", title_style="bold")
    tabela.add_column("")
    tabela.add_column(f"Atual ({quando:%d/%m/%Y %H:%M})")
    if anterior:
        dados_anteriores, quando_anterior = anterior
        tabela.add_column(f"Anterior ({quando_anterior:%d/%m/%Y %H:%M})")
        valores_anteriores = {r: v for secao in linhas_do_resumo(dados_anteriores) for r, v in secao.items()}

    linhas_alteradas = []
    for i, secao in enumerate(linhas_do_resumo(atual)):
        if i:
            tabela.add_section()
        for rotulo, valor in secao.items():
            if not anterior:
                tabela.add_row(Text(rotulo, style="bold"), Text(valor))
                continue
            antes = valores_anteriores.pop(rotulo, "—")
            if antes == valor:
                tabela.add_row(Text(rotulo, style="bold"), Text(valor), Text("igual", style="dim"))
            else:
                linhas_alteradas.append(rotulo)
                tabela.add_row(Text(rotulo), Text(valor), Text(antes), style="bold yellow")
    if anterior:
        for rotulo, antes in valores_anteriores.items():  # linhas que sumiram
            linhas_alteradas.append(rotulo)
            tabela.add_row(Text(rotulo), Text("—"), Text(antes), style="bold yellow")

    console.print()
    console.print(tabela)
    if not anterior:
        console.print("Primeira consulta deste pedido. As próximas serão comparadas com esta.")
    elif campos := campos_alterados(dados_anteriores, atual):
        onde = ", ".join(linhas_alteradas) or "campos que não aparecem na tabela"
        console.print(Text(f"⚠ Mudou desde a consulta de {quando_anterior:%d/%m/%Y %H:%M}: {onde}.", style="bold yellow"))
        console.print(Text(f"Campos alterados na resposta: {', '.join(campos)}", style="dim"))
    else:
        console.print(Text(f"✔ Sem mudanças desde a consulta de {quando_anterior:%d/%m/%Y %H:%M}.", style="bold green"))


def env_ativo(nome: str) -> bool:
    return os.environ.get(nome, "").strip().lower() in {"1", "true", "sim", "yes"}


def main() -> int:
    # Carrega o .env da pasta do script; variáveis já definidas no ambiente têm prioridade.
    load_dotenv()
    lista = os.environ.get("GEMINI_MODELOS") or MODELOS_PADRAO
    modelos_gemini[:] = [m.strip() for m in lista.split(",") if m.strip()]
    for campo in CAMPOS_DE_MODELO:
        os.environ.setdefault(campo, modelos_gemini[0])

    parser = argparse.ArgumentParser(
        description="Consulta o estado de um processo de nacionalidade no portal Meu Registo (IRN)."
    )
    parser.add_argument(
        "senha",
        nargs="?",
        type=senha_de_acesso,
        default=os.environ.get("SENHA_ACESSO") or None,
        help="senha de acesso do pedido, ex.: 1234-5678-9012 (padrão: SENHA_ACESSO do .env)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=env_ativo("HEADLESS"),
        help="roda sem abrir a janela do navegador (padrão: HEADLESS do .env)",
    )
    args = parser.parse_args()
    if args.senha is None:
        parser.error("informe a senha de acesso na linha de comando ou em SENHA_ACESSO no .env")

    try:
        # Lê GEMINI_API_KEY do ambiente ou do .env. O Camoufox já humaniza o
        # mouse, por isso a trajetória própria do hcaptcha-challenger fica desligada.
        config = AgentConfig(DISABLE_BEZIER_TRAJECTORY=True)
    except ValidationError:
        config = None
    if config is None or not config.GEMINI_API_KEY.get_secret_value():
        print("Defina GEMINI_API_KEY no arquivo .env (veja .env.example).", file=sys.stderr)
        return 2
    try:
        verificar_gemini(config)
    except ErroConsulta as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 2

    try:
        dados = asyncio.run(executar(args.senha, args.headless, config))
    except KeyboardInterrupt:
        print("Consulta interrompida.", file=sys.stderr)
        return 130
    except ErroConsulta as erro:
        print(f"Erro: {erro}", file=sys.stderr)
        return 1
    except PlaywrightError as erro:
        if "has been closed" in erro.message:
            # Ctrl+C ou janela fechada: o navegador some antes do fim da consulta.
            print("Consulta interrompida: o navegador foi fechado.", file=sys.stderr)
            return 130
        # Timeout ao carregar a página ou o hCaptcha, falha de rede etc.
        print(f"Erro no navegador: {erro.message.splitlines()[0]}", file=sys.stderr)
        return 1

    # A base do comparativo é a última consulta salva, lida antes de gravar a nova.
    anterior = ultima_consulta(args.senha)
    agora = datetime.now()
    PASTA_SAIDA.mkdir(exist_ok=True)
    arquivo = PASTA_SAIDA / f"{args.senha}_{agora:%Y%m%d-%H%M%S}.json"
    arquivo.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")

    imprimir_comparativo(args.senha, dados, agora, anterior)
    print(f"\nResposta completa em {arquivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
