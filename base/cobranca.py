"""Classificacao dos chamados de cobranca.

Copiado do PHDS sem alteracao de regra: recebe dados ja extraidos e decide
o que fazer com cada chamado. Nao conhece HTTP, navegador nem interface -
e o que o torna testavel e reaproveitavel isoladamente.

A regra em uma linha: so e cobravel o chamado cuja ULTIMA mensagem seja um
dos textos padrao (os gatilhos do config.yaml). Mensagem que ja diz que o
prazo foi excedido nunca vira cobravel - ela ja e a cobranca.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config, Gatilho
from .registro import AVISO, OK, Registro

# --- Situacoes possiveis de um chamado ------------------------------------
PRONTO = "PRONTO"                    # bate com gatilho -> vai ser respondido
AGUARDANDO_PRAZO = "AGUARDANDO_PRAZO"  # bate, mas o prazo ainda nao estourou
JA_ESCALADO = "JA_ESCALADO"          # ultima resposta ja e uma cobranca
FORA_DE_ESCOPO = "FORA_DE_ESCOPO"    # ultima resposta nao bate com nenhum gatilho
INDETERMINADO = "INDETERMINADO"      # nao foi possivel isolar a ultima resposta

# Ordem usada nos relatorios (mais acionavel primeiro).
ORDEM_SITUACOES = [PRONTO, FORA_DE_ESCOPO, AGUARDANDO_PRAZO, INDETERMINADO, JA_ESCALADO]

_RE_ESPACOS = re.compile(r"\s+")
_RE_DATA = re.compile(
    r"(\d{2})[/-](\d{2})[/-](\d{4})(?:[\sT,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def normalizar(texto: str) -> str:
    """Minusculo, sem acento, sem pontuacao redundante, espacos colapsados.

    E o que permite casar o texto do gatilho mesmo que o TomTicket renderize
    com quebras de linha, &nbsp; ou espacos diferentes do original.
    """
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.replace("\xa0", " ").lower()
    texto = re.sub(r"[^\w\s]", " ", texto)
    return _RE_ESPACOS.sub(" ", texto).strip()


def extrair_data(texto: str) -> datetime | None:
    """Converte uma data da API (ISO) ou um texto com dd/mm/aaaa em datetime."""
    if not texto:
        return None
    bruto = str(texto).strip()

    # formato da API: 2026-08-06T14:32:00Z / 2026-08-06 11:32:00-03
    try:
        lida = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        # O dashboard mistura os dois no mesmo chamado: as mensagens vem com
        # "-03" e a data de criacao vem com "Z". Largar o fuso sem converter
        # trata 14:21Z como 14:21 daqui - tres horas no FUTURO - e isso
        # encurta em 3h todo tempo decorrido calculado em cima. Como o prazo
        # E um tempo decorrido, o erro sai direto na conta de 24h.
        if lida.tzinfo is not None:
            lida = lida.astimezone()
        return lida.replace(tzinfo=None)

    m = _RE_DATA.search(bruto)
    if not m:
        return None
    dia, mes, ano, hora, minuto, seg = m.groups()
    try:
        return datetime(
            int(ano), int(mes), int(dia),
            int(hora or 0), int(minuto or 0), int(seg or 0),
        )
    except ValueError:
        return None


@dataclass
class Resposta:
    """Uma mensagem do chamado (item de `mensagens[]` na API)."""
    texto: str
    data: datetime | None = None
    autor: str = ""
    tipo_autor: str = ""  # "C" = cliente, "A" = atendente

    @property
    def normalizado(self) -> str:
        return normalizar(self.texto)

    @property
    def autor_legivel(self) -> str:
        papel = "Cliente" if self.tipo_autor.upper() == "C" else "Atendente"
        return f"{self.autor} ({papel})" if self.autor else papel


@dataclass
class Chamado:
    protocolo: str
    ticket_id: str = ""
    url: str = ""
    assunto: str = ""
    solicitante: str = ""
    projeto: str = ""
    departamento: str = ""
    categoria: str = ""
    atendente: str = ""
    prioridade: str = ""
    situacao_tomticket: str = ""
    data_criacao: datetime | None = None
    respostas: list[Resposta] = field(default_factory=list)

    # preenchido pela classificacao
    situacao: str = INDETERMINADO
    gatilho: Gatilho | None = None
    motivo: str = ""
    ultima_resposta: Resposta | None = None
    prazo_excedido: bool | None = None
    horas_decorridas: float | None = None

    @property
    def trecho_ultima_resposta(self) -> str:
        if not self.ultima_resposta:
            return ""
        t = _RE_ESPACOS.sub(" ", self.ultima_resposta.texto).strip()
        return t[:300] + ("..." if len(t) > 300 else "")


def _contem(alvo_normalizado: str, assinatura: str) -> bool:
    return normalizar(assinatura) in alvo_normalizado


def assinaturas_de_bloqueio(cfg: Config) -> list[str]:
    """Marcas que PROIBEM uma nova cobranca no chamado.

    Sao duas camadas:

    1. As frases de `assinaturas_ja_escalado` do config.yaml.
    2. Um trecho de CADA resposta configurada, derivado automaticamente.

    A camada 2 e a garantia forte: o robo sempre reconhece o proprio texto que
    envia, mesmo que voce edite as respostas no config.yaml e esqueca de
    atualizar a lista da camada 1. Sem ela, mudar a redacao da cobranca faria o
    robo deixar de enxergar as cobrancas que ele mesmo mandou - e cobrar duas
    vezes o mesmo chamado.
    """
    marcas = [m for m in cfg.assinaturas_ja_escalado if normalizar(m)]
    for texto in cfg.respostas.values():
        nucleo = normalizar(texto)[:80]
        if len(nucleo) >= 30:
            marcas.append(nucleo)
    return marcas


def classificar(chamado: Chamado, cfg: Config, agora: datetime | None = None) -> Chamado:
    """Decide a situacao do chamado a partir da sua ultima resposta."""
    agora = agora or datetime.now()

    if not chamado.respostas:
        chamado.situacao = INDETERMINADO
        chamado.motivo = "Nenhuma resposta foi identificada na tela do chamado."
        return chamado

    ultima = chamado.respostas[-1]
    chamado.ultima_resposta = ultima
    texto = ultima.normalizado

    if not texto:
        chamado.situacao = INDETERMINADO
        chamado.motivo = "A ultima resposta foi localizada mas esta vazia."
        return chamado

    # 1) A ultima mensagem ja diz que o prazo foi excedido? Entao NAO cobrar.
    #    Esta checagem vem antes de tudo e sai na hora: nenhuma mensagem de
    #    prazo excedido pode, em hipotese alguma, virar PRONTO.
    for assinatura in assinaturas_de_bloqueio(cfg):
        if _contem(texto, assinatura):
            chamado.situacao = JA_ESCALADO
            chamado.motivo = (
                "A ultima mensagem ja avisa que o prazo foi excedido "
                f"(trecho reconhecido: \"{assinatura[:60]}\")."
            )
            return chamado

    # 2) Bate com o texto automatico de 24h ou 48h?
    gatilho = next((g for g in cfg.gatilhos if _contem(texto, g.assinatura)), None)
    if gatilho is None:
        chamado.situacao = FORA_DE_ESCOPO
        chamado.motivo = "A ultima resposta nao corresponde a nenhum texto padrao."
        return chamado

    chamado.gatilho = gatilho

    # 3) O prazo do gatilho ja estourou? (informativo ou bloqueante)
    if ultima.data:
        decorridas = (agora - ultima.data).total_seconds() / 3600
        chamado.horas_decorridas = round(decorridas, 1)
        chamado.prazo_excedido = decorridas >= gatilho.horas_prazo
    else:
        chamado.prazo_excedido = None

    if cfg.verificar_prazo:
        if chamado.prazo_excedido is None:
            chamado.situacao = AGUARDANDO_PRAZO
            chamado.motivo = (
                "Bate com o gatilho "
                f"{gatilho.id}, mas nao foi possivel ler a data da resposta "
                "para confirmar o estouro do prazo."
            )
            return chamado
        if not chamado.prazo_excedido:
            faltam = gatilho.horas_prazo - (chamado.horas_decorridas or 0)
            chamado.situacao = AGUARDANDO_PRAZO
            chamado.motivo = (
                f"Bate com o gatilho {gatilho.id}, mas ainda faltam "
                f"{faltam:.1f}h para estourar o prazo."
            )
            return chamado

    chamado.situacao = PRONTO
    chamado.motivo = f"Ultima resposta corresponde ao texto padrao de {gatilho.id}."
    return chamado


def ordenar_respostas(respostas: list[Resposta]) -> list[Resposta]:
    """Ordem cronologica crescente (a ultima mensagem fica no fim).

    Critico: a classificacao olha `respostas[-1]`. Se a ordem vier invertida,
    o robo leria a mensagem ERRADA como sendo a ultima - e poderia cobrar um
    chamado que ja foi cobrado.
    """
    if not respostas:
        return respostas
    if all(r.data for r in respostas):
        return sorted(respostas, key=lambda r: r.data)

    # datas parciais: se as que existem estao em ordem decrescente, inverte
    datadas = [r for r in respostas if r.data]
    if len(datadas) >= 2 and datadas[0].data > datadas[-1].data:
        return list(reversed(respostas))
    return respostas


def _texto(valor) -> str:
    return "" if valor is None else str(valor).strip()


def montar_chamado(alerta: dict, cfg: Config | None = None) -> Chamado:
    """Converte um item de /tickets/sla-alertas em Chamado."""
    ticket_id = _texto(alerta.get("ticket_id") or alerta.get("id"))
    base = cfg.url_base.rstrip("/") if cfg else ""
    return Chamado(
        protocolo=_texto(alerta.get("protocolo")) or ticket_id,
        ticket_id=ticket_id,
        url=f"{base}/ticket/{ticket_id}" if base and ticket_id else "",
        assunto=_texto(alerta.get("assunto")),
        solicitante=_texto(alerta.get("cliente")),
        projeto=_texto(alerta.get("projeto")),
        departamento=_texto(alerta.get("departamento")),
        categoria=_texto(alerta.get("categoria")),
        atendente=_texto(alerta.get("atendente")),
        prioridade=_texto(alerta.get("prioridade")),
        situacao_tomticket=_texto(alerta.get("ultima_situacao")),
        data_criacao=extrair_data(_texto(alerta.get("data_criacao"))),
    )


def aplicar_detalhe(chamado: Chamado, detalhe: dict) -> Chamado:
    """Preenche o chamado com o retorno de /tickets/{id} (campo `mensagens`)."""
    for campo_api, atributo in (
        ("assunto", "assunto"),
        ("cliente", "solicitante"),
        ("projeto", "projeto"),
        ("departamento", "departamento"),
        ("categoria", "categoria"),
        ("atendente", "atendente"),
        ("prioridade", "prioridade"),
        ("ultima_situacao", "situacao_tomticket"),
    ):
        valor = _texto(detalhe.get(campo_api))
        if valor:
            setattr(chamado, atributo, valor)

    if not chamado.protocolo or chamado.protocolo == chamado.ticket_id:
        chamado.protocolo = _texto(detalhe.get("protocolo")) or chamado.protocolo

    mensagens = [
        Resposta(
            texto=_texto(m.get("message")),
            data=extrair_data(_texto(m.get("date"))),
            autor=_texto(m.get("sender")),
            tipo_autor=_texto(m.get("sender_type")),
        )
        for m in (detalhe.get("mensagens") or [])
        if _texto(m.get("message"))
    ]
    chamado.respostas = ordenar_respostas(mensagens)
    return chamado


def prazo_legivel(chamado: Chamado) -> str:
    """O prazo em texto - com uma casa decimal, de proposito.

    Arredondando, 23,9h virava "24h" e a linha lia "nao (24h)": parece que o
    prazo de 24h passou e o robo discordou. Nao passou, faltavam 4 minutos - e
    a diferenca entre as duas leituras e a diferenca entre confiar e nao
    confiar no numero.
    """
    if chamado.prazo_excedido is None:
        return "desconhecido"
    horas = chamado.horas_decorridas or 0
    if not chamado.gatilho:
        return f"{'sim' if chamado.prazo_excedido else 'nao'} ({horas:.1f}h)"
    alvo = chamado.gatilho.horas_prazo
    if chamado.prazo_excedido:
        return f"sim ({horas:.1f}h de {alvo}h)"
    return f"nao (faltam {alvo - horas:.1f}h de {alvo}h)"


def resumir(chamados: list[Chamado]) -> dict[str, int]:
    resumo = {s: 0 for s in ORDEM_SITUACOES}
    for c in chamados:
        resumo[c.situacao] = resumo.get(c.situacao, 0) + 1
    return resumo


def conferir_no_tomticket(sessao, chamados: list[Chamado], cfg: Config,
                          registro: Registro | None = None) -> list[str]:
    """Rele no TomTicket os que ficaram PRONTO e rebaixa quem ja foi cobrado.

    Rode isto entre CLASSIFICAR e MOSTRAR. O dashboard e uma copia e sincroniza
    de tempos em tempos: uma cobranca escrita ha dez minutos ainda nao esta
    nele, e o chamado aparece na lista como se nada tivesse sido feito. Quem
    olha a tela marca, manda, e so entao a trava do envio descobre - tarde.

    A leitura e a MESMA que o envio faz um instante antes de escrever
    (`sessao.conversa_contem`), so que um instante antes de mostrar.

    Devolve os protocolos rebaixados. So mexe em PRONTO: sao os unicos que
    valem uma leitura por chamado no TomTicket.

    Nao levanta excecao por chamado: o que nao puder ser conferido CONTINUA
    pronto. Na duvida, oferecer e o erro barato - a trava do envio ainda esta
    la, e ela e a que impede a cobranca repetida de verdade.
    """
    from .tomticket import procurar_marcas

    registro = registro or Registro()
    prontos = [c for c in chamados if c.situacao == PRONTO]
    if not prontos:
        return []

    marcas = assinaturas_de_bloqueio(cfg)
    rebaixados: list[str] = []

    for posicao, chamado in enumerate(prontos, 1):
        registro.progresso(posicao, len(prontos), chamado.protocolo)
        if not chamado.ticket_id:
            registro.log(
                f"{chamado.protocolo}: sem id interno; nao da para conferir.",
                AVISO)
            continue
        try:
            texto, ultima = sessao.conversa(chamado.ticket_id)
        except Exception as erro:
            registro.log(
                f"{chamado.protocolo}: nao consegui conferir ({erro})", AVISO)
            continue

        marca = procurar_marcas(texto, marcas)
        if marca:
            chamado.situacao = JA_ESCALADO
            chamado.motivo = (
                "Conferido no TomTicket agora: a cobranca JA esta no chamado "
                f'(trecho: "{marca[:60]}"). O dashboard ainda nao sincronizou.')
            rebaixados.append(chamado.protocolo)
            continue

        # E o RELOGIO, pela conversa ao vivo. O prazo do dashboard e contado
        # sobre a copia dele: se a copia nao tem a resposta de uma hora atras,
        # a conta sai do recado anterior e da "prazo vencido" num chamado que
        # acabou de ser respondido.
        horas_prazo = chamado.gatilho.horas_prazo if chamado.gatilho else 0
        if not (ultima and horas_prazo):
            continue
        decorridas = (datetime.now() - ultima).total_seconds() / 3600
        chamado.horas_decorridas = round(decorridas, 1)
        chamado.prazo_excedido = decorridas >= horas_prazo
        if chamado.prazo_excedido:
            continue
        chamado.situacao = AGUARDANDO_PRAZO
        chamado.motivo = (
            f"Conferido no TomTicket agora: a ultima mensagem e de "
            f"{ultima:%d/%m %H:%M} - faltam {horas_prazo - decorridas:.1f}h "
            f"para estourar o prazo de {horas_prazo}h.")
        rebaixados.append(chamado.protocolo)

    if rebaixados:
        registro.log(
            f"{len(rebaixados)} sairam da lista, conferidos no TomTicket: "
            + ", ".join(rebaixados), OK)
    return rebaixados


__all__ = [
    "PRONTO", "AGUARDANDO_PRAZO", "JA_ESCALADO", "FORA_DE_ESCOPO",
    "INDETERMINADO", "ORDEM_SITUACOES", "Chamado", "Resposta",
    "classificar", "normalizar", "extrair_data", "ordenar_respostas",
    "montar_chamado", "aplicar_detalhe", "prazo_legivel", "resumir",
    "assinaturas_de_bloqueio", "conferir_no_tomticket",
]
