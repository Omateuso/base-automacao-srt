"""
Por onde a base fala com quem a executa.

A base nao imprime nada por conta propria. Quem chama decide o que fazer com
cada linha - jogar no terminal, num log, numa interface, num Slack. Isso e o
que permite o mesmo codigo rodar dentro de uma janela, de um cron e de um
teste sem mudar uma linha.

    base = Registro()               # engole tudo (padrao)
    base = RegistroConsole()        # imprime no terminal

Quem quiser integrar em outro programa implementa os mesmos metodos:

    class MeuRegistro:
        def log(self, mensagem, nivel="info"): ...
        def etapa(self, titulo): ...
        def progresso(self, feito, total, texto=""): ...
        def checar(self): ...       # levante Interrompido para parar no meio
"""

from __future__ import annotations

INFO = "info"
OK = "ok"
AVISO = "aviso"
ERRO = "erro"
DETALHE = "detalhe"


class Interrompido(RuntimeError):
    """Erguido por `checar()` quando quem chama pediu para parar."""


class Registro:
    """Canal padrao: engole tudo. Serve de base para as implementacoes reais."""

    def log(self, mensagem: str, nivel: str = INFO) -> None:
        """Uma linha de acompanhamento."""

    def etapa(self, titulo: str) -> None:
        """Comeco de uma fase (ex.: 'Lendo os Alertas')."""
        self.log(titulo)

    def progresso(self, feito: int, total: int, texto: str = "") -> None:
        """Avanco dentro da fase atual. total=0 significa indeterminado."""

    def checar(self) -> None:
        """Ponto de parada. Levante `Interrompido` aqui para abortar."""


class RegistroConsole(Registro):
    """Imprime no terminal, com uma marca por nivel."""

    MARCAS = {INFO: "  ", OK: "[OK] ", AVISO: "[!] ", ERRO: "[X] ",
              DETALHE: "   "}

    def log(self, mensagem: str, nivel: str = INFO) -> None:
        print(f"{self.MARCAS.get(nivel, '  ')}{mensagem}")

    def etapa(self, titulo: str) -> None:
        print()
        print(titulo)
        print("-" * min(len(titulo), 70))

    def progresso(self, feito: int, total: int, texto: str = "") -> None:
        if total:
            print(f"\r   [{feito}/{total}] {texto}", end="", flush=True)
            if feito >= total:
                print()
