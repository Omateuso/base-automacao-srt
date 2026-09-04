"""
O dashboard (IGEDES) por HTTP - sem navegador.

E de onde saem os chamados a cobrar. A leitura sempre foi chamada de API; o
navegador so guardava o cookie do login. Aqui o login e feito por HTTP
(`POST /api/auth/login`), o cookie fica guardado e o resto acontece dentro do
programa.

    painel = SessaoDashboard("https://helpdesk.exemplo.org", "sessao.json")
    painel.entrar("voce@exemplo.com", "senha")     # so na primeira vez
    alertas = painel.alertas()
    detalhe = painel.detalhe(ticket_id)

A sessao dura horas e fica em disco (fechada para outros usuarios da maquina).
Quando expira, o dashboard responde 401 e a base loga de novo sozinha, se
tiver credencial.

Endpoints usados - todos os que a propria tela do dashboard usa:

    POST /api/auth/login            {email, senha}
    GET  /api/tickets/sla-alertas   a lista de Alertas
    GET  /api/tickets/{id}          detalhe + mensagens
    POST /api/tickets/finish        {tickets, message}
    POST /api/tickets/link-operator {assignments:[{ticket_id, operator_id}]}
    GET  /api/meta/operators-db     atendentes (para achar o id pelo nome)
"""

from __future__ import annotations

import json
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from . import cofre
from .registro import AVISO, DETALHE, OK, Registro

NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
ESPERA = 180        # /sla-alertas e lento quando ha muita coisa

# Quantas vezes insistir quando a falha e do servidor, e quanto esperar entre
# elas. O dashboard as vezes devolve 500 com "canceling statement due to
# statement timeout": e o banco DELE cancelando a consulta por demora, nao um
# erro nosso - e costuma passar em segundos.
TENTATIVAS = 3
ESPERAS = (3, 8)


def _vale_repetir(status: int, corpo: str) -> str:
    """Diz por que vale insistir - ou "" quando nao vale.

    Repetir so faz sentido quando a falha e do outro lado e passageira. 401 e
    login (quem chama resolve), 4xx e pedido errado (repetir da o mesmo erro).
    """
    if status == 0:
        return "falha de rede"
    if status in (502, 503, 504):
        return f"servidor indisponivel ({status})"
    if status == 500 and ("statement timeout" in corpo
                          or "canceling statement" in corpo
                          or "timeout" in corpo.lower()):
        return "o banco do dashboard cancelou a consulta por demora"
    return ""



class ErroDashboard(RuntimeError):
    pass


class SemLogin(ErroDashboard):
    """A sessao caiu ou nunca existiu."""


def _chave(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


class SessaoDashboard:
    """Uma sessao do dashboard, por HTTP, com o cookie guardado em disco."""

    def __init__(self, base: str, arquivo: Path | str,
                 registro: Registro | None = None):
        self.base = str(base).rstrip("/")
        self.arquivo = Path(arquivo)
        self.registro = registro or Registro()
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": NAVEGADOR,
            "Accept": "application/json",
            "Referer": self.base + "/alertas",
            "Origin": self.base,
        })
        self.quando = ""
        self.carregar()

    # -- sessao em disco ---------------------------------------------------

    @property
    def tem_cookies(self) -> bool:
        return len(self.http.cookies) > 0

    def carregar(self) -> None:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if str(dados.get("base", "")).rstrip("/") != self.base:
            return                       # cookie de outro dashboard
        self.quando = str(dados.get("quando", ""))
        for cookie in dados.get("cookies", []):
            self._por(cookie)

    def _por(self, cookie: dict) -> None:
        nome, valor = cookie.get("name"), cookie.get("value")
        if not nome or valor is None:
            return
        self.http.cookies.set(
            nome, valor, domain=str(cookie.get("domain") or "").lstrip("."),
            path=cookie.get("path") or "/")

    def gravar(self) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        dados = {
            "base": self.base,
            "quando": datetime.now().isoformat(timespec="seconds"),
            "cookies": [{"name": c.name, "value": c.value,
                         "domain": c.domain, "path": c.path}
                        for c in self.http.cookies],
        }
        try:
            self.arquivo.write_text(
                json.dumps(dados, indent=2, ensure_ascii=False),
                encoding="utf-8")
            cofre.restringir_ao_usuario(self.arquivo)
        except OSError:
            pass

    def adotar(self, cookies: list[dict]) -> None:
        """Recebe cookies de fora (de um navegador ja logado, por exemplo)."""
        self.http.cookies.clear()
        for cookie in cookies or []:
            self._por(cookie)
        self.quando = datetime.now().isoformat(timespec="seconds")
        self.gravar()

    def esquecer(self) -> None:
        self.http.cookies.clear()
        try:
            self.arquivo.unlink()
        except OSError:
            pass

    # -- login -------------------------------------------------------------

    def entrar(self, email: str, senha: str) -> bool:
        """`POST /api/auth/login` - o mesmo caminho da tela. Sem navegador."""
        self.http.cookies.clear()
        status, corpo = self.chamar(
            "POST", "/auth/login", {"email": email, "senha": senha})
        if status != 200 or not self.tem_cookies:
            self.registro.log(
                f"login do dashboard recusado ({status}).", AVISO)
            return False
        self.gravar()
        self.registro.log("Entrei no dashboard, sem janela.", OK)
        return True

    def garantir(self, email: str = "", senha: str = "") -> bool:
        """Sessao de pe: usa o cookie guardado ou entra de novo."""
        if self.tem_cookies:
            status, _ = self.chamar("GET", "/tickets/kpis")
            if status == 200:
                return True
        return self.entrar(email, senha) if email and senha else False

    # -- chamadas ----------------------------------------------------------

    def _uma_chamada(self, metodo: str, caminho: str,
                     corpo: Any) -> tuple[int, str]:
        try:
            resposta = self.http.request(
                metodo, self.base + "/api" + caminho, timeout=ESPERA,
                json=corpo if corpo is not None else None)
        except requests.RequestException as erro:
            return 0, str(erro)
        return resposta.status_code, resposta.text

    def chamar(self, metodo: str, caminho: str,
               corpo: Any = None) -> tuple[int, str]:
        """(status, corpo cru), insistindo quando a falha e do servidor.

        Falha passageira do outro lado nao deve virar erro na sua cara: o
        dashboard as vezes cancela a consulta por demora e responde na
        tentativa seguinte.
        """
        for tentativa in range(TENTATIVAS):
            status, texto = self._uma_chamada(metodo, caminho, corpo)
            motivo = _vale_repetir(status, texto)
            if not motivo or tentativa == TENTATIVAS - 1:
                if motivo:
                    self.registro.log(
                        f"{caminho}: {motivo}. Desisti depois de "
                        f"{TENTATIVAS} tentativas.", AVISO)
                return status, texto
            espera = ESPERAS[min(tentativa, len(ESPERAS) - 1)]
            self.registro.log(
                f"{caminho}: {motivo}. Tentando de novo em {espera}s "
                f"({tentativa + 1}/{TENTATIVAS}).", AVISO)
            time.sleep(espera)
        return status, texto

    def _json(self, metodo: str, caminho: str, corpo: Any = None) -> Any:
        status, bruto = self.chamar(metodo, caminho, corpo)
        if status == 401:
            raise SemLogin("o dashboard respondeu 401 (sessao expirada).")
        if status == 0:
            raise ErroDashboard(f"falha de rede em {caminho}: {bruto[:200]}")
        if not (200 <= status < 300):
            raise ErroDashboard(f"{metodo} {caminho} devolveu {status}")
        try:
            return json.loads(bruto) if bruto else None
        except ValueError:
            raise ErroDashboard(f"{caminho} nao devolveu JSON")

    # -- o que a cobranca usa ----------------------------------------------

    def alertas(self) -> list[dict]:
        """A tela de Alertas inteira."""
        dados = self._json("GET", "/tickets/sla-alertas")
        if isinstance(dados, dict):
            dados = dados.get("data") or dados.get("tickets") or []
        return list(dados or [])

    def detalhe(self, ticket_id: str) -> dict:
        """Um chamado com as mensagens."""
        dados = self._json("GET", f"/tickets/{ticket_id}")
        if isinstance(dados, dict) and "data" in dados and "mensagens" not in dados:
            dados = dados["data"]
        return dados or {}

    def finalizar(self, ticket_ids: list[str], mensagem: str) -> Any:
        """Encerra chamados. Confira depois no TomTicket: pedir nao e ter feito."""
        return self._json("POST", "/tickets/finish",
                          {"tickets": list(ticket_ids), "message": mensagem})

    # -- vincular atendente -------------------------------------------------

    def operadores(self) -> list[dict]:
        dados = self._json("GET", "/meta/operators-db")
        return dados if isinstance(dados, list) else (dados.get("data") or [])

    def id_do_operador(self, nome: str) -> str:
        """O id pelo NOME - id fixo no codigo apodrece quando o cadastro muda."""
        alvo = _chave(nome)
        for operador in self.operadores():
            if _chave(operador.get("name")) == alvo:
                return str(operador.get("id") or "")
        return ""

    def vincular(self, pares: list[tuple[str, str]]) -> list[dict]:
        """[(ticket_id, operator_id)] -> results do dashboard."""
        if not pares:
            return []
        dados = self._json(
            "POST", "/tickets/link-operator",
            {"assignments": [{"ticket_id": t, "operator_id": o}
                             for t, o in pares]})
        return (dados or {}).get("results") or []


# ---------------------------------------------------------------------------
# Os mesmos filtros que a tela de Alertas aplica
# ---------------------------------------------------------------------------

PARAMS = {"projeto": "projeto", "dept": "departamento", "prio": "prioridade",
          "tab": "aba", "zona": "zona"}


def filtros_da_url(url: str) -> dict[str, str]:
    """Le os filtros que a tela de Alertas guarda na query string."""
    consulta = parse_qs(urlparse(url or "").query)
    lidos = {nosso: consulta[deles][0].strip()
             for deles, nosso in PARAMS.items()
             if consulta.get(deles) and consulta[deles][0].strip()}
    lidos.setdefault("aba", "sem_retorno")
    return lidos


def filtrar(alertas: list[dict], filtros: dict[str, str]) -> list[dict]:
    """Aplica os filtros da tela - as mesmas regras, copiadas do dashboard.

    A aba "sem_retorno" ("Precisam de retorno") e a que interessa a cobranca:
    sao os chamados em que o helpdesk ainda nao respondeu.
    """
    saida = []
    for alerta in alertas:
        if filtros.get("projeto") and \
                _chave(alerta.get("projeto")) != _chave(filtros["projeto"]):
            continue
        if filtros.get("departamento") and \
                _chave(alerta.get("departamento")) != _chave(filtros["departamento"]):
            continue
        prioridade = filtros.get("prioridade", "")
        if prioridade and _chave(prioridade) not in ("todos", "") and \
                _chave(alerta.get("prioridade")) != _chave(prioridade):
            continue
        aba = filtros.get("aba") or "sem_retorno"
        respondeu = bool(alerta.get("has_helpdesk_response"))
        if aba == "sem_retorno" and respondeu:
            continue
        if aba == "com_retorno" and not respondeu:
            continue
        saida.append(alerta)
    return saida
