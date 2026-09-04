"""
Um chamado, um formato so - venha ele de onde vier.

Existem duas maneiras de ler chamados do TomTicket, e elas devolvem campos com
nomes diferentes:

    API oficial   GET api.tomticket.com/v2.0/ticket/list   (Bearer token)
    console       POST console.tomticket.com/.../advancedfilter  (cookie)

Quem consome - um site de rotas, um painel, uma planilha - nao deveria precisar
saber qual das duas trouxe o dado. Entao as duas passam por aqui e saem como
`Chamado`, com os mesmos nomes.

Isso tambem e o que permite trocar de fonte sem reescrever o resto: comeca-se
pelo console (que funciona com o login que voce ja tem) e migra-se para a API
oficial quando o token sair, sem tocar em quem consome.

    from base.chamados import de_api, de_console

    for bruto in api.chamados(department_id="143645"):
        chamado = de_api(bruto)
        print(chamado.protocolo, chamado.unidade, chamado.aberto)
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime

# Os codigos de "situacao" do TomTicket. Sao os mesmos nos dois lados: vieram
# do catalogo do proprio console e batem com a faixa 0-11 que a API documenta.
SITUACOES = {
    0: "Sem atendente vinculado",
    1: "Nao iniciada pelo atendente",
    2: "Respondido, aguardando resposta do cliente",
    3: "Respondido pelo cliente, aguardando resposta",
    4: "Cancelada",
    5: "Finalizada",
    6: "Atendente modificado",
    8: "Aguardando aprovacao de finalizacao do gerente",
    9: "Aguardando aprovacao de cancelamento do gerente",
    10: "Aguardando avaliacao do gerente para enviar ao cliente",
    11: "Aguardando aprovacao do cliente",
}

# Chamado fora de cena. Todo o resto conta como aberto.
FECHADAS = (4, 5)
ABERTAS = tuple(c for c in sorted(SITUACOES) if c not in FECHADAS)

# A API devolve prioridade como numero; o console, como texto.
PRIORIDADES = {1: "Baixa", 2: "Normal", 3: "Alta", 4: "Urgente"}


def _texto(valor) -> str:
    return "" if valor is None else str(valor).strip()


def _chave(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", _texto(texto))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def quando(texto) -> datetime | None:
    """A data como qualquer um dos dois lados a escreve.

    A API manda ISO com fuso ("2026-09-01T11:23:40-03:00"); o console manda
    "01/09/2026 11:23". E ha o caso do "...Z", que e UTC: convertido, e nao
    apenas despido do fuso - senao a data fica horas no futuro e toda conta de
    tempo decorrido feita em cima dela encurta na mesma medida.
    """
    bruto = _texto(texto)
    if not bruto:
        return None
    try:
        lida = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        if lida.tzinfo is not None:
            lida = lida.astimezone()
        return lida.replace(tzinfo=None)

    import re
    achado = re.search(
        r"(\d{2})[/-](\d{2})[/-](\d{4})(?:[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        bruto)
    if not achado:
        return None
    dia, mes, ano, hora, minuto, segundo = achado.groups()
    try:
        return datetime(int(ano), int(mes), int(dia),
                        int(hora or 0), int(minuto or 0), int(segundo or 0))
    except ValueError:
        return None


@dataclass
class Chamado:
    """Um chamado, com os nomes que quem consome usa."""
    protocolo: str = ""
    ticket_id: str = ""
    assunto: str = ""
    mensagem: str = ""
    unidade: str = ""          # o cliente: a UNIDADE onde o servico acontece
    unidade_id: str = ""
    organizacao: str = ""
    departamento: str = ""
    departamento_id: str = ""
    categoria: str = ""
    prioridade: str = ""
    atendente: str = ""
    situacao: str = ""
    situacao_id: int | None = None
    criado_em: datetime | None = None
    atualizado_em: datetime | None = None
    encerrado_em: datetime | None = None
    prazo: datetime | None = None
    fonte: str = ""            # "api" | "console"
    bruto: dict = field(default_factory=dict, repr=False)

    @property
    def aberto(self) -> bool:
        """Nao foi finalizado nem cancelado.

        Quando a situacao nao vier (ou vier um codigo novo), a data de
        encerramento decide. Na duvida, ABERTO: sumir com um chamado da rota
        por causa de um codigo desconhecido e pior do que mostrar um a mais.
        """
        if self.situacao_id in FECHADAS:
            return False
        return self.encerrado_em is None

    @property
    def idade_em_horas(self) -> float | None:
        if not self.criado_em:
            return None
        return (datetime.now() - self.criado_em).total_seconds() / 3600

    def como_dicionario(self, com_bruto: bool = False) -> dict:
        """Pronto para virar JSON - as datas viram ISO."""
        dados = asdict(self)
        dados.pop("bruto", None)
        for campo in ("criado_em", "atualizado_em", "encerrado_em", "prazo"):
            valor = dados.get(campo)
            dados[campo] = valor.isoformat(timespec="seconds") if valor else None
        dados["aberto"] = self.aberto
        if com_bruto:
            dados["bruto"] = self.bruto
        return dados


def de_api(bruto: dict) -> Chamado:
    """Um item de `GET /v2.0/ticket/list` (ou `/ticket/detail`)."""
    cliente = bruto.get("customer") or {}
    organizacao = bruto.get("organization") or {}
    departamento = bruto.get("department") or {}
    categoria = bruto.get("category") or {}
    operador = bruto.get("operator") or {}
    situacao = bruto.get("situation") or {}
    sla = (bruto.get("sla") or {}).get("deadline") or {}

    identificador = situacao.get("id")
    try:
        identificador = int(identificador)
    except (TypeError, ValueError):
        identificador = None

    prioridade = bruto.get("priority")
    if isinstance(prioridade, int) or _texto(prioridade).isdigit():
        prioridade = PRIORIDADES.get(int(prioridade), _texto(prioridade))

    return Chamado(
        protocolo=_texto(bruto.get("protocol")),
        ticket_id=_texto(bruto.get("id")),
        assunto=_texto(bruto.get("subject")),
        mensagem=_texto(bruto.get("message")),
        unidade=_texto(cliente.get("name")),
        unidade_id=_texto(cliente.get("internal_id") or cliente.get("email")),
        organizacao=_texto(organizacao.get("name")),
        departamento=_texto(departamento.get("name")),
        departamento_id=_texto(departamento.get("id")),
        categoria=_texto(categoria.get("name")),
        prioridade=_texto(prioridade),
        atendente=_texto(operador.get("name")),
        situacao=_texto(situacao.get("description")) or SITUACOES.get(identificador, ""),
        situacao_id=identificador,
        criado_em=quando(bruto.get("creation_date")),
        atualizado_em=quando(situacao.get("apply_date")),
        encerrado_em=quando(bruto.get("end_date")),
        prazo=quando(sla.get("date")),
        fonte="api",
        bruto=bruto,
    )


def de_console(bruto: dict) -> Chamado:
    """Um item da busca avancada do console (`ticket/advancedfilter`)."""
    identificador = bruto.get("ultimasituacao")
    try:
        identificador = int(identificador)
    except (TypeError, ValueError):
        identificador = None

    return Chamado(
        protocolo=_texto(bruto.get("protocolo")),
        ticket_id=_texto(bruto.get("id")),
        assunto=_texto(bruto.get("titulo")),
        unidade=_texto(bruto.get("nomecliente") or bruto.get("idcliente")),
        unidade_id=_texto(bruto.get("idcliente")),
        organizacao=_texto(bruto.get("nomeorganizacao")),
        departamento=_texto(bruto.get("codproduto")),
        categoria=_texto(bruto.get("codtipoassunto")),
        prioridade=_texto(bruto.get("prioridade")),
        atendente=_texto(bruto.get("atendente")),
        situacao=_texto(bruto.get("labelsituacao"))
                 or SITUACOES.get(identificador, ""),
        situacao_id=identificador,
        criado_em=quando(bruto.get("datahora")),
        atualizado_em=quando(bruto.get("dataultimasituacao")),
        encerrado_em=quando(bruto.get("dataencerramento")),
        fonte="console",
        bruto=bruto,
    )


def do_dashboard(bruto: dict) -> Chamado:
    """Um alerta de `/api/tickets/sla-alertas` do dashboard IGEDES.

    O dashboard e uma COPIA do TomTicket e demora a largar um chamado fechado -
    ja foram vistos 80 chamados listados nele semanas depois de finalizados.
    Use-o para os campos que so ele tem (projeto, horas_abertas, sla_horas), e
    o TomTicket para saber se o chamado ainda esta aberto.
    """
    return Chamado(
        protocolo=_texto(bruto.get("protocolo")),
        ticket_id=_texto(bruto.get("ticket_id") or bruto.get("id")),
        assunto=_texto(bruto.get("assunto")),
        unidade=_texto(bruto.get("cliente")),
        organizacao=_texto(bruto.get("projeto")),
        departamento=_texto(bruto.get("departamento")),
        categoria=_texto(bruto.get("categoria")),
        prioridade=_texto(bruto.get("prioridade")),
        atendente=_texto(bruto.get("atendente")),
        situacao=_texto(bruto.get("ultima_situacao")),
        criado_em=quando(bruto.get("data_criacao")),
        fonte="dashboard",
        bruto=bruto,
    )


def por_protocolo(chamados) -> dict:
    return {c.protocolo: c for c in chamados if c.protocolo}


def somente_abertos(chamados) -> list:
    return [c for c in chamados if c.aberto]


def do_departamento(chamados, departamento: str) -> list:
    """Filtra pelo NOME do departamento, sem se importar com acento e caixa."""
    alvo = _chave(departamento)
    return [c for c in chamados if _chave(c.departamento) == alvo]
