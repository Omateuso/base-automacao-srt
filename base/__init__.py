"""
base-automatizacao - as automacoes de chamados, sem interface.

Copia isolada do que o PHDS usa para trabalhar: extrair cobrancas do dashboard,
extrair chamados abertos e conversas do TomTicket, e vigiar o que chega. Nao ha
janela, nao ha PySide6, e nada aqui imprime sozinho - quem chama recebe dados e
decide o que fazer com eles.

    from base import chamados, coletor, tomticket_api      # para um SITE
    from base import cobranca, finalizacao, vigia           # as automacoes

DUAS PORTAS PARA O TOMTICKET, e a escolha importa:

    tomticket_api   a API OFICIAL (Bearer token, contrato versionado). E o que
                    um servidor deve usar. Sem sessao para manter, sem
                    navegador, sem endpoint interno mudando sem aviso.
    tomticket       o CONSOLE, com o cookie de um login humano. Adequado a um
                    programa de mesa que roda na conta de quem o usa; ruim num
                    servidor. Foi por aqui que tudo isto comecou, e continua
                    aqui porque funciona sem depender do plano da conta.

As duas desaguam no mesmo formato (`base.chamados.Chamado`), entao da para
comecar por uma e trocar para a outra sem reescrever quem consome.

As tres frentes, em uma linha cada:

    COBRANCA     le os Alertas do dashboard, classifica pela ultima mensagem e
                 pelo prazo, e escreve a cobranca no TomTicket quando devida.
    FINALIZACAO  cruza o dashboard com o TomTicket para saber quem ainda esta
                 aberto e o que ja pode ser fechado.
    VIGIA        compara a busca com o que ja foi visto e diz o que e chamado
                 novo, qual prazo responder e o que falta fazer nele.

E uma peca que nao e frente nenhuma, e e a mais util fora daqui:

    COLETOR      mantem uma foto sempre atual dos chamados abertos de um
                 departamento, lendo so o que mudou. E o que um site consome.

O que NAO veio junto, e por que:

  * a interface (janela, tabelas, notificacoes) - e do PHDS, nao da automacao;
  * o classificador de "pode finalizar" - sao 700 linhas de regras calibradas
    com os chamados de uma operacao especifica; a base entrega as conversas e
    voce aplica a sua regra;
  * os caminhos Selenium/Playwright de leitura - tudo aqui e HTTP. O navegador
    so aparece no login opcional do TomTicket, que nao expoe um login simples.

Dependencias: `requests` e `PyYAML`. `playwright` so se voce usar o login pelo
navegador.
"""

from . import (chamados, cobranca, cofre, coletor, config, dashboard,
               finalizacao, registro, tomticket, tomticket_api, vigia)
from .chamados import Chamado, de_api, de_console, do_dashboard
from .coletor import Coletor
from .config import Config, Gatilho, carregar
from .dashboard import SessaoDashboard, filtrar, filtros_da_url
from .registro import Registro, RegistroConsole
from .tomticket import SessaoTomTicket
from .tomticket_api import ApiTomTicket

__all__ = [
    "chamados", "cobranca", "cofre", "coletor", "config", "dashboard",
    "finalizacao", "registro", "tomticket", "tomticket_api", "vigia",
    "Chamado", "Coletor", "Config", "Gatilho", "carregar",
    "ApiTomTicket", "SessaoDashboard", "SessaoTomTicket",
    "Registro", "RegistroConsole",
    "de_api", "de_console", "do_dashboard", "filtrar", "filtros_da_url",
]

__version__ = "2.0"
