"""
A API OFICIAL do TomTicket - o caminho certo para um servidor.

O resto desta base fala com o console (`console.tomticket.com`) usando o cookie
de um login humano. Isso e adequado para um programa de mesa que roda na conta
de quem o usa, e e inadequado para um site: um servidor nao tem navegador para
logar, o cookie expira sem avisar, e endpoints internos mudam sem aviso nem
versao.

A API oficial resolve os tres: token que voce controla, contrato publicado e
versionado, e nenhuma sessao para manter.

    GET https://api.tomticket.com/v2.0/ticket/list
    Authorization: Bearer <token>

Os filtros que interessam a um site de rotas de manutencao:

    department_id     so o departamento da manutencao
    situation         codigos de situacao, separados por virgula
    last_update_ge    so o que mudou desde a ultima leitura  <- quase tempo real
    page              50 por pagina; a resposta traz `pages` e `next_page`

O limite documentado e de 3 requisicoes por segundo. Este cliente respeita esse
limite sozinho (ver `Limitador`) - passar do limite nao e um erro que voce ve,
e um enfileiramento que faz a leitura ficar lenta sem explicacao.

AVISO DE HONESTIDADE: este modulo foi escrito a partir da documentacao oficial
e testado offline (formato dos parametros, paginacao, limite, normalizacao).
Ele NAO foi exercitado contra uma conta real - nao havia token disponivel
quando foi escrito. Antes de por em producao, rode `conferir_token()` e compare
uma pagina com o que voce ve na tela do TomTicket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

from .chamados import ABERTAS, Chamado, de_api
from .registro import AVISO, DETALHE, OK, Registro

BASE = "https://api.tomticket.com/v2.0"
ESPERA = 60

# O teto documentado. Deixamos uma folga: estourar nao devolve erro, enfileira.
POR_SEGUNDO = 3

TENTATIVAS = 4
ESPERAS = (2, 5, 10)

# `last_update_ge` aceita no maximo 90 dias para tras.
MAX_DIAS_DE_JANELA = 90


class ErroApi(RuntimeError):
    pass


class TokenRecusado(ErroApi):
    """401/403: o token esta errado, expirou ou o plano nao inclui a API."""


class Limitador:
    """Segura as chamadas para nao passar de N por segundo.

    Sem isto, uma varredura de 20 paginas dispara 20 requisicoes de uma vez; o
    TomTicket enfileira as excedentes e a leitura fica lenta sem que nada
    apareca como erro - o pior tipo de problema para diagnosticar depois.
    """

    def __init__(self, por_segundo: int = POR_SEGUNDO):
        self.intervalo = 1.0 / max(1, por_segundo)
        self._ultima = 0.0

    def esperar(self) -> None:
        agora = time.monotonic()
        falta = self.intervalo - (agora - self._ultima)
        if falta > 0:
            time.sleep(falta)
        self._ultima = time.monotonic()


@dataclass
class Pagina:
    """Uma pagina de resultados, como a API a devolve."""
    itens: list = field(default_factory=list)
    tamanho: int = 0
    paginas: int = 0
    proxima: int | None = None
    erro: str = ""


def _vale_repetir(status: int, corpo: str) -> str:
    """Diz por que vale insistir - ou "" quando nao vale.

    Repetir so faz sentido quando a falha e do outro lado e passageira. 401 e
    token (repetir da o mesmo), 4xx e pedido errado.
    """
    if status == 0:
        return "falha de rede"
    if status == 429:
        return "passamos do limite de requisicoes"
    if status in (500, 502, 503, 504):
        return f"servidor indisponivel ({status})"
    return ""


class ApiTomTicket:
    """Cliente da API oficial. Uma instancia por token."""

    def __init__(self, token: str, base: str = BASE,
                 registro: Registro | None = None,
                 por_segundo: int = POR_SEGUNDO):
        if not str(token or "").strip():
            raise ErroApi(
                "sem token: gere um em Configuracoes > API no TomTicket e "
                "passe por variavel de ambiente, nunca no codigo.")
        self.base = str(base).rstrip("/")
        self.registro = registro or Registro()
        self.limitador = Limitador(por_segundo)
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {str(token).strip()}",
            "Accept": "application/json",
        })

    # -- chamadas ----------------------------------------------------------

    def _uma_chamada(self, caminho: str, parametros: dict) -> tuple[int, str, dict]:
        self.limitador.esperar()
        try:
            resposta = self.http.get(self.base + caminho, params=parametros,
                                     timeout=ESPERA)
        except requests.RequestException as erro:
            return 0, str(erro), {}
        try:
            corpo = resposta.json()
        except ValueError:
            corpo = {}
        return resposta.status_code, resposta.text, corpo

    def chamar(self, caminho: str, parametros: dict | None = None) -> dict:
        """GET com repeticao em falha passageira. Devolve o JSON."""
        parametros = {k: v for k, v in (parametros or {}).items()
                      if v not in (None, "")}
        for tentativa in range(TENTATIVAS):
            status, texto, corpo = self._uma_chamada(caminho, parametros)

            if status in (401, 403):
                raise TokenRecusado(
                    f"o TomTicket recusou o token ({status}). Confira se ele "
                    f"esta valido e se o plano da conta inclui a API.")

            motivo = _vale_repetir(status, texto)
            if not motivo:
                if not (200 <= status < 300):
                    raise ErroApi(f"GET {caminho} devolveu {status}: {texto[:200]}")
                # A API sinaliza erro no proprio corpo, com HTTP 200.
                if isinstance(corpo, dict) and corpo.get("error"):
                    raise ErroApi(
                        f"{caminho}: {corpo.get('message') or 'erro sem mensagem'}")
                return corpo if isinstance(corpo, dict) else {}

            if tentativa == TENTATIVAS - 1:
                raise ErroApi(
                    f"{caminho}: {motivo}. Desisti depois de {TENTATIVAS} "
                    f"tentativas.")
            espera = ESPERAS[min(tentativa, len(ESPERAS) - 1)]
            self.registro.log(
                f"{caminho}: {motivo}. Tentando de novo em {espera}s "
                f"({tentativa + 1}/{TENTATIVAS}).", AVISO)
            time.sleep(espera)
        return {}

    # -- leitura de chamados ------------------------------------------------

    def pagina_de_chamados(self, pagina: int = 1, **filtros) -> Pagina:
        """Uma pagina de `GET /ticket/list` (50 por pagina)."""
        corpo = self.chamar("/ticket/list", dict(filtros, page=pagina))
        return Pagina(
            itens=list(corpo.get("data") or []),
            tamanho=int(corpo.get("size") or 0),
            paginas=int(corpo.get("pages") or 0),
            proxima=corpo.get("next_page"),
            erro=str(corpo.get("message") or ""),
        )

    def chamados(self, limite_de_paginas: int = 200, **filtros):
        """Todos os chamados que batem com os filtros, pagina a pagina.

        Devolve `Chamado` ja normalizado - quem consome nao precisa conhecer os
        nomes de campo da API. Use `pagina_de_chamados` se quiser o cru.
        """
        pagina = 1
        lidos = 0
        while pagina <= limite_de_paginas:
            resultado = self.pagina_de_chamados(pagina, **filtros)
            if not resultado.itens:
                break
            for bruto in resultado.itens:
                lidos += 1
                yield de_api(bruto)
            self.registro.progresso(lidos, resultado.tamanho or 0,
                                    "lendo chamados")
            if not resultado.proxima or resultado.proxima <= pagina:
                break
            pagina = int(resultado.proxima)

    def abertos_do_departamento(self, department_id: str,
                                desde: datetime | None = None,
                                **extra) -> list:
        """Os chamados ABERTOS de um departamento.

        `situation` e um filtro que INCLUI: passamos a lista das situacoes
        abertas em vez de tentar excluir as fechadas. Se o TomTicket criar uma
        situacao nova, ela nao entra por aqui - e melhor assim: um codigo
        desconhecido nao deve virar parada de rota sem alguem olhar.

        `desde` vira `last_update_ge` - e o que faz a leitura seguinte custar
        quase nada. A API aceita no maximo 90 dias para tras.
        """
        filtros = dict(extra)
        filtros["department_id"] = str(department_id)
        filtros["situation"] = ",".join(str(c) for c in ABERTAS)
        if desde is not None:
            filtros["last_update_ge"] = formatar_data(limitar_janela(desde))
        return list(self.chamados(**filtros))

    def chamado(self, ticket_id: str) -> Chamado | None:
        """Um chamado especifico, com os detalhes."""
        corpo = self.chamar("/ticket/detail", {"id": str(ticket_id)})
        dados = corpo.get("data")
        if isinstance(dados, list):
            dados = dados[0] if dados else None
        return de_api(dados) if dados else None

    def departamentos(self) -> list:
        """[(id, nome)] - para descobrir o id do departamento pelo nome.

        Id fixo no codigo apodrece quando o cadastro e refeito; resolver pelo
        nome uma vez, na subida, e o que evita descobrir isso em producao.
        """
        corpo = self.chamar("/department/list", {})
        saida = []
        for item in (corpo.get("data") or []):
            saida.append((str(item.get("id") or ""),
                          str(item.get("name") or item.get("description") or "")))
        return saida

    def id_do_departamento(self, nome: str) -> str:
        from .chamados import _chave
        alvo = _chave(nome)
        for identificador, rotulo in self.departamentos():
            if _chave(rotulo) == alvo:
                return identificador
        return ""

    def conferir_token(self) -> bool:
        """Uma leitura minima, so para provar que o token funciona.

        Vale rodar na subida do servico: um token invalido descoberto no
        primeiro polling vira um erro no meio da noite; descoberto aqui, vira
        uma linha no log de inicializacao.
        """
        try:
            self.pagina_de_chamados(1)
        except TokenRecusado as erro:
            self.registro.log(str(erro), AVISO)
            return False
        self.registro.log("Token da API do TomTicket aceito.", OK)
        return True


# ---------------------------------------------------------------------------
# Datas do jeito que a API espera
# ---------------------------------------------------------------------------

def formatar_data(momento: datetime) -> str:
    """O formato aceito pelos filtros de data."""
    return momento.strftime("%Y-%m-%d %H:%M:%S")


def limitar_janela(desde: datetime,
                   maximo_de_dias: int = MAX_DIAS_DE_JANELA) -> datetime:
    """Nao pede mais para tras do que a API aceita.

    `last_update_ge` cobre no maximo 90 dias. Pedir mais nao devolve mais - so
    devolve erro, e num servico que ficou dias parado isso seria justamente na
    hora de voltar.
    """
    piso = datetime.now() - timedelta(days=maximo_de_dias)
    return max(desde, piso)
