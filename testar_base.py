"""
Teste da base - offline, sem rede e sem navegador.

A base e uma copia isolada: ela precisa provar que funciona SOZINHA, fora do
projeto de onde veio. Entao o primeiro teste e o mais bobo e o mais
importante - ela importa sem o PHDS por perto? - e os demais cobrem as regras
que decidem o que e escrito no chamado de um cliente.

    python testar_base.py
"""

import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))

falhas: list[str] = []

# O que vem com o Python - nao conta como dependencia da base.
# Modulos da biblioteca padrao (mais o playwright, que e opcional e so
# aparece dentro de funcao). Tudo que NAO estiver aqui conta como dependencia
# de terceiros - e a base so pode ter duas.
PADRAO = {"__future__", "ast", "base64", "copy", "ctypes", "dataclasses",
          "datetime", "html", "json", "os", "pathlib", "re", "subprocess",
          "sys", "tempfile", "threading", "time", "typing", "unicodedata",
          "urllib", "playwright"}


def conferir(descricao: str, condicao: bool, detalhe: str = "") -> None:
    print(f"   [{'OK  ' if condicao else 'FALHA'}] {descricao}")
    if not condicao:
        falhas.append(descricao + (f" ({detalhe})" if detalhe else ""))


def testar_independencia() -> None:
    """A base nao pode depender do projeto de onde saiu."""
    print("\nA base vive sozinha")
    import base

    caminhos = [m for m in sys.modules
                if m.startswith(("interface", "nucleo"))]
    conferir("nao carrega nada do PHDS", caminhos == [], str(caminhos))

    proibidos = ("PySide6", "selenium")
    carregados = [p for p in proibidos if p in sys.modules]
    conferir("nao carrega interface nem Selenium", carregados == [],
             str(carregados))

    # Procura IMPORT, nao a palavra: os docstrings falam de PySide6 e de
    # playwright justamente para dizer que a base nao depende deles.
    import ast

    importados, no_topo = set(), set()
    for arquivo in (AQUI / "base").glob("*.py"):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            nomes = []
            if isinstance(no, ast.Import):
                nomes = [a.name.split(".")[0] for a in no.names]
            elif isinstance(no, ast.ImportFrom) and no.level == 0 and no.module:
                nomes = [no.module.split(".")[0]]
            importados.update(nomes)
            if nomes and no.col_offset == 0:
                no_topo.update(nomes)

    conferir("nenhum modulo importa PySide6", "PySide6" not in importados,
             str(sorted(importados)))
    conferir("nem Selenium", "selenium" not in importados)
    conferir("o playwright, quando aparece, e dentro da funcao (opcional)",
             "playwright" not in no_topo, str(sorted(no_topo)))

    de_fora = importados - PADRAO
    conferir("as dependencias obrigatorias sao so requests e PyYAML",
             de_fora <= {"requests", "yaml"}, str(sorted(de_fora)))


def testar_config() -> None:
    print("\nOs textos e gatilhos vem do config.yaml")
    from base import config

    cfg = config.carregar()
    conferir("dois gatilhos: 24h e 48h",
             sorted(g.id for g in cfg.gatilhos) == ["24h", "48h"])
    conferir("com prazo em horas",
             [g.horas_prazo for g in cfg.gatilhos] == [24, 48])
    conferir("e uma resposta para cada",
             set(cfg.respostas) == {"24h", "48h"})

    print("\n   A trava do laco: resposta nao pode disparar gatilho")
    conferir("o config que veio esta limpo", config.conferir_travas(cfg) == [])

    envenenado = config.Config(
        gatilhos=cfg.gatilhos,
        respostas={"24h": cfg.gatilhos[0].texto_original})
    conferir("um config que se auto-dispara e recusado",
             config.conferir_travas(envenenado) != [])


def testar_classificacao() -> None:
    """A regra que decide se um chamado e cobravel."""
    print("\nSo e cobravel quem tem o texto padrao por ULTIMO")
    from base import cobranca, config

    cfg = config.carregar()
    gatilho = cfg.gatilhos[0]

    def chamado_com(*textos):
        ch = cobranca.Chamado(protocolo="100001", ticket_id="a" * 32)
        ch.respostas = [
            cobranca.Resposta(texto=t, autor="Fulano", tipo_autor="A")
            for t in textos]
        return cobranca.classificar(ch, cfg)

    ch = chamado_com("bom dia", gatilho.texto_original)
    conferir("texto padrao por ultimo: PRONTO", ch.situacao == cobranca.PRONTO,
             ch.situacao)
    conferir("e sabe qual gatilho foi",
             ch.gatilho and ch.gatilho.id == gatilho.id)

    ch = chamado_com(gatilho.texto_original, "vou mandar a equipe amanha")
    conferir("respondido depois do texto padrao: NAO e cobravel",
             ch.situacao != cobranca.PRONTO, ch.situacao)

    ch = chamado_com(gatilho.texto_original, cfg.resposta_para(gatilho.id))
    conferir("ja cobrado: nao cobra de novo",
             ch.situacao == cobranca.JA_ESCALADO, ch.situacao)

    ch = chamado_com("qualquer conversa")
    conferir("sem o texto padrao: fora de escopo",
             ch.situacao == cobranca.FORA_DE_ESCOPO, ch.situacao)

    ch = chamado_com()
    conferir("sem mensagem nenhuma: indeterminado, nao cobravel",
             ch.situacao == cobranca.INDETERMINADO, ch.situacao)

    print("\n   E a formatacao nao atrapalha")
    ch = chamado_com(gatilho.texto_original.upper().replace(" ", "  "))
    conferir("maiusculas e espacos dobrados ainda casam",
             ch.situacao == cobranca.PRONTO, ch.situacao)


def testar_vigia() -> None:
    print("\nA vigia: o que e novo e qual prazo responder")
    from base import vigia

    for prioridade in ("Alta", "urgente", "URGENTE"):
        conferir(f"{prioridade!r} -> 24h", vigia.prazo_de(prioridade) == "24h")
    for prioridade in ("Normal", "baixa"):
        conferir(f"{prioridade!r} -> 48h", vigia.prazo_de(prioridade) == "48h")
    for prioridade in ("", "Média", "seja la o que for"):
        conferir(f"{prioridade!r} nao vira prazo nenhum",
                 vigia.prazo_de(prioridade) is None)

    def item(protocolo, quando="01/01/2026 08:00:00"):
        return {"protocolo": protocolo, "id": protocolo * 4,
                "titulo": "assunto", "prioridade": "Alta",
                "dataultimasituacao": quando, "datahora": quando}

    lista = [item("1"), item("2"), item("3")]
    conferir("com a memoria vazia, todos sao novos",
             len(vigia.separar_novos(lista, set())) == 3)
    conferir("o que ja foi visto nao volta",
             [c.protocolo for c in vigia.separar_novos(lista, {"1", "2"})]
             == ["3"])
    conferir("a mesma pagina duas vezes nao duplica",
             len(vigia.separar_novos(lista + lista, set())) == 3)

    print("\n   A linha de base poupa o que acabou de chegar")
    from datetime import datetime, timedelta
    agora = datetime(2026, 9, 1, 14, 5)
    antigos = [item(str(100 + i), "15/07/2026 10:00:00") for i in range(50)]
    recem = item("999", "01/09/2026 14:01:00")
    base = vigia.linha_de_base(antigos + [recem], agora=agora)
    conferir("os antigos viram paisagem", len(base) == 50)
    conferir("e o de tres minutos atras fica de fora, para ser avisado",
             "999" not in base)


def testar_extracao_de_conversas() -> None:
    print("\nA extracao rebaixa so o que mudou")
    import tempfile
    from base import finalizacao

    with tempfile.TemporaryDirectory() as pasta:
        cache = finalizacao.CacheDeConversas(pasta)
        item = {"protocolo": "1", "id": "a" * 32,
                "dataultimasituacao": "01/09/2026 10:00:00"}
        conferir("chamado que nao esta no cache: baixa",
                 cache.precisa_baixar(item))

        cache.gravar(item["id"], finalizacao.montar_registro(
            item, {"historicos": [
                {"tipo": "H", "byAtendente": True, "nomepessoa": "CSM",
                 "mensagem": "<p>primeira<br>linha</p>", "unixtime": 1},
            ]}))
        conferir("mesma data: nao baixa de novo",
                 not cache.precisa_baixar(item))

        mexido = dict(item, dataultimasituacao="01/09/2026 15:00:00")
        conferir("data diferente: baixa", cache.precisa_baixar(mexido))

        guardado = cache.ler(item["id"])
        conferir("a mensagem virou texto",
                 guardado["mensagens"][0]["message"] == "primeira\nlinha",
                 repr(guardado["mensagens"][0]["message"]))
        conferir("com quem falou",
                 guardado["mensagens"][0]["sender_type"] == "A")
        conferir("e o cache le tudo de volta", len(cache.todos()) == 1)


def testar_filtros_do_dashboard() -> None:
    print("\nOs filtros da tela de Alertas")
    from base import dashboard

    filtros = dashboard.filtros_da_url(
        "https://x.org/alertas?projeto=SRT&dept=MANUTEN%C3%87%C3%83O+-+SRT")
    conferir("le projeto da URL", filtros.get("projeto") == "SRT", str(filtros))
    conferir("le departamento com acento",
             filtros.get("departamento") == "MANUTENÇÃO - SRT", str(filtros))
    conferir("e a aba tem padrao", filtros.get("aba") == "sem_retorno")

    alertas = [
        {"protocolo": "1", "projeto": "SRT", "departamento": "MANUTENÇÃO - SRT",
         "has_helpdesk_response": False},
        {"protocolo": "2", "projeto": "SRT", "departamento": "MANUTENÇÃO - SRT",
         "has_helpdesk_response": True},
        {"protocolo": "3", "projeto": "OUTRO", "departamento": "MANUTENÇÃO - SRT",
         "has_helpdesk_response": False},
    ]
    passaram = [a["protocolo"] for a in dashboard.filtrar(alertas, filtros)]
    conferir("so o do projeto certo e sem retorno passa", passaram == ["1"],
             str(passaram))


def testar_falha_passageira() -> None:
    """Lentidao do servidor nao pode virar erro na cara de quem usa."""
    print("\nFalha passageira do servidor vira nova tentativa")
    import tempfile
    import time as relogio
    from base import dashboard

    conferir("500 com statement timeout vale repetir",
             bool(dashboard._vale_repetir(
                 500, '{"error":"canceling statement due to statement timeout"}')))
    conferir("502/503/504 tambem",
             all(dashboard._vale_repetir(s, "") for s in (502, 503, 504)))
    conferir("401 nao - isso e login",
             not dashboard._vale_repetir(401, "nao autorizado"))
    conferir("404 nao - repetir da o mesmo",
             not dashboard._vale_repetir(404, "nao existe"))

    respostas = [(500, "statement timeout"), (500, "statement timeout"),
                 (200, "[]")]
    chamadas, esperas = [], []

    class SessaoQueFalha(dashboard.SessaoDashboard):
        def _uma_chamada(self, metodo, caminho, corpo):
            chamadas.append(caminho)
            return respostas[len(chamadas) - 1]

    dormir = relogio.sleep
    relogio.sleep = lambda s: esperas.append(s)
    try:
        with tempfile.TemporaryDirectory() as pasta:
            sessao = SessaoQueFalha("https://exemplo.org",
                                    Path(pasta) / "s.json")
            status, _ = sessao.chamar("GET", "/tickets/sla-alertas")
    finally:
        relogio.sleep = dormir

    conferir("insistiu ate dar certo", status == 200 and len(chamadas) == 3,
             f"{len(chamadas)} tentativas, status {status}")
    conferir("com pausa entre as tentativas", esperas == [3, 8], str(esperas))


def testar_conferencia_ao_vivo() -> None:
    """O dashboard atrasa; a lista de prontos nao pode atrasar junto."""
    print("\nA conferencia ao vivo tira da lista quem nao pode ser cobrado")
    from datetime import datetime, timedelta

    from base import cobranca, config

    cfg = config.carregar()
    gatilho = cfg.gatilhos[0]        # o de 24h

    def pronto(protocolo, ticket_id):
        ch = cobranca.Chamado(protocolo=protocolo, ticket_id=ticket_id)
        ch.respostas = [cobranca.Resposta(
            texto=gatilho.texto_original, autor="Fulano", tipo_autor="A",
            data=datetime.now() - timedelta(hours=40))]
        return cobranca.classificar(ch, cfg)

    class TomTicketDeMentira:
        """conversas: {ticket_id: (texto, ha quantas horas foi a ultima)}."""

        def __init__(self, conversas=None, quebra=()):
            self.conversas = conversas or {}
            self.quebra = set(quebra)
            self.perguntados = []

        def conversa(self, ticket_id):
            self.perguntados.append(ticket_id)
            if ticket_id in self.quebra:
                raise RuntimeError("a conversa nao carregou")
            texto, horas = self.conversas.get(ticket_id, ("bom dia", 99))
            return texto, datetime.now() - timedelta(hours=horas)

    A, B, C = "a" * 32, "b" * 32, "c" * 32

    chamados = [pronto("111", A), pronto("222", B)]
    chamados.append(cobranca.Chamado(protocolo="333", ticket_id=C))
    chamados[-1].situacao = cobranca.FORA_DE_ESCOPO

    falso = TomTicketDeMentira({
        A: ("Equipe, a demanda ja excedeu o prazo de 24 horas", 40),
        B: ("bom dia, vamos verificar", 40),
    })
    rebaixados = cobranca.conferir_no_tomticket(falso, chamados, cfg)

    conferir("o ja cobrado deixa de ser PRONTO",
             chamados[0].situacao == cobranca.JA_ESCALADO, chamados[0].situacao)
    conferir("quem nao foi cobrado e ja venceu continua PRONTO",
             chamados[1].situacao == cobranca.PRONTO, chamados[1].situacao)
    conferir("e devolve quem saiu da lista", rebaixados == ["111"],
             str(rebaixados))
    conferir("nao gasta leitura com quem nao esta pronto",
             len(falso.perguntados) == 2, str(len(falso.perguntados)))

    print("\n   O relogio tambem sai da conversa ao vivo")
    chamados = [pronto("444", A)]
    falso = TomTicketDeMentira({A: ("bom dia, vamos verificar", 1)})
    rebaixados = cobranca.conferir_no_tomticket(falso, chamados, cfg)
    conferir("respondido ha 1h com prazo de 24h nao e cobravel",
             chamados[0].situacao == cobranca.AGUARDANDO_PRAZO,
             chamados[0].situacao)
    conferir("e o motivo diz quanto falta",
             "faltam 23.0h" in chamados[0].motivo, chamados[0].motivo[:80])
    conferir("sai da lista", rebaixados == ["444"], str(rebaixados))

    print("\n   Na duvida, oferece: falhar ao conferir nao esconde chamado")
    chamados = [pronto("111", A), pronto("222", B)]
    falso = TomTicketDeMentira({B: ("ja excedeu o prazo", 40)}, quebra=(A,))
    rebaixados = cobranca.conferir_no_tomticket(falso, chamados, cfg)
    conferir("o que quebrou continua pronto",
             chamados[0].situacao == cobranca.PRONTO)
    conferir("e o seguinte foi conferido assim mesmo", rebaixados == ["222"],
             str(rebaixados))


def testar_fuso_horario() -> None:
    """A conta do prazo e um tempo decorrido: fuso errado = prazo errado."""
    print("\nDatas com 'Z' e com '-03' dao a MESMA hora local")
    from base import cobranca, vigia

    com_fuso = cobranca.extrair_data("2026-09-01 11:23:40-03")
    em_utc = cobranca.extrair_data("2026-09-01T14:23:40.000Z")
    conferir("o mesmo instante nos dois formatos", com_fuso == em_utc,
             f"{com_fuso} != {em_utc}")
    conferir("e nao fica no futuro", com_fuso.hour == 11, str(com_fuso))
    conferir("a vigia le do mesmo jeito",
             vigia.quando("2026-09-01T14:23:40.000Z") == em_utc,
             str(vigia.quando("2026-09-01T14:23:40.000Z")))

    print("\n   E o texto do prazo nao arredonda para parecer contradicao")
    chamado = cobranca.Chamado(protocolo="1")
    chamado.gatilho = cobranca.Gatilho(id="24h", horas_prazo=24,
                                       assinatura="x", texto_original="x")
    chamado.horas_decorridas = 23.9
    chamado.prazo_excedido = False
    conferir("23,9h nao vira o enganoso 'nao (24h)'",
             cobranca.prazo_legivel(chamado) != "nao (24h)",
             cobranca.prazo_legivel(chamado))


def testar_formato_unico() -> None:
    """As duas portas do TomTicket saem no mesmo formato."""
    print("\nAPI oficial e console desaguam no mesmo Chamado")
    from base import chamados

    da_api = chamados.de_api({
        "id": "abc", "protocol": "328731", "subject": "Ralo entupido",
        "customer": {"name": "CAPS LIMA BARRETO", "internal_id": "u1"},
        "organization": {"id": "1", "name": "SRT"},
        "department": {"id": "143645", "name": "MANUTENÇÃO - SRT"},
        "operator": {"name": "CSM - SRT"},
        "priority": 3,
        "situation": {"id": 2, "description": "Respondido",
                      "apply_date": "2026-09-01T11:23:40-03:00"},
        "creation_date": "2026-09-01T11:21:34-03:00",
    })
    do_console = chamados.de_console({
        "id": "abc", "protocolo": "328731", "titulo": "Ralo entupido",
        "nomecliente": "CAPS LIMA BARRETO", "idcliente": "u1",
        "nomeorganizacao": "SRT", "codproduto": "MANUTENÇÃO - SRT",
        "atendente": "CSM - SRT", "prioridade": "Alta", "ultimasituacao": 2,
        "datahora": "01/09/2026 11:21", "dataultimasituacao": "01/09/2026 11:23",
    })

    for campo in ("protocolo", "ticket_id", "assunto", "unidade",
                  "departamento", "atendente", "prioridade", "situacao_id"):
        conferir(f"{campo} bate nas duas fontes",
                 getattr(da_api, campo) == getattr(do_console, campo),
                 f"{getattr(da_api, campo)!r} != {getattr(do_console, campo)!r}")
    conferir("a prioridade numerica da API vira texto",
             da_api.prioridade == "Alta", da_api.prioridade)
    conferir("os dois estao abertos", da_api.aberto and do_console.aberto)
    conferir("e viram JSON sem data solta",
             isinstance(da_api.como_dicionario()["criado_em"], str))

    print("\n   Situacao 5 e Finalizada; codigo novo NAO fecha sozinho")
    fechado = chamados.de_api({"protocol": "1", "situation": {"id": 5}})
    conferir("finalizada nao esta aberta", not fechado.aberto)
    desconhecido = chamados.de_api({"protocol": "2", "situation": {"id": 99}})
    conferir("codigo desconhecido continua aberto (na duvida, mostra)",
             desconhecido.aberto)


def testar_janela_e_limite() -> None:
    """Os dois limites da API que nao aparecem como erro."""
    print("\nA janela de 90 dias e o limite de 3 por segundo")
    import time
    from datetime import datetime, timedelta
    from base import tomticket_api

    antigo = datetime.now() - timedelta(days=200)
    limitado = tomticket_api.limitar_janela(antigo)
    conferir("nao pede mais de 90 dias para tras",
             (datetime.now() - limitado).days <= 90,
             str((datetime.now() - limitado).days))
    recente = datetime.now() - timedelta(hours=2)
    conferir("e nao mexe numa janela curta",
             tomticket_api.limitar_janela(recente) == recente)
    conferir("a data sai no formato que a API espera",
             tomticket_api.formatar_data(datetime(2026, 9, 1, 11, 23, 40))
             == "2026-09-01 11:23:40")

    limitador = tomticket_api.Limitador(por_segundo=3)
    inicio = time.monotonic()
    for _ in range(4):
        limitador.esperar()
    gasto = time.monotonic() - inicio
    conferir("quatro chamadas levam ao menos 1s a 3/s", gasto >= 0.99,
             f"{gasto:.2f}s")

    print("\n   Falha passageira vale repetir; token recusado, nao")
    vale = tomticket_api._vale_repetir
    conferir("429 vale repetir", bool(vale(429, "")))
    conferir("503 tambem", bool(vale(503, "")))
    conferir("rede caida tambem", bool(vale(0, "erro")))
    conferir("401 nao - e o token", not vale(401, ""))
    conferir("404 nao - repetir da o mesmo", not vale(404, ""))


class _FonteDeMentira:
    """Uma API falsa: guarda os chamados e responde como a de verdade."""

    def __init__(self, abertos=(), mudancas=()):
        from base.chamados import de_api
        self.abertos_iniciais = [de_api(b) for b in abertos]
        self.mudancas = [[de_api(b) for b in lote] for lote in mudancas]
        self.filtros_usados = []
        self.quebrar = False

    def abertos_do_departamento(self, department_id, desde=None, **extra):
        if self.quebrar:
            raise RuntimeError("a rede caiu")
        return list(self.abertos_iniciais)

    def chamados(self, **filtros):
        if self.quebrar:
            raise RuntimeError("a rede caiu")
        self.filtros_usados.append(filtros)
        return list(self.mudancas.pop(0)) if self.mudancas else []


def _bruto(protocolo, situacao=2, assunto="x"):
    return {"id": f"id{protocolo}", "protocol": protocolo, "subject": assunto,
            "situation": {"id": situacao},
            "creation_date": "2026-09-01T09:00:00-03:00"}


def testar_coletor() -> None:
    """A foto que o site le - e a armadilha do incremental."""
    print("\nO coletor mantem a lista em dia")
    from base.coletor import Coletor

    fonte = _FonteDeMentira(
        abertos=[_bruto("111"), _bruto("222")],
        mudancas=[[_bruto("333"), _bruto("111", situacao=5)]])
    coletor = Coletor(fonte, "143645")

    primeira = coletor.sincronizar()
    conferir("a primeira le tudo", primeira.completa)
    conferir("e a lista tem os dois", sorted(coletor.chamados) == ["111", "222"],
             str(sorted(coletor.chamados)))

    segunda = coletor.sincronizar()
    conferir("a segunda le so o que mudou", not segunda.completa)
    conferir("o chamado novo entrou", segunda.novos == ["333"],
             str(segunda.novos))
    conferir("O QUE FECHOU SAIU DA LISTA", segunda.fechados == ["111"],
             str(segunda.fechados))
    conferir("e a lista final esta certa",
             sorted(coletor.chamados) == ["222", "333"],
             str(sorted(coletor.chamados)))

    print("\n   A leitura incremental NAO filtra situacao - e o motivo")
    filtros = fonte.filtros_usados[-1]
    conferir("pede tudo que mudou, aberto ou nao",
             "situation" not in filtros, str(sorted(filtros)))
    conferir("com o corte de data", "last_update_ge" in filtros,
             str(sorted(filtros)))
    conferir("e so daquele departamento",
             filtros.get("department_id") == "143645", str(filtros))

    print("\n   Uma falha de rede nao apaga a lista que o site esta usando")
    fonte.quebrar = True
    relogio = coletor.ultimo_sync
    falha = coletor.sincronizar()
    conferir("o erro e reportado", bool(falha.erro), falha.erro)
    conferir("a lista continua de pe",
             sorted(coletor.chamados) == ["222", "333"],
             str(sorted(coletor.chamados)))
    conferir("e o relogio nao avanca (a janela sera relida)",
             coletor.ultimo_sync == relogio)

    print("\n   E a foto sai pronta para virar JSON")
    fonte.quebrar = False
    dados = coletor.como_json()
    conferir("uma linha por chamado aberto", len(dados) == 2, str(len(dados)))
    conferir("com o campo aberto resolvido",
             all(d["aberto"] for d in dados))
    conferir("e sem objeto de data solto",
             all(d["criado_em"] is None or isinstance(d["criado_em"], str)
                 for d in dados))


def main() -> int:
    print("=" * 70)
    print(" BASE-AUTOMATIZACAO - nenhum teste acessa a rede")
    print("=" * 70)

    testar_independencia()
    testar_config()
    testar_classificacao()
    testar_vigia()
    testar_extracao_de_conversas()
    testar_filtros_do_dashboard()
    testar_falha_passageira()
    testar_conferencia_ao_vivo()
    testar_fuso_horario()
    testar_formato_unico()
    testar_janela_e_limite()
    testar_coletor()

    print()
    print("=" * 70)
    if falhas:
        print(f" {len(falhas)} PROBLEMA(S):")
        for f in falhas:
            print(f"   - {f}")
        print("=" * 70)
        return 1
    print(" A base funciona sozinha.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
