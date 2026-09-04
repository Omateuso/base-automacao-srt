"""
A sessao do TomTicket dentro do programa - sem janela.

O que o navegador estava fazendo aqui: nada de automacao. Toda a leitura ja e
chamada de API (`advancedsearchcollection`, `ticket/advancedfilter`,
`ticket/historico`); o Chrome so guardava o COOKIE da sua sessao, e as chamadas
eram feitas de dentro da pagina para poderem usa-lo.

Entao o navegador vira o que ele e: um chaveiro. Loga uma vez - sem janela,
com o login do .env -, entrega os cookies, e o programa passa a falar direto
com o TomTicket por HTTP. Enquanto o cookie valer, nenhuma janela abre.

    entrar()  ->  cookies em disco  ->  requests.Session

Se qualquer chamada falhar ou a sessao cair, quem chama volta ao caminho de
antes (as mesmas chamadas, mas de dentro de uma pagina aberta). A vigia nao
pode parar de vigiar porque um endpoint mudou de humor.

Sobre guardar cookie em disco: e um token de sessao. O arquivo e fechado para
outros usuarios da maquina (ver `cofre.restringir_ao_usuario`) e o cookie
expira sozinho. Se algum dia o acesso vazar, saia de todas as sessoes no
TomTicket.

    sessao = SessaoTomTicket("sessao.json")
    if not sessao.logado():
        sessao.entrar_pelo_navegador("perfil-chrome", credenciais)
    for pagina in range(400):
        lote = sessao.pagina_da_busca(sessao.busca_salva()["condicao"], pagina)
        if not lote:
            break
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import requests

from . import cofre
from .registro import AVISO, DETALHE, OK, Registro

BASE = "https://console.tomticket.com"
URL_BUSCA = f"{BASE}/dashboard/tickets/advanced-search-dynamic"
URL_LOGADO = f"{BASE}/engine/index.php/security/logged"
URL_LOGIN = f"{BASE}/login"

JS_LOGADO = """
async () => {
  try {
    const r = await fetch('/engine/index.php/security/logged',
                          { credentials: 'include' });
    const j = await r.json();
    return j && j.status === true;
  } catch (e) { return false; }
}
"""
URL_FILTRO = f"{BASE}/engine/index.php/advancedsearchcollection/get/1"
URL_AVANCADA = f"{BASE}/engine/index.php/ticket/advancedfilter"
URL_HISTORICO = f"{BASE}/engine/index.php/ticket/historico/{{id}}/0/0/1"
# O mesmo endpoint que o botao "Enviar Resposta" da tela usa (o codigo do
# console chama a funcao de `enviaResposta`).
URL_RESPONDER = f"{BASE}/engine/index.php/ticket/salvarhistorico"

NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
ESPERA = 30


def _para_html(texto: str) -> str:
    """Texto simples -> o HTML simples que o editor do TomTicket produz."""
    import html as _html

    escapado = _html.escape(str(texto or ""), quote=False)
    paragrafos = [p.strip() for p in escapado.split("\n\n") if p.strip()]
    return "".join(
        "<p>" + p.replace("\n", "<br>") + "</p>" for p in paragrafos)


def _sem_marcacao(bruto: str) -> str:
    """Tira tags e normaliza espacos - para comparar texto de conversa."""
    import html as _html
    import re as _re

    texto = _re.sub(r"<[^>]+>", " ", str(bruto or ""))
    texto = _html.unescape(texto).replace(" ", " ")
    return " ".join(texto.split())


def _preencher_login(pagina, credenciais: dict) -> None:
    """Preenche a tela de login do TomTicket. Usado so pelo caminho opcional."""
    campos = (
        (("input[name='conta']", "input[name='account']", "input#conta"),
         credenciais.get("TOMTICKET_CONTA", "")),
        (("input[type='email']", "input[name='email']"),
         credenciais.get("TOMTICKET_EMAIL", "")),
        (("input[type='password']", "input[name='senha']"),
         credenciais.get("TOMTICKET_SENHA", "")),
    )
    for seletores, valor in campos:
        if not valor:
            continue
        for seletor in seletores:
            alvo = pagina.locator(seletor).first
            try:
                if alvo.count() and alvo.is_visible():
                    alvo.click()
                    alvo.fill(valor)
                    break
            except Exception:
                continue
    pagina.keyboard.press("Enter")
    for _ in range(30):
        pagina.wait_for_timeout(2000)
        try:
            if pagina.evaluate(JS_LOGADO):
                return
        except Exception:
            pass


def _quando_da_linha(linha: dict) -> datetime | None:
    """A hora de uma linha do historico do TomTicket.

    `unixtime` e o campo bom - segundos, ja no fuso certo quando convertido.
    Mas ele vem 0 em mensagens antigas; ai sobra `datahora` ("01/09/2026
    11:23"), com precisao de minuto, que para uma conta de 24h basta.
    """
    try:
        segundos = int(linha.get("unixtime") or 0)
    except (TypeError, ValueError):
        segundos = 0
    if segundos > 0:
        return datetime.fromtimestamp(segundos)

    from .vigia import quando
    return quando(str(linha.get("datahora") or ""))


def procurar_marcas(texto: str, marcas: list[str]) -> str | None:
    """A primeira marca que aparece no texto - ou None.

    As duas pontas passam pela MESMA normalizacao: minusculo, sem acento, sem
    pontuacao. E por um motivo concreto: a assinatura do gatilho no
    config.yaml e escrita sem acento ("prazo de ate 24 horas") e a conversa
    vem com eles ("ate"). Comparando cru, a trava anti-duplicidade nunca
    casaria - e o mesmo prazo seria respondido duas vezes no chamado do
    cliente.
    """
    from .vigia import _sem_pontuacao

    limpo = _sem_pontuacao(_sem_marcacao(texto))
    for marca in marcas:
        alvo = _sem_pontuacao(_sem_marcacao(str(marca)))
        if alvo and alvo in limpo:
            return marca
    return None


class ErroSessao(RuntimeError):
    pass


class SemLogin(ErroSessao):
    """A sessao caiu ou nunca existiu - alguem precisa entrar de novo."""


class SessaoTomTicket:
    """Fala com o TomTicket por HTTP, usando o cookie de uma sessao real."""

    def __init__(self, arquivo: Path | str, registro: Registro | None = None):
        self.arquivo = Path(arquivo)
        self.ponte = registro or Registro()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": NAVEGADOR,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": URL_BUSCA,
            "Origin": BASE,
        })
        self.quando: str = ""
        self.carregar()

    # -- cookies ----------------------------------------------------------

    @property
    def tem_cookies(self) -> bool:
        return len(self.http.cookies) > 0

    def carregar(self) -> None:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.quando = str(dados.get("quando", ""))
        for cookie in dados.get("cookies", []):
            nome, valor = cookie.get("name"), cookie.get("value")
            if not nome or valor is None:
                continue
            self.http.cookies.set(
                nome, valor,
                domain=cookie.get("domain") or "console.tomticket.com",
                path=cookie.get("path") or "/")

    def gravar(self) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        dados = {
            "quando": datetime.now().isoformat(timespec="seconds"),
            "cookies": [
                {"name": c.name, "value": c.value,
                 "domain": c.domain, "path": c.path}
                for c in self.http.cookies
            ],
        }
        try:
            self.arquivo.write_text(
                json.dumps(dados, indent=2, ensure_ascii=False),
                encoding="utf-8")
            cofre.restringir_ao_usuario(self.arquivo)
        except OSError:
            pass

    def adotar(self, cookies: list[dict]) -> None:
        """Recebe os cookies de um navegador ja logado e passa a usa-los."""
        self.http.cookies.clear()
        for cookie in cookies or []:
            nome, valor = cookie.get("name"), cookie.get("value")
            if not nome or valor is None:
                continue
            self.http.cookies.set(
                nome, valor,
                domain=cookie.get("domain") or "console.tomticket.com",
                path=cookie.get("path") or "/")
        self.quando = datetime.now().isoformat(timespec="seconds")
        self.gravar()

    def esquecer(self) -> None:
        self.http.cookies.clear()
        try:
            self.arquivo.unlink()
        except OSError:
            pass

    # -- login ------------------------------------------------------------

    def logado(self) -> bool:
        if not self.tem_cookies:
            return False
        try:
            resposta = self.http.get(URL_LOGADO, timeout=ESPERA)
            return bool(resposta.ok and (resposta.json() or {}).get("status"))
        except (requests.RequestException, ValueError):
            return False

    def entrar_pelo_navegador(self, perfil: Path, credenciais: dict,
                              visivel: bool = False) -> None:
        """Loga uma vez e guarda o cookie. Exige o playwright instalado.

        O TomTicket nao expoe um login por HTTP simples (o endpoint interno
        pede um token que so existe dentro do fluxo do proprio app), entao
        esta e a unica parte da base que pode precisar de navegador - e so
        quando nao ha sessao guardada. Se voce ja tem o cookie de outro lugar,
        use `adotar()` e o playwright nem precisa existir.

        `credenciais` e {"TOMTICKET_CONTA": ..., "TOMTICKET_EMAIL": ...,
        "TOMTICKET_SENHA": ...}.
        """
        from playwright.sync_api import sync_playwright  # opcional

        perfil = Path(perfil)
        perfil.mkdir(parents=True, exist_ok=True)
        sem_janela = bool(credenciais) and not visivel

        with sync_playwright() as play:
            ctx = play.chromium.launch_persistent_context(
                user_data_dir=str(perfil), headless=sem_janela, viewport=None,
                args=["--test-type"])
            try:
                pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
                pagina.goto(URL_BUSCA, wait_until="domcontentloaded",
                            timeout=60000)
                if not pagina.evaluate(JS_LOGADO):
                    if credenciais:
                        _preencher_login(pagina, credenciais)
                    if not pagina.evaluate(JS_LOGADO):
                        raise SemLogin("nao consegui confirmar o login.")
                self.adotar(ctx.cookies())
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass

    # -- chamadas ---------------------------------------------------------

    def _json(self, resposta) -> dict | list:
        if resposta.status_code in (401, 403):
            raise SemLogin(f"o TomTicket respondeu {resposta.status_code}.")
        if not resposta.ok:
            raise ErroSessao(f"HTTP {resposta.status_code}")
        try:
            return resposta.json()
        except ValueError:
            # Sessao caida costuma devolver a pagina de login em HTML.
            raise SemLogin("resposta nao veio em JSON (sessao caida?).")

    def busca_salva(self) -> dict:
        """A condicao e o nome da busca avancada salva."""
        dados = self._json(self.http.get(URL_FILTRO, timeout=ESPERA)) or {}
        linhas = (dados or {}).get("rows") or []
        if not linhas:
            return {}
        primeira = linhas[0]
        return {
            "condicao": primeira.get("search_condition"),
            "nome": (primeira.get("name") or primeira.get("title")
                     or primeira.get("description") or ""),
            "quantas": len(linhas),
        }

    def pagina_da_busca(self, condicao, numero: int) -> list[dict]:
        resposta = self.http.post(
            URL_AVANCADA, timeout=ESPERA,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"json": json.dumps(condicao), "page": numero}))
        dados = self._json(resposta) or {}
        if isinstance(dados, dict) and dados.get("error"):
            raise ErroSessao(dados.get("message") or "erro na busca")
        return (dados or {}).get("lista") or []

    def historico(self, ticket_id: str) -> dict:
        return self._json(
            self.http.get(URL_HISTORICO.format(id=ticket_id), timeout=ESPERA))

    # -- escrita ----------------------------------------------------------

    def enviar_resposta(self, ticket_id: str, texto: str) -> str:
        """Escreve a resposta na conversa do chamado - o que o botao Enviar faz.

        O caminho e o mesmo da tela do TomTicket, e o codigo dela nomeia assim:

            enviaResposta(k) -> POST ticket/salvarhistorico

        onde `k` e uma LINHA DO HISTORICO com o campo `resposta` preenchido.
        Por isso a conversa e lida antes: a resposta vai pendurada na mensagem
        mais recente, exatamente como quando voce clica em Responder nela.

        Devolve a mensagem que o TomTicket deu. Levanta ErroSessao se ele
        recusar - e quem chama confere depois, relendo a conversa.
        """
        conversa = self.historico(ticket_id) or {}
        historicos = conversa.get("historicos") or []
        if not historicos:
            raise ErroSessao(
                "a conversa do chamado veio vazia; nao sei onde pendurar a "
                "resposta.")

        linha = dict(historicos[0])            # a mais recente
        linha["resposta"] = _para_html(texto)
        linha["aplicarHora"] = False
        linha["horatrabalho"] = ""

        resposta = self.http.post(
            URL_RESPONDER, timeout=ESPERA,
            headers={"Content-Type": "application/json"},
            data=json.dumps(linha, ensure_ascii=False).encode("utf-8"))
        dados = self._json(resposta)
        if isinstance(dados, dict) and dados.get("error"):
            raise ErroSessao(dados.get("message") or "o TomTicket recusou.")
        return str((dados or {}).get("message") or "resposta enviada")

    def esta_finalizado(self, ticket_id: str) -> bool:
        """O chamado ja foi encerrado no TomTicket?

        Serve para conferir o que o programa mandou fazer: pedir para fechar e
        assumir que fechou e o mesmo erro de mandar e-mail e assumir que
        chegou.
        """
        chamado = self.historico(ticket_id) or {}
        encerrado = str(chamado.get("dataencerramento") or "").strip()
        etiqueta = str(chamado.get("labelsituacao") or "").strip().lower()
        return bool(encerrado) or (etiqueta not in ("", "aberto"))

    def conversa(self, ticket_id: str) -> tuple[str, datetime | None]:
        """O texto da conversa inteira e QUANDO foi a mensagem mais recente.

        Uma leitura so, porque quem confere uma cobranca precisa das duas
        coisas na mesma hora: se o texto ja esta la, e ha quanto tempo foi a
        ultima mensagem. Sao as duas perguntas que o dashboard responde
        atrasado.

        A hora sai do MAIOR carimbo entre as linhas, nao da primeira: a ordem
        que o TomTicket devolve hoje e a mais recente primeiro, mas nada aqui
        depende disso continuar verdade.
        """
        conversa = self.historico(ticket_id) or {}
        linhas = [h for h in (conversa.get("historicos") or [])
                  if not h.get("tipo") or h.get("tipo") == "H"]
        texto = " ".join(str(h.get("mensagem") or "") for h in linhas)
        datas = [d for d in (_quando_da_linha(h) for h in linhas) if d]
        return texto, (max(datas) if datas else None)

    def conversa_contem(self, ticket_id: str, marcas: list[str]) -> str | None:
        """A conversa AO VIVO ja tem alguma destas marcas? Devolve a que achou.

        E a trava anti-duplicidade da cobranca, agora sem navegador: le a
        conversa no instante anterior a escrita, que e o unico momento em que
        a informacao vale.
        """
        texto, _ = self.conversa(ticket_id)
        return procurar_marcas(texto, marcas)
