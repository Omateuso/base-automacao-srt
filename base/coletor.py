"""
Uma foto sempre atual dos chamados abertos de um departamento.

E a peca que um site precisa: alguem tem que perguntar ao TomTicket de tempos
em tempos e manter uma lista que o site le. O Coletor faz so isso, e faz de um
jeito que aguenta rodar o dia inteiro:

    coletor = Coletor(api, department_id="143645")
    coletor.sincronizar()          # a primeira: le tudo que esta aberto
    ...
    coletor.sincronizar()          # as seguintes: so o que mudou

A ARMADILHA QUE ISTO EVITA
--------------------------
A leitura incremental natural seria "me de os chamados ABERTOS que mudaram
desde a ultima vez". Ela parece certa e esta errada: quando um chamado e
finalizado, ele deixa de ser aberto e portanto NAO volta na resposta - o site
nunca fica sabendo, e a equipe segue vendo uma parada de rota que ja foi
resolvida. Foi exatamente esse tipo de lista que apareceu no PHDS, com 80
chamados finalizados havia semanas ainda na tela.

Entao a leitura incremental daqui NAO filtra situacao: pede tudo que mudou e
decide o que fazer com cada um.

    mudou e continua aberto  ->  entra ou atualiza
    mudou e fechou           ->  SAI da lista

A JANELA DE SEGURANCA
---------------------
Cada sincronizacao pergunta a partir de um instante um pouco anterior ao fim da
anterior (`FOLGA`). Relogios de servidores diferentes nao batem no milissegundo,
e uma escrita que caia exatamente na fronteira ficaria invisivel para sempre.
Reler alguns chamados a mais custa quase nada; perder um custa uma visita.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .chamados import Chamado
from .registro import AVISO, DETALHE, OK, Registro

# Quanto voltamos no tempo alem do fim da leitura anterior.
FOLGA = timedelta(minutes=5)


@dataclass
class Sincronizacao:
    """O que uma passada mudou."""
    quando: datetime = field(default_factory=datetime.now)
    completa: bool = False
    lidos: int = 0
    novos: list = field(default_factory=list)
    atualizados: list = field(default_factory=list)
    fechados: list = field(default_factory=list)
    total: int = 0
    segundos: float = 0.0
    erro: str = ""

    @property
    def mudou(self) -> bool:
        return bool(self.novos or self.atualizados or self.fechados)

    @property
    def resumo(self) -> str:
        if self.erro:
            return f"falhou: {self.erro}"
        modo = "leitura completa" if self.completa else "só o que mudou"
        partes = [f"{self.total} abertos"]
        if self.novos:
            partes.append(f"{len(self.novos)} novos")
        if self.atualizados:
            partes.append(f"{len(self.atualizados)} atualizados")
        if self.fechados:
            partes.append(f"{len(self.fechados)} fechados")
        return f"{' · '.join(partes)} ({modo}, {self.segundos:.1f}s)"


class Coletor:
    """Mantem os chamados abertos de um departamento em dia.

    `fonte` precisa oferecer dois metodos:

        abertos_do_departamento(department_id)  -> list[Chamado]
        chamados(**filtros)                     -> iteravel de Chamado

    `ApiTomTicket` atende os dois. Qualquer outra coisa que atenda tambem
    serve - e o que permite testar isto sem rede.
    """

    def __init__(self, fonte, department_id: str,
                 registro: Registro | None = None, folga: timedelta = FOLGA):
        self.fonte = fonte
        self.department_id = str(department_id)
        self.registro = registro or Registro()
        self.folga = folga
        self.chamados: dict = {}          # protocolo -> Chamado
        self.ultimo_sync: datetime | None = None
        self.ultima: Sincronizacao | None = None

    # -- leitura ------------------------------------------------------------

    @property
    def abertos(self) -> list:
        """Os chamados abertos, do mais novo para o mais antigo."""
        return sorted(self.chamados.values(),
                      key=lambda c: (c.criado_em or datetime.min), reverse=True)

    def como_json(self) -> list:
        """Pronto para o site consumir."""
        return [c.como_dicionario() for c in self.abertos]

    # -- sincronizacao ------------------------------------------------------

    def sincronizar(self, completa: bool = False) -> Sincronizacao:
        """Poe a lista em dia. A primeira le tudo; as seguintes, so o que mudou."""
        inicio = datetime.now()
        primeira = completa or self.ultimo_sync is None
        resultado = Sincronizacao(completa=primeira)

        try:
            if primeira:
                self._ler_tudo(resultado)
            else:
                self._ler_o_que_mudou(resultado)
        except Exception as erro:                    # noqa: BLE001
            # Uma falha de rede nao pode zerar a lista que o site esta usando.
            # A foto anterior continua de pe e a proxima passada tenta de novo.
            resultado.erro = str(erro)
            resultado.total = len(self.chamados)
            resultado.segundos = (datetime.now() - inicio).total_seconds()
            self.registro.log(
                f"sincronizacao falhou ({erro}); mantive a lista anterior com "
                f"{len(self.chamados)} chamado(s).", AVISO)
            self.ultima = resultado
            return resultado

        # So avanca o relogio quando a leitura deu certo - senao a proxima
        # passada pularia justamente a janela que falhou.
        self.ultimo_sync = inicio
        resultado.total = len(self.chamados)
        resultado.segundos = (datetime.now() - inicio).total_seconds()
        self.ultima = resultado
        self.registro.log(resultado.resumo, OK if resultado.mudou else DETALHE)
        return resultado

    def _ler_tudo(self, resultado: Sincronizacao) -> None:
        lidos = self.fonte.abertos_do_departamento(self.department_id)
        resultado.lidos = len(lidos)
        novos = {c.protocolo: c for c in lidos if c.protocolo}
        resultado.novos = [p for p in novos if p not in self.chamados]
        resultado.fechados = [p for p in self.chamados if p not in novos]
        self.chamados = novos

    def _ler_o_que_mudou(self, resultado: Sincronizacao) -> None:
        desde = (self.ultimo_sync or datetime.now()) - self.folga

        # SEM filtro de situacao, de proposito: e assim que ficamos sabendo dos
        # que FECHARAM. Pedindo so os abertos, um chamado finalizado sumiria da
        # resposta e ficaria na lista para sempre.
        for chamado in self.fonte.chamados(
                department_id=self.department_id,
                last_update_ge=self._formatar(desde)):
            resultado.lidos += 1
            protocolo = chamado.protocolo
            if not protocolo:
                continue

            if not chamado.aberto:
                if self.chamados.pop(protocolo, None) is not None:
                    resultado.fechados.append(protocolo)
                continue

            if protocolo in self.chamados:
                resultado.atualizados.append(protocolo)
            else:
                resultado.novos.append(protocolo)
            self.chamados[protocolo] = chamado

    def _formatar(self, momento: datetime) -> str:
        from .tomticket_api import formatar_data, limitar_janela
        return formatar_data(limitar_janela(momento))

    # -- uso -----------------------------------------------------------------

    def rodar(self, segundos: int = 120, vezes: int | None = None,
              ao_mudar=None) -> None:
        """Sincroniza de tempos em tempos, para sempre (ou `vezes` vezes).

        `ao_mudar(sincronizacao, coletor)` e chamado so quando algo mudou - e
        onde o site avisa o front, grava no banco ou recalcula a rota.

        Isto e o laco mais simples que funciona. Num servico de verdade, o
        agendador do seu framework (APScheduler, Celery beat, um cron) faz o
        mesmo papel chamando `sincronizar()`.
        """
        import time

        contador = 0
        while vezes is None or contador < vezes:
            resultado = self.sincronizar()
            if resultado.mudou and ao_mudar is not None:
                ao_mudar(resultado, self)
            contador += 1
            if vezes is not None and contador >= vezes:
                break
            time.sleep(max(1, segundos))
