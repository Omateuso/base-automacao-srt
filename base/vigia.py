"""
As regras da vigia, sem rede e sem navegador.

Duas decisoes moram aqui, e so aqui:

  * o que conta como CHAMADO NOVO - protocolo que a vigia ainda nao tinha
    visto, nesta maquina, em nenhuma checagem anterior;
  * qual prazo responder - 24h para Alta e Urgente, 48h para Normal e Baixa,
    que e a regra que voce ja aplica na mao.

O texto da resposta NAO e escrito aqui: ele ja existe em
`nucleo/cobranca/config.yaml`, na secao `gatilhos`, como `texto_original`. E o
mesmo texto que, semanas depois, faz o robo de cobranca reconhecer o chamado
como cobravel. Se a vigia tivesse um texto proprio, os dois lados sairiam do
ar um do outro no primeiro dia em que voce editasse um deles.

Prioridade que nao esteja na tabela nao vira 24h nem 48h por padrao: a vigia
marca "prioridade desconhecida" e pergunta. Errar o prazo escrito para o
cliente e pior do que perguntar.

E ha uma terceira, que nasceu de um chamado perdido: a linha de base olha a
HORA DE CRIACAO, nao so o protocolo. Na primeira passada, adotar a lista
inteira como "ja existente" tambem engolia o chamado que tinha entrado tres
minutos antes de voce ligar a vigia - o unico que ainda precisava de acao.
Agora o que nasceu na ultima hora fica de fora da linha de base e e avisado.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Prioridade (como o TomTicket devolve) -> id do gatilho em config.yaml
PRAZO_POR_PRIORIDADE = {
    "urgente": "24h",
    "alta": "24h",
    "normal": "48h",
    "baixa": "48h",
}

# Quem deve ficar vinculado ao chamado depois que voce trata.
ATENDENTE_ALVO = "CSM - SRT"

# Quanto tempo para tras a linha de base considera "acabou de chegar". Uma hora
# cobre o caso real: o chamado entra, voce percebe minutos depois e liga a
# vigia - e ele precisa aparecer, nao ser adotado como paisagem.
MINUTOS_DE_GRACA = 60

_RE_DATA = re.compile(
    r"(\d{2})[/-](\d{2})[/-](\d{4})(?:[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def quando(texto: str) -> datetime | None:
    """A data de criacao como a busca a devolve: ISO ou dd/mm/aaaa hh:mm:ss."""
    bruto = str(texto or "").strip()
    if not bruto:
        return None
    try:
        lida = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        # "Z" e UTC: sem converter, a data fica horas no futuro e toda conta
        # de tempo decorrido feita em cima dela encurta na mesma medida.
        if lida.tzinfo is not None:
            lida = lida.astimezone()
        return lida.replace(tzinfo=None)
    achado = _RE_DATA.search(bruto)
    if not achado:
        return None
    dia, mes, ano, hora, minuto, segundo = achado.groups()
    try:
        return datetime(int(ano), int(mes), int(dia),
                        int(hora or 0), int(minuto or 0), int(segundo or 0))
    except ValueError:
        return None


def _chave(texto: str) -> str:
    """Minusculo, sem acento, sem espaco sobrando - para comparar rotulos."""
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def _sem_pontuacao(texto: str) -> str:
    """Como `_chave`, mas tambem sem pontuacao - a normalizacao que a cobranca
    usa para casar as assinaturas dos gatilhos."""
    return " ".join(re.sub(r"[^\w\s]", " ", _chave(texto)).split())


def prazo_de(prioridade: str) -> str | None:
    """"Alta" -> "24h"; "Normal" -> "48h"; qualquer outra coisa -> None."""
    return PRAZO_POR_PRIORIDADE.get(_chave(prioridade))


def mesmo_atendente(atendente: str, alvo: str = ATENDENTE_ALVO) -> bool:
    """"CSM - SRT", "CSM-SRT" e "csm  -  srt" sao a mesma pessoa."""
    limpar = lambda t: _chave(t).replace(" ", "")     # noqa: E731
    return bool(atendente) and limpar(atendente) == limpar(alvo)


@dataclass
class Chamado:
    """Um chamado como a busca avancada o devolve."""
    protocolo: str
    ticket_id: str = ""
    assunto: str = ""
    cliente: str = ""
    prioridade: str = ""
    atendente: str = ""
    departamento: str = ""
    categoria: str = ""
    criado_em: str = ""
    visto_em: str = ""            # quando a vigia viu este chamado pela 1a vez
    respondido: bool = False      # alguem do helpdesk ja escreveu no chamado
    prazo_respondido: str = ""    # "24h"/"48h" se o texto de prazo ja foi dado
    conferido_em: str = ""        # quando a vigia leu a conversa deste chamado

    @property
    def criado(self) -> datetime | None:
        return quando(self.criado_em)

    @property
    def chegou(self) -> str:
        """Quando o chamado entrou - a hora dele, nao a hora em que a vigia viu."""
        nascido = self.criado
        return nascido.strftime("%d/%m %H:%M") if nascido else (self.visto_em or "—")

    @property
    def prazo(self) -> str | None:
        return prazo_de(self.prioridade)

    @property
    def precisa_vincular(self) -> bool:
        return not mesmo_atendente(self.atendente)

    @property
    def falta(self) -> str:
        """O que ainda falta fazer neste chamado, em uma linha."""
        partes = []
        if self.precisa_vincular:
            partes.append("vincular ao CSM - SRT")

        if self.prazo_respondido:
            pass                      # o prazo ja foi dado: nao falta responder
        elif self.prazo:
            partes.append(f"responder o prazo de {self.prazo}")
        else:
            partes.append("prioridade desconhecida: escolha o prazo")

        if not partes:
            return "nada a fazer"
        return " · ".join(partes)

    @property
    def situacao_da_resposta(self) -> str:
        """Uma coluna so para a pergunta "ja foi respondido?"."""
        if self.prazo_respondido:
            return f"prazo {self.prazo_respondido} respondido"
        if self.respondido:
            return "respondido, sem prazo"
        if not self.conferido_em:
            return "—"
        return "sem resposta"

    @property
    def pronto(self) -> bool:
        """Vinculado e com o prazo respondido: nao sobra nada para voce."""
        return bool(self.prazo_respondido) and not self.precisa_vincular


# Nomes dos campos como o TomTicket devolve na busca avancada. Sao os mesmos
# que `tomticket_conversas.montar_registro` ja lia - se um dia mudarem, muda
# aqui e nos dois lugares o efeito e o mesmo.
def converter(item: dict) -> Chamado:
    return Chamado(
        protocolo=str(item.get("protocolo") or "").strip(),
        ticket_id=str(item.get("id") or "").strip(),
        assunto=str(item.get("titulo") or "").strip(),
        cliente=str(item.get("nomecliente") or item.get("idcliente") or "").strip(),
        prioridade=str(item.get("prioridade") or "").strip(),
        atendente=str(item.get("atendente") or "").strip(),
        departamento=str(item.get("codproduto") or "").strip(),
        categoria=str(item.get("codtipoassunto") or "").strip(),
        criado_em=str(item.get("datahora") or "").strip(),
    )


@dataclass
class Checagem:
    """O resultado de uma passada da vigia."""
    quando: datetime = field(default_factory=datetime.now)
    novos: list[Chamado] = field(default_factory=list)
    vistos_agora: int = 0          # quantos chamados a busca devolveu
    paginas: int = 0
    completa: bool = False         # varreu a lista inteira, nao so as 1as paginas
    primeira: bool = False         # so montou a linha de base, nao notifica
    busca: str = ""                # nome da busca salva que foi lida
    caminho: str = ""              # "HTTP" (sem navegador) ou "janela"

    @property
    def houve_novidade(self) -> bool:
        # Vale tambem na primeira passada: o que nasceu na ultima hora fica de
        # fora da linha de base justamente para poder ser avisado.
        return bool(self.novos)


def separar_novos(itens: list[dict], vistos: set[str]) -> list[Chamado]:
    """
    Os chamados cujo protocolo ainda nao esta em `vistos`, sem repetir.

    A busca pagina; a mesma pagina pode voltar duas vezes quando alguem mexe na
    lista no meio da varredura, entao o de-duplicar tem que ser por protocolo e
    nao por posicao.
    """
    novos: list[Chamado] = []
    ja = set(vistos)
    for item in itens:
        chamado = converter(item)
        if not chamado.protocolo or chamado.protocolo in ja:
            continue
        ja.add(chamado.protocolo)
        novos.append(chamado)
    return novos


def recentes(itens: list[dict], minutos: int = MINUTOS_DE_GRACA,
             agora: datetime | None = None) -> set[str]:
    """Protocolos criados nos ultimos `minutos` - os que a linha de base poupa.

    Chamado sem data legivel conta como recente: na duvida, avisar e o erro
    barato; engolir em silencio e o caro.
    """
    agora = agora or datetime.now()
    corte = agora - timedelta(minutes=minutos)
    novos: set[str] = set()
    for item in itens:
        protocolo = str(item.get("protocolo") or "").strip()
        if not protocolo:
            continue
        nascido = quando(item.get("datahora"))
        if nascido is None or nascido >= corte:
            novos.add(protocolo)
    return novos


def analisar_respostas(mensagens: list[dict],
                      assinaturas: dict[str, str]) -> tuple[bool, str]:
    """
    Le a conversa de um chamado e responde duas coisas.

    (ja respondeu alguem do helpdesk?, qual prazo ja foi dado)

    A comparacao usa as MESMAS assinaturas dos gatilhos da cobranca e a mesma
    normalizacao dela: e o unico jeito de "ja respondido" aqui significar
    exatamente o que a cobranca vai reconhecer depois. Uma frase parecida
    escrita a mao nao conta - e nem deve: o que fecha o ciclo e o texto padrao.
    """
    respondido = False
    prazo = ""
    for mensagem in mensagens:
        if not mensagem.get("atendente"):
            continue
        respondido = True
        texto = _sem_pontuacao(mensagem.get("texto", ""))
        if not texto:
            continue
        for identificador, assinatura in assinaturas.items():
            alvo = _sem_pontuacao(assinatura)
            if alvo and alvo in texto:
                prazo = identificador     # o ultimo prevalece
    return respondido, prazo


def linha_de_base(itens: list[dict], minutos: int = MINUTOS_DE_GRACA,
                  agora: datetime | None = None) -> set[str]:
    """O que a PRIMEIRA passada adota como paisagem: tudo menos o que acabou
    de chegar. O que fica de fora daqui e avisado na mesma passada."""
    return protocolos(itens) - recentes(itens, minutos, agora)


def protocolos(itens: list[dict]) -> set[str]:
    return {p for p in (str(i.get("protocolo") or "").strip() for i in itens) if p}
