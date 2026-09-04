"""
As tres frentes, uma de cada vez - para ver a base funcionando.

    python exemplo.py cobranca      le os Alertas e classifica (nao envia)
    python exemplo.py finalizacao   le a busca inteira e atualiza as conversas
    python exemplo.py vigia         diz o que chegou de novo desde a ultima vez

Nada aqui escreve em chamado de cliente. As funcoes que escrevem existem
(`enviar_resposta`, `finalizar`, `vincular`) e estao comentadas no fim de cada
exemplo, com a trava que voce deveria manter ao usa-las.

Antes de rodar, no ambiente ou num .env ao lado deste arquivo:

    DASHBOARD_URL=https://helpdesk.exemplo.org
    DASHBOARD_EMAIL=voce@exemplo.com
    DASHBOARD_SENHA=...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from base import cobranca, config, dashboard, finalizacao, tomticket, vigia
from base.registro import RegistroConsole

AQUI = Path(__file__).resolve().parent
DADOS = AQUI / "dados"
ENV = AQUI / ".env"


def _painel(registro):
    """Uma sessao do dashboard, logada."""
    cfg = config.carregar()
    if not cfg.url_base:
        raise SystemExit("Defina DASHBOARD_URL no ambiente ou no .env.")
    painel = dashboard.SessaoDashboard(
        cfg.url_base, DADOS / "sessao-dashboard.json", registro)
    cred = config.credenciais_dashboard(ENV)
    if not painel.garantir(**(cred or {})):
        raise SystemExit("Nao consegui entrar no dashboard.")
    return cfg, painel


def _tomticket(registro):
    """Uma sessao do TomTicket - pelo cookie guardado, ou pelo navegador."""
    sessao = tomticket.SessaoTomTicket(
        DADOS / "sessao-tomticket.json", registro)
    if not sessao.logado():
        cred = config.credenciais_tomticket(ENV)
        if not cred:
            raise SystemExit(
                "Sem sessao do TomTicket. Preencha TOMTICKET_CONTA/EMAIL/SENHA "
                "(o login abre o navegador uma vez) ou passe os cookies de uma "
                "sessao sua com sessao.adotar(...).")
        sessao.entrar_pelo_navegador(DADOS / "perfil-tomticket", cred)
    return sessao


def exemplo_cobranca() -> int:
    """Le os Alertas, classifica e mostra o que estaria pronto para cobrar."""
    registro = RegistroConsole()
    cfg, painel = _painel(registro)

    registro.etapa("Lendo os Alertas")
    alertas = painel.alertas()
    filtros = {"projeto": cfg.projeto, "departamento": cfg.departamento,
               "aba": "sem_retorno"}
    selecionados = dashboard.filtrar(alertas, filtros)
    registro.log(f"{len(alertas)} em Alertas -> {len(selecionados)} nos filtros")

    registro.etapa("Classificando")
    chamados = []
    for posicao, alerta in enumerate(selecionados, 1):
        chamado = cobranca.montar_chamado(alerta, cfg)
        cobranca.aplicar_detalhe(chamado, painel.detalhe(chamado.ticket_id))
        cobranca.classificar(chamado, cfg)
        chamados.append(chamado)
        registro.progresso(posicao, len(selecionados), chamado.protocolo)

    # O dashboard e uma copia e atrasa: antes de MOSTRAR a lista, confira no
    # TomTicket quem ficou pronto. Sem isto, um chamado cobrado ha dez minutos
    # continua sendo oferecido ate o proximo sync.
    registro.etapa("Conferindo os prontos no TomTicket")
    try:
        sessao = _tomticket(registro)
        cobranca.conferir_no_tomticket(sessao, chamados, cfg, registro)
    except Exception as erro:
        registro.log(
            f"sem conferencia ao vivo ({erro}); a lista e a do dashboard.",
            "aviso")

    for chamado in chamados:
        if chamado.situacao == cobranca.PRONTO:
            registro.log(
                f"{chamado.protocolo}  [{chamado.gatilho.id}]  "
                f"{chamado.assunto[:50]}", "ok")

    for situacao, quantas in sorted(cobranca.resumir(chamados).items()):
        if quantas:
            registro.log(f"{situacao:.<24} {quantas}", "detalhe")

    # Para ENVIAR, com a trava que vale (ler a conversa ao vivo de novo,
    # agora no instante anterior a escrita):
    #
    #     marcas = cobranca.assinaturas_de_bloqueio(cfg)
    #     if not sessao.conversa_contem(chamado.ticket_id, marcas):
    #         sessao.enviar_resposta(chamado.ticket_id,
    #                                cfg.resposta_para(chamado.gatilho.id))
    #         # e depois RELER: so contou se o texto aparecer na conversa
    return 0


def exemplo_finalizacao() -> int:
    """Le a busca inteira e rebaixa so as conversas que mudaram."""
    registro = RegistroConsole()
    sessao = _tomticket(registro)
    cache = finalizacao.CacheDeConversas(DADOS / "conversas")

    registro.etapa("Lendo a busca avancada salva")
    salva = sessao.busca_salva()
    registro.log(f"busca: {salva.get('nome') or 'sem nome'}")
    itens = finalizacao.busca_inteira(sessao, salva["condicao"], registro)

    registro.etapa("Atualizando as conversas")
    resultado = finalizacao.atualizar(sessao, itens, cache, registro)
    registro.log(resultado.resumo, "ok")
    registro.log(f"{len(cache.todos())} conversas em disco", "detalhe")

    # A partir daqui, a regra de "pode finalizar" e sua: cada registro do
    # cache tem `mensagens` em ordem, com sender_type "A" (atendente) ou "C".
    return 0


def exemplo_vigia() -> int:
    """Diz o que chegou de novo desde a ultima execucao."""
    registro = RegistroConsole()
    sessao = _tomticket(registro)
    memoria = DADOS / "vigia-vistos.json"

    import json
    try:
        vistos = set(json.loads(memoria.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        vistos = set()

    registro.etapa("Lendo a busca")
    salva = sessao.busca_salva()
    itens = finalizacao.busca_inteira(sessao, salva["condicao"], registro)

    if not vistos:
        # Primeira vez: linha de base. Adota como paisagem tudo que nao
        # acabou de chegar - senao o primeiro uso seria mil avisos.
        paisagem = vigia.linha_de_base(itens)
        registro.log(f"linha de base: {len(paisagem)} chamados ja existentes")
        vistos = paisagem

    novos = vigia.separar_novos(itens, vistos)
    registro.etapa(f"{len(novos)} chamado(s) novo(s)")
    for chamado in novos:
        registro.log(f"{chamado.protocolo}  {chamado.prioridade or '?'}  "
                     f"{chamado.assunto[:45]}", "aviso")
        registro.log(f"   falta: {chamado.falta}", "detalhe")

    memoria.parent.mkdir(parents=True, exist_ok=True)
    memoria.write_text(
        json.dumps(sorted(vistos | vigia.protocolos(itens))), encoding="utf-8")

    # Para RESPONDER o prazo (24h para Alta/Urgente, 48h para Normal/Baixa):
    #
    #     cfg = config.carregar()
    #     prazo = chamado.prazo                     # None = nao chute
    #     texto = cfg.gatilho_por_id(prazo).texto_original
    #     sessao.enviar_resposta(chamado.ticket_id, texto)
    #
    # Para VINCULAR ao atendente, pelo dashboard:
    #
    #     _, painel = _painel(registro)
    #     painel.vincular([(chamado.ticket_id,
    #                       painel.id_do_operador("CSM - SRT"))])
    return 0


COMANDOS = {"cobranca": exemplo_cobranca, "finalizacao": exemplo_finalizacao,
            "vigia": exemplo_vigia}


def main() -> int:
    escolha = sys.argv[1] if len(sys.argv) > 1 else ""
    if escolha not in COMANDOS:
        print(__doc__)
        return 0
    return COMANDOS[escolha]()


if __name__ == "__main__":
    sys.exit(main())
