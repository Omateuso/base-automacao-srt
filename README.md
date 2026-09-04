# base-automatizacao

Ler, classificar e escrever chamados do TomTicket a partir de qualquer
programa — um app de mesa, um serviço, um site.

```powershell
python -m pip install -r requirements.txt
python testar_base.py          # offline, sem rede e sem navegador
python exemplo.py cobranca
python exemplo_web.py          # os chamados da manutenção num endpoint HTTP
```

Duas dependências obrigatórias: `requests` e `PyYAML`. Há um teste que falha se
uma terceira entrar sem querer.

---

## Escolha a porta antes de escrever qualquer linha

Existem **três** formas de chegar aos chamados, e a escolha certa depende de
onde o seu código roda.

| Módulo | O que é | Use quando |
|---|---|---|
| `base.tomticket_api` | a **API oficial** (`api.tomticket.com/v2.0`, Bearer token) | **é um servidor / site.** Contrato publicado e versionado, sem sessão para manter |
| `base.tomticket` | o **console** (`console.tomticket.com`), com o cookie de um login humano | não há token, ou o plano não inclui a API. Bom num app de mesa |
| `base.dashboard` | o dashboard IGEDES (`/api/tickets/sla-alertas`) | você precisa dos campos que só ele tem — projeto, `horas_abertas`, `sla_horas` |

As três desaguam no **mesmo formato**, `base.chamados.Chamado`, então dá para
começar por uma e trocar depois sem reescrever quem consome.

> **Para produção, prefira a API oficial.** O caminho do console depende de um
> cookie obtido por login de navegador e de endpoints internos que podem mudar
> sem aviso e sem versão. Ele existe aqui porque funciona sem depender do plano
> da conta — não porque seja a escolha certa num servidor.

---

## Num site: o coletor

O que um site precisa não é "uma função que busca chamados": é **uma lista que
está sempre em dia**. É isso que o `Coletor` mantém.

```python
from base.coletor import Coletor
from base.tomticket_api import ApiTomTicket

api = ApiTomTicket(os.environ["TOMTICKET_TOKEN"])
coletor = Coletor(api, api.id_do_departamento("MANUTENÇÃO - SRT"))

coletor.sincronizar()          # a primeira: lê tudo que está aberto
...                            # de tempos em tempos:
coletor.sincronizar()          # só o que mudou

coletor.como_json()            # a lista, pronta para o front
```

**A armadilha que o coletor evita.** A leitura incremental óbvia seria "me dê
os chamados **abertos** que mudaram desde a última vez". Ela parece certa e
está errada: quando um chamado é finalizado ele deixa de ser aberto e portanto
não volta na resposta — o site nunca fica sabendo, e a equipe segue vendo uma
parada de rota que já foi resolvida. Então a leitura incremental **não filtra
situação**: pede tudo que mudou e decide.

```
mudou e continua aberto  ->  entra ou atualiza
mudou e fechou           ->  SAI da lista
```

Isso não é hipótese: no programa de onde esta base saiu, 80 chamados
finalizados havia semanas continuavam na tela por exatamente esse motivo.

**Mais três decisões que valem copiar:**

- **A janela tem folga.** Cada leitura pergunta a partir de um instante um
  pouco anterior ao fim da anterior. Relógios de servidores diferentes não
  batem no milissegundo, e uma escrita na fronteira ficaria invisível para
  sempre. Reler alguns a mais custa quase nada; perder um custa uma visita.
- **Falha de rede não zera a lista.** A foto anterior continua de pé e o
  relógio **não avança** — senão a próxima passada pularia justamente a janela
  que falhou.
- **Situação desconhecida conta como aberta.** Se o TomTicket criar um código
  novo, o chamado aparece. Sumir com uma parada de rota por causa de um código
  que ninguém mapeou é o erro caro.

### O desenho, em uma frase

O token nunca sai do servidor.

```
navegador  ->  seu backend  ->  api.tomticket.com
           <-   (JSON seu)  <-
```

Token no front é token público: qualquer visitante abre o DevTools e tem a base
de chamados da empresa. E não chame a API a cada requisição do site — dez
pessoas com o mapa aberto já passariam do limite de **3 requisições por
segundo**. Uma tarefa de fundo mantém a foto; as requisições leem a foto.

`exemplo_web.py` tem isso pronto, com FastAPI, incluindo um `/chamados/estado`
que mostra quando foi a última leitura — sem ele, a coleta pode morrer e o mapa
segue desenhando a última foto sem ninguém perceber.

---

## As três frentes de automação

| Módulo | O que faz |
|---|---|
| `base.cobranca` | classifica pela **última mensagem** e pelo **prazo**: só é cobrável quem tem o texto padrão por último e já passou das 24h/48h |
| `base.finalizacao` | conversas em cache incremental, e a consulta dos chamados abertos de um departamento |
| `base.vigia` | compara a busca com o que já foi visto: o que é novo, qual prazo responder, o que falta fazer |

E as de apoio: `base.chamados` (o formato único), `base.coletor` (a foto em
dia), `base.config` (textos e credenciais), `base.cofre` (senha cifrada pela
DPAPI do Windows) e `base.registro` (por onde a base fala com quem a executa —
ela nunca imprime sozinha).

---

## As travas — leia antes de fazer a base escrever

Elas não são detalhe de implementação: são o motivo de a automação ser
confiável. Se você reaproveitar o código, reaproveite-as.

**Só é cobrável quem tem o texto padrão por último E já passou do prazo.** Uma
mensagem que já diz que o prazo foi excedido nunca vira cobrável — ela *é* a
cobrança. E o `config.py` **recusa** um `config.yaml` em que uma resposta
contenha a assinatura de um gatilho: seria um laço em que a própria resposta
vira motivo para responder de novo, e isso só apareceria no chamado do cliente.

**Confira ao vivo antes de MOSTRAR, não só antes de escrever.** O dashboard é
uma cópia e atrasa; o chamado já cobrado continua na lista de prontos até ele
sincronizar, e quem está olhando a tela marca e manda antes disso.

```python
cobranca.conferir_no_tomticket(sessao, chamados, cfg, registro)
```

Quem já foi cobrado vira `JA_ESCALADO` e sai da lista. Só os `PRONTO` são
lidos, e o que não puder ser conferido **continua pronto** — na dúvida,
oferecer é o erro barato, porque a trava do envio ainda está lá.

A mesma leitura confere o **relógio**. O prazo do dashboard é contado sobre a
cópia dele: se a cópia não tem a resposta de uma hora atrás, a conta sai do
recado anterior e dá "prazo vencido" num chamado que acabou de ser respondido.

**O prazo é um tempo decorrido — então o fuso importa.** O dashboard mistura
`...-03` nas mensagens e `...Z` na data de criação. Largar o fuso sem converter
põe a data três horas no futuro e encurta a conta em três horas, o que num
prazo de 24h é a diferença entre cobrar e não cobrar.

**Leia a conversa ao vivo antes de escrever**, e **releia depois**. "O servidor
aceitou" e "a mensagem chegou" são coisas diferentes quando o que está em jogo
é o que o cliente lê:

```python
if not sessao.conversa_contem(ticket_id, cobranca.assinaturas_de_bloqueio(cfg)):
    sessao.enviar_resposta(ticket_id, cfg.resposta_para(gatilho_id))
    if not sessao.conversa_contem(ticket_id, [texto[:80]]):
        raise RuntimeError("aceitou, mas não apareceu na conversa")
```

**Falha passageira do servidor não é erro seu.** `SessaoDashboard.chamar` e
`ApiTomTicket.chamar` tentam de novo (com pausa) em 429, 500 com
`statement timeout`, 502/503/504 ou falha de rede — e **não** insistem em 401
nem 4xx, onde repetir daria o mesmo. É a diferença entre "o servidor estava
lento" e "a automação quebrou".

**Prioridade fora da tabela não vira prazo.** `vigia.prazo_de("Média")` é
`None`, e é para continuar sendo: escrever "24 horas" num chamado por chute é
pior do que parar e perguntar.

**A linha de base poupa o que acabou de chegar.** Ao vigiar pela primeira vez,
`vigia.linha_de_base(itens)` adota como paisagem tudo *menos* o criado na
última hora. Sem isso, o primeiro uso viraria mil avisos; sem a exceção, o
chamado que entrou três minutos antes seria engolido. Os dois erros já
aconteceram de verdade.

---

## O que não veio junto, e por quê

- **A interface** (janela, tabelas, notificações): é do PHDS, não da automação.
- **O classificador de "pode finalizar"**: são ~700 linhas de regras calibradas
  com os chamados de uma operação específica. A base entrega as conversas no
  cache; a regra de conclusão é sua.
- **O endereço das unidades**: o TomTicket não tem. Para virar rota, case
  `chamado.unidade` com uma tabela sua de `unidade -> lat/lon`.

---

## O login de cada porta

**API oficial:** um token, gerado em Configurações > API no TomTicket, enviado
como `Authorization: Bearer`. Nada mais. Disponível nos planos que incluem API
— confirme com quem administra a conta antes de desenhar em cima disso.

**Console:** não expõe um login simples por HTTP — o endpoint interno pede um
token que só existe dentro do fluxo do próprio app. Então a base aceita a
sessão de duas formas:

```python
sessao.adotar(cookies)                              # de onde você já tiver
sessao.entrar_pelo_navegador(perfil, credenciais)   # exige playwright
```

Depois do primeiro login o cookie fica guardado e o navegador não aparece mais.

**Dashboard IGEDES:** login por HTTP (`POST /api/auth/login`), cookie guardado
em disco, renovado sozinho quando expira. Sem navegador em momento algum.

---

## Segredos

Credenciais vêm do ambiente ou de um `.env` — nunca do código. A senha pode
ficar **cifrada** pela DPAPI do Windows (`base.cofre`): o valor começa com
`dpapi:` e é decifrado na leitura, e um arquivo copiado para outra máquina ou
tirado de um backup não serve para nada.

O que isso **não** resolve: quem estiver logado como você na mesma máquina
decifra, porque o programa também precisa. Criptografia que o próprio programa
desfaz sozinho não é segredo — o que muda é o alcance do vazamento.

**Para produção, em servidor:** não guarde a senha de uma pessoa. Token de API
revogável, injetado pelo ambiente ou vindo de um cofre de segredos. A DPAPI
resolve o caso "programa de mesa que entra sozinho na conta de quem o usa" —
não sobrevive a um servidor compartilhado.

---

## Os testes

```powershell
python testar_base.py
```

Offline, sem rede e sem navegador. Cobrem a independência (a base carrega sem o
PHDS por perto e sem PySide6/Selenium), a trava do laço no `config.yaml`, as
cinco situações da classificação, os prazos por prioridade, o fuso horário, a
linha de base da vigia, a extração incremental, o formato único das duas
fontes, os limites da API (janela de 90 dias, 3 req/s) e — o mais importante
para um site — **o chamado que fecha sair da lista do coletor**.

O primeiro teste é o mais bobo e o mais importante: **a base importa sozinha?**
Uma cópia que só funciona ao lado do original não é uma cópia isolada.

### Uma ressalva honesta

`base.tomticket_api` foi escrito a partir da documentação oficial e testado
**offline**: formato dos parâmetros, paginação, limite de requisições,
normalização dos campos. Ele **não foi exercitado contra uma conta real** — não
havia token disponível quando foi escrito. Antes de pôr em produção, rode
`api.conferir_token()` e compare uma página com o que você vê na tela do
TomTicket. O resto da base (console e dashboard) foi verificado ao vivo.
#   b a s e - a u t o m a c a o - s r t  
 