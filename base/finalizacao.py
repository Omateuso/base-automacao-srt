"""
Extracao dos chamados em aberto e das conversas - so o que mudou.

Esta e a parte que descobre, entre os chamados abertos, o que ja foi resolvido
na pratica. Aqui mora a EXTRACAO; a regra de "pode finalizar" fica com quem
usa a base, porque ela e especifica de cada operacao.

O ponto importante e o custo. A busca avancada devolve ~1.000 chamados, e
rebaixar as ~1.000 conversas a cada rodada e o que fazia isso levar minutos.
Mas cada chamado traz `dataultimasituacao`, e o cache guarda o mesmo campo:
comparar os dois diz exatamente quais conversas envelheceram. Num dia normal
sao duas ou tres.

    busca inteira (HTTP)  ->  compara com o cache  ->  baixa so o que mudou

A cobertura nao muda com isso: a busca e lida INTEIRA, todas as paginas, e
quem classifica le o cache completo. O atalho e no download, nao em quem e
considerado. `atualizar(..., tudo=True)` ignora o atalho e rebaixa tudo - e a
conferencia que prova, quando houver duvida, que nada esta ficando para tras.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .registro import DETALHE, OK, Registro
from .tomticket import ErroSessao, SessaoTomTicket


@dataclass
class Resultado:
    """O que uma atualizacao fez."""
    quando: datetime = field(default_factory=datetime.now)
    na_busca: int = 0          # chamados que a busca devolveu
    mudaram: int = 0           # conversas que precisavam ser rebaixadas
    baixadas: int = 0          # conversas efetivamente gravadas
    novas: int = 0             # chamados que nem estavam no cache
    falhas: int = 0
    segundos: float = 0.0
    completa: bool = False

    @property
    def resumo(self) -> str:
        modo = "conferencia completa" if self.completa else "leitura rapida"
        return (f"{self.na_busca} na busca · {self.baixadas} conversa(s) "
                f"atualizada(s) em {self.segundos:.0f}s ({modo})")


def html_para_texto(bruto: str) -> str:
    """A mensagem vem em HTML; o que interessa e o texto."""
    if not bruto:
        return ""
    texto = re.sub(r"<br\s*/?>", "\n", bruto, flags=re.I)
    texto = re.sub(r"</p\s*>", "\n", texto, flags=re.I)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = html.unescape(texto).replace("\xa0", " ")
    texto = re.sub(r"[ \t]+", " ", texto)
    return re.sub(r"\n\s*\n+", "\n", texto).strip()


def montar_registro(resumo: dict, detalhe: dict) -> dict:
    """Um chamado no formato do cache: dados + mensagens em ordem."""
    mensagens = []
    for item in detalhe.get("historicos") or []:
        # 'H' e mensagem da conversa; outros tipos sao eventos internos
        if item.get("tipo") and item.get("tipo") != "H":
            continue
        texto = html_para_texto(item.get("mensagem") or "")
        if not texto:
            continue
        mensagens.append({
            "sender": (item.get("nomepessoa") or "").strip(),
            "sender_type": "A" if item.get("byAtendente") else "C",
            "date": item.get("datahora") or "",
            "message": texto,
            "unixtime": item.get("unixtime"),
        })
    mensagens.sort(key=lambda m: m.get("unixtime") or 0)

    return {
        "ticket_id": resumo.get("id") or detalhe.get("id") or "",
        "protocolo": resumo.get("protocolo") or detalhe.get("protocolo"),
        "assunto": resumo.get("titulo") or detalhe.get("titulo") or "",
        "departamento": resumo.get("codproduto") or "",
        "categoria": resumo.get("codtipoassunto") or "",
        "atendente": resumo.get("atendente") or "",
        "cliente": resumo.get("nomecliente") or resumo.get("idcliente") or "",
        "projeto": resumo.get("nomeorganizacao") or "",
        "prioridade": resumo.get("prioridade") or "",
        "data_criacao": resumo.get("datahora") or "",
        "ultima_situacao": resumo.get("situacao") or "",
        "data_ultima_situacao": resumo.get("dataultimasituacao") or "",
        "mensagens": mensagens,
        "origem": "tomticket",
    }


class CacheDeConversas:
    """As conversas em disco, uma por chamado."""

    def __init__(self, pasta: Path | str):
        self.pasta = Path(pasta)

    def arquivo(self, ticket_id: str) -> Path:
        return self.pasta / f"{ticket_id}.json"

    def ler(self, ticket_id: str) -> dict | None:
        try:
            return json.loads(
                self.arquivo(ticket_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def gravar(self, ticket_id: str, registro: dict) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arquivo(ticket_id).write_text(
            json.dumps(registro, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def todos(self) -> list[dict]:
        """Tudo que esta em disco - e sobre isto que a classificacao roda."""
        saida = []
        for arquivo in sorted(self.pasta.glob("*.json")):
            try:
                saida.append(json.loads(arquivo.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return saida

    def precisa_baixar(self, item: dict) -> bool:
        """Este chamado mudou desde a ultima vez que gravamos a conversa?"""
        ticket_id = str(item.get("id") or "")
        if not ticket_id:
            return False
        guardado = self.ler(ticket_id)
        if guardado is None:
            return True
        return (str(guardado.get("data_ultima_situacao") or "")
                != str(item.get("dataultimasituacao") or ""))


def condicao_do_departamento(sessao: SessaoTomTicket, departamento: str,
                             registro: Registro | None = None):
    """A consulta da busca avancada para UM departamento, sem os fechados.

    Sem token da API oficial, a unica forma de perguntar "quais chamados deste
    departamento ainda estao abertos" e a busca avancada do console. Ela aceita
    uma condicao arbitraria, mas o formato dela e cheio de metadados de coluna
    que so o proprio console sabe montar - entao usamos a busca SALVA na conta
    como molde e trocamos so os valores.

    Duas armadilhas, as duas ja vividas:

      * `termValue1` e uma LISTA, nao o texto de uma lista. Mandando
        "['143645']" o console responde "O filtro de busca contem erros!";
      * so o PRIMEIRO bloco da busca salva e aproveitado. Os outros sao
        filtros de quem criou a busca, com outras regras - o segundo bloco da
        busca real de hoje nao filtra situacao nenhuma, e por ele entrariam
        chamados ja finalizados.

    Devolve (condicao, avisos).
    """
    import copy

    from .chamados import FECHADAS, _chave

    registro = registro or Registro()
    avisos: list[str] = []

    blocos = (sessao.busca_salva() or {}).get("condicao") or []
    if not blocos:
        raise ErroSessao(
            "nao achei busca avancada salva no TomTicket - e dela que sai o "
            "formato da consulta.")

    bloco = copy.deepcopy(blocos[0])

    def regra(pedaco):
        for r in (bloco.get("rules") or []):
            if pedaco in str((r.get("column") or {}).get("id") or ""):
                return r
        return None

    regra_dep, regra_sit = regra("codproduto"), regra("ultimasituacao")
    if regra_dep is None:
        raise ErroSessao(
            "a busca salva nao tem filtro de Departamento; sem ele nao da "
            "para montar a consulta.")

    catalogo = {str(o.get("value")): o.get("label")
                for o in (((regra_dep.get("column") or {}).get("detail") or {})
                          .get("options") or [])}
    alvo = _chave(departamento)
    identificador = next(
        (i for i, rotulo in catalogo.items() if _chave(rotulo) == alvo), "")

    if identificador:
        regra_dep["term"] = "*"
        regra_dep["termValue1"] = [identificador]
        registro.log(f"departamento {departamento} = {identificador}", DETALHE)
    else:
        avisos.append(
            f"nao achei o departamento {departamento!r} no catalogo do "
            f"TomTicket; usei o filtro da busca salva.")
        registro.log(avisos[-1], AVISO)

    if regra_sit is not None:
        regra_sit["term"] = "!=*"
        regra_sit["termValue1"] = list(FECHADAS)
    else:
        avisos.append(
            "a busca salva nao tem filtro de situacao; chamados finalizados "
            "podem entrar na lista.")
        registro.log(avisos[-1], AVISO)

    return [bloco], avisos


def abertos_do_departamento(sessao: SessaoTomTicket, departamento: str,
                            registro: Registro | None = None) -> list:
    """Os chamados abertos de um departamento, como `Chamado` normalizado.

    E o equivalente, pelo console, de
    `ApiTomTicket.abertos_do_departamento` - mesma resposta, mesmo formato,
    sem precisar de token. Use a API oficial quando ela estiver disponivel:
    aqui depende-se de um cookie de login humano e de endpoints internos.
    """
    from .chamados import de_console

    condicao, _ = condicao_do_departamento(sessao, departamento, registro)
    return [de_console(i) for i in busca_inteira(sessao, condicao, registro)]


def busca_inteira(sessao: SessaoTomTicket, condicao,
                  registro: Registro | None = None,
                  limite_de_paginas: int = 400) -> list[dict]:
    """Todas as paginas da busca avancada. Nenhum chamado fica de fora."""
    registro = registro or Registro()
    itens: list[dict] = []
    vistos: set[str] = set()
    for pagina in range(limite_de_paginas):
        lote = sessao.pagina_da_busca(condicao, pagina)
        if not lote:
            break
        desta = {str(i.get("protocolo") or "") for i in lote}
        itens.extend(lote)
        if desta <= vistos:            # a mesma pagina voltou
            break
        vistos |= desta
        registro.progresso(len(itens), 0, f"lendo a busca ({len(itens)})")
    registro.log(f"busca lida inteira: {len(itens)} chamados", DETALHE)
    return itens


def atualizar(sessao: SessaoTomTicket, itens: list[dict],
              cache: CacheDeConversas, registro: Registro | None = None,
              tudo: bool = False) -> Resultado:
    """Rebaixa as conversas que mudaram (ou todas, com `tudo=True`)."""
    registro = registro or Registro()
    inicio = datetime.now()
    resultado = Resultado(na_busca=len(itens), completa=tudo)

    pendentes = []
    for item in itens:
        if not tudo and not cache.precisa_baixar(item):
            continue
        if not str(item.get("id") or ""):
            continue
        pendentes.append(item)
        if cache.ler(str(item["id"])) is None:
            resultado.novas += 1
    resultado.mudaram = len(pendentes)

    if not pendentes:
        resultado.segundos = (datetime.now() - inicio).total_seconds()
        registro.log(
            f"cache em dia: nenhuma das {len(itens)} conversas mudou.", DETALHE)
        return resultado

    registro.log(
        f"{len(pendentes)} conversa(s) mudaram ({resultado.novas} novas).",
        DETALHE)
    for posicao, item in enumerate(pendentes, 1):
        registro.checar()
        ticket_id = str(item["id"])
        try:
            detalhe = sessao.historico(ticket_id)
        except ErroSessao as erro:
            resultado.falhas += 1
            registro.log(f"{item.get('protocolo')}: {erro}", DETALHE)
            continue
        try:
            cache.gravar(ticket_id, montar_registro(item, detalhe or {}))
            resultado.baixadas += 1
        except OSError as erro:
            resultado.falhas += 1
            registro.log(f"{item.get('protocolo')}: {erro}", DETALHE)
        registro.progresso(posicao, len(pendentes), "baixando conversas")

    resultado.segundos = (datetime.now() - inicio).total_seconds()
    registro.log(resultado.resumo, OK)
    return resultado
