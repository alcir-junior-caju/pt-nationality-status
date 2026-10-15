# consulta-processo

Consulta o estado de um processo de nacionalidade portuguesa no portal
[Meu Registo](https://meu.registo.justica.gov.pt/Pedidos/Consultar-estado-do-processo-de-nacionalidade), do IRN.

O script abre a página no [Camoufox](https://github.com/daijro/camoufox), resolve o hCaptcha com o
[hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger), que usa o Gemini, e faz a consulta
de dentro do navegador.

## Requisitos

- [uv](https://docs.astral.sh/uv/), que instala o Python e as dependências.
- Uma chave da API do Gemini, criada no [Google AI Studio](https://aistudio.google.com/app/apikey).

## Instalação

1. Instale o uv, se ainda não tiver:

   ```bash
   brew install uv
   # ou: curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Crie o arquivo de configuração:

   ```bash
   cp .env.example .env
   ```

   Abra o `.env` e preencha `GEMINI_API_KEY` e `SENHA_ACESSO` (veja [Configuração](#configuração)).

3. Opcional: instale as dependências e baixe o navegador do Camoufox antes da primeira consulta.
   Sem este passo, a primeira execução faz as duas coisas sozinha e demora mais.

   ```bash
   uv sync
   uv run camoufox fetch
   ```

O projeto usa Python 3.13, definido em `.python-version`. Se ele não estiver instalado, o uv baixa
automaticamente. O Python 3.15 ainda não serve, porque faltam pacotes prontos para ele.

## Uso

Rode de dentro da pasta do projeto:

```bash
uv run consulta_nacionalidade.py                  # usa a SENHA_ACESSO do .env
uv run consulta_nacionalidade.py 1234-5678-9012   # consulta outra senha
```

- A senha de acesso pode ir com ou sem traços (`123456789012`).
- `--headless` roda sem abrir a janela do navegador; `--no-headless` abre a janela mesmo com `HEADLESS=true`
  no `.env`.

O terminal mostra uma tabela com os dados do pedido (requerente, processo, local, mandatários e
notificações) e o andamento, com a situação de cada etapa: ativa, pendente ou concluída.

Cada consulta fica salva em `saida/<senha>_<data-hora>.json`, e a seguinte é comparada com a última salva da
mesma senha. A coluna "Anterior" mostra "igual" ou o valor antigo, com a linha destacada, e a linha final
diz se houve mudança. A comparação cobre a resposta inteira do site, inclusive campos que não aparecem na
tabela.

Códigos de saída: `0` consulta feita, `1` erro na consulta ou no navegador, `2` configuração ou argumento
inválido (por exemplo, senha de acesso ausente ou modelo do Gemini recusado), `130` consulta interrompida.

## Configuração

A configuração fica no `.env`, que está fora do Git. Variáveis já definidas no ambiente têm prioridade sobre
ele.

| Variável | Obrigatória | Para que serve |
|---|---|---|
| `GEMINI_API_KEY` | Sim | Chave da API do Gemini. Para ficar no plano gratuito, crie num projeto sem cobrança ativada. |
| `SENHA_ACESSO` | Quando a senha não vai no comando | Código do processo, que o site chama de "senha de acesso". A senha passada no comando tem prioridade. |
| `HEADLESS` | Não | `true` roda sem abrir a janela do navegador. O padrão é `false`. |
| `GEMINI_MODELOS` | Não | Modelos do Gemini em ordem de preferência, separados por vírgula. O padrão é `gemini-3.8-flash,gemini-3.1-flash-lite`: o primeiro resolve os desafios e o seguinte entra quando o anterior falha (por exemplo, erro 503 de alta demanda) ou não responde em 60 s. Os `gemini-2.5-*`, padrão do hcaptcha-challenger, não aceitam mais chaves novas, e um modelo Pro, como o `gemini-3.1-pro-preview`, exige cobrança ativada no projeto. |

O hcaptcha-challenger aceita outras opções no `.env`, como tempos de espera e o modelo de cada tarefa
(`CHALLENGE_CLASSIFIER_MODEL`, `IMAGE_CLASSIFIER_MODEL`, `SPATIAL_POINT_REASONER_MODEL` e
`SPATIAL_PATH_REASONER_MODEL`; sem elas, todas começam no primeiro de `GEMINI_MODELOS`). A lista completa está no
[`.env.example` do hcaptcha-challenger](https://github.com/QIN2DIM/hcaptcha-challenger/blob/main/examples/.env.example).

## Como funciona

Antes de abrir o navegador, o script faz uma chamada curta a cada modelo configurado para conferir a chave e
os modelos do Gemini. Assim um modelo indisponível aparece logo, e não no meio do desafio.

1. Abre a página de consulta no Camoufox, com um perfil salvo em `tmp/camoufox` para manter os cookies
   entre execuções.
2. Lê do site a URL da ação e as versões que o OutSystems exige no corpo da requisição. Elas mudam a cada
   atualização do site, por isso não ficam fixas no código.
3. Clica no checkbox do hCaptcha. Se o hCaptcha aprovar direto, o token aparece na página sem nenhuma
   chamada ao Gemini. Se abrir um desafio de imagem, o hcaptcha-challenger resolve com o Gemini, que é quem
   interpreta as imagens. Se o modelo falhar ou não responder, a chamada passa para o próximo de
   `GEMINI_MODELOS`. Com a janela aberta, você também pode resolver o desafio na mão: o script percebe o
   token e segue.
4. Faz o POST para `ActionGetPedidoData_New` com `fetch`, de dentro da página. A requisição leva os cookies
   do navegador e o cabeçalho `X-CSRFToken` tirado do cookie `nr2Users`, como faz o próprio site:

   ```json
   {
     "versionInfo": { "moduleVersion": "...", "apiVersion": "..." },
     "viewName": "Consulta.ConsultarEstadoDoProcessoDeNacionalidade",
     "inputParameters": { "UserGuid": "1234-5678-9012", "Token": "<token do hCaptcha>" }
   }
   ```

   O servidor confere o token do hCaptcha. Sem um token válido, a consulta é recusada.

## Arquivos

| Caminho | Conteúdo |
|---|---|
| `consulta_nacionalidade.py` | O script |
| `.env` | Configuração: chave do Gemini, senha de acesso e opções (fora do Git) |
| `saida/` | Histórico das consultas em JSON; a mais recente de cada senha é a base do comparativo (fora do Git) |
| `tmp/` | Perfil do navegador e registros do hcaptcha-challenger (fora do Git) |

## Problemas comuns

| Mensagem | O que fazer |
|---|---|
| `Defina GEMINI_API_KEY no arquivo .env` | Preencha `GEMINI_API_KEY` no `.env`. Se o arquivo não existir, crie a partir do `.env.example`. |
| `informe a senha de acesso ...` | Preencha `SENHA_ACESSO` no `.env` ou passe a senha no comando. |
| `o Gemini recusou o modelo ...` | O modelo não está disponível para a sua chave. Ajuste `GEMINI_MODELOS` no `.env` ou ative a cobrança no projeto. |
| `Aviso: cota do Gemini no limite ...` | A cota gratuita do modelo acabou. Os limites diários renovam à meia-noite do horário do Pacífico. Enquanto isso, o script usa o próximo modelo de `GEMINI_MODELOS`. |
| `Gemini: ... falhou (HTTP 503); trocando para ...` | É só um aviso: o modelo estava sobrecarregado e o script passou para o próximo da lista. |
| `Ainda resolvendo o hCaptcha ...` | O agente continua trabalhando. Com a janela aberta, você pode resolver o desafio na mão. |
| `Pedidos.NotAllowed` | O site recusou o token do hCaptcha, inválido ou expirado. Rode de novo. |
| `o hCaptcha não foi resolvido` | Rode de novo. Se continuar, rode sem `--headless` para acompanhar o desafio e confira a cota da API do Gemini. |
| `Erro no navegador: ... Timeout ...` | A página ou o hCaptcha não carregou a tempo. Confira se o site está no ar e rode de novo. |
| `... o site mudou` | O IRN alterou a página, e o script precisa de ajuste. |
| Resumo incompleto no terminal | Veja o JSON em `saida/`, que traz a resposta completa do site. |

## Uso responsável

- A senha de acesso dá acesso a dados pessoais do requerente. Consulte apenas pedidos que você está
  autorizado a acompanhar.
- Não faça consultas em sequência rápida: o site tem um firewall de aplicação e pode bloquear o IP. Cada
  consulta também consome chamadas à API do Gemini.
