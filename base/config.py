"""
Configuracao da base: textos, gatilhos e credenciais.

Duas coisas moram aqui, e por motivos diferentes.

Os TEXTOS e GATILHOS vem de `config.yaml`, ao lado deste arquivo, porque sao
regra de negocio que muda com frequencia e nao deve exigir mexer em codigo.
E ha uma trava embutida: o texto que a automacao ENVIA nunca pode conter a
assinatura de um gatilho, senao a propria resposta viraria motivo para
responder de novo - um laco que so aparece no chamado do cliente.

As CREDENCIAIS vem do ambiente (ou de um .env), nunca do codigo:

    DASHBOARD_URL=https://helpdesk.exemplo.org
    DASHBOARD_EMAIL=voce@exemplo.com
    DASHBOARD_SENHA=...            # pode estar cifrada (ver cofre.py)
    TOMTICKET_CONTA=...            # so para o login pelo navegador
    TOMTICKET_EMAIL=...
    TOMTICKET_SENHA=...

Senha cifrada pelo `cofre` e reconhecida sozinha: o valor comeca com "dpapi:"
e e decifrado na leitura. Sem as variaveis, quem chama passa as credenciais na
mao - a base nao exige arquivo nenhum.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import cofre

AQUI = Path(__file__).resolve().parent
ARQ_CONFIG = AQUI / "config.yaml"


@dataclass
class Gatilho:
    """Um texto padrao que, sendo a ultima mensagem, torna o chamado cobravel."""
    id: str
    horas_prazo: int
    assinatura: str          # trecho que identifica o texto sem depender de formatacao
    texto_original: str


@dataclass
class Config:
    """O que as regras precisam saber. Compativel com o classificador."""
    url_base: str = ""
    gatilhos: list[Gatilho] = field(default_factory=list)
    respostas: dict[str, str] = field(default_factory=dict)
    assinaturas_ja_escalado: list[str] = field(default_factory=list)
    verificar_prazo: bool = False
    mensagem_fechamento: str = ""
    projeto: str = ""
    departamento: str = ""
    atendente_alvo: str = "CSM - SRT"

    def resposta_para(self, gatilho_id: str) -> str:
        try:
            return self.respostas[gatilho_id]
        except KeyError:
            raise KeyError(
                f"config.yaml: falta a resposta para o gatilho '{gatilho_id}'")

    def gatilho_por_id(self, gatilho_id: str) -> Gatilho | None:
        for gatilho in self.gatilhos:
            if gatilho.id == gatilho_id:
                return gatilho
        return None


def _lista(valor) -> list[str]:
    if not valor:
        return []
    return [str(v) for v in ([valor] if isinstance(valor, str) else valor)]


def carregar(caminho: Path | str | None = None) -> Config:
    """Le o config.yaml e devolve a configuracao ja conferida."""
    arquivo = Path(caminho or ARQ_CONFIG)
    dados = yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}
    filtros = dados.get("filtros", {}) or {}
    regras = dados.get("regras", {}) or {}

    gatilhos = [
        Gatilho(id=str(g["id"]), horas_prazo=int(g.get("horas_prazo", 0)),
                assinatura=str(g["assinatura"]),
                texto_original=str(g.get("texto_original", "")).strip())
        for g in dados.get("gatilhos", [])
    ]
    if not gatilhos:
        raise ValueError(f"{arquivo}: nenhum gatilho definido.")

    cfg = Config(
        url_base=os.environ.get("DASHBOARD_URL", "").strip(),
        gatilhos=gatilhos,
        respostas={str(k): str(v) for k, v in (dados.get("respostas") or {}).items()},
        assinaturas_ja_escalado=_lista(dados.get("assinaturas_ja_escalado")),
        verificar_prazo=bool(regras.get("verificar_prazo", False)),
        mensagem_fechamento=str(regras.get("mensagem_fechamento", "") or "").strip(),
        projeto=str(filtros.get("projeto", "") or "").strip(),
        departamento=str(filtros.get("departamento", "") or "").strip(),
        atendente_alvo=str(regras.get("atendente_alvo", "CSM - SRT")).strip(),
    )

    problemas = conferir_travas(cfg)
    if problemas:
        raise ValueError(
            "config.yaml quebraria a trava anti-duplicidade:\n  - "
            + "\n  - ".join(problemas))
    return cfg


def conferir_travas(cfg: Config) -> list[str]:
    """As respostas nao podem disparar os gatilhos. Lista vazia = tudo bem.

    E a trava que impede o laco: se o texto enviado contivesse a assinatura de
    um gatilho, a proxima leitura veria a propria resposta como motivo para
    responder de novo.
    """
    from .cobranca import normalizar

    problemas = []
    for identificador, resposta in cfg.respostas.items():
        limpa = normalizar(resposta)
        for gatilho in cfg.gatilhos:
            if normalizar(gatilho.assinatura) in limpa:
                problemas.append(
                    f"a resposta '{identificador}' contem a assinatura do "
                    f"gatilho '{gatilho.id}'")
    return problemas


# ---------------------------------------------------------------------------
# Credenciais - do ambiente ou de um .env, nunca do codigo
# ---------------------------------------------------------------------------

def ler_env(arquivo: Path | str) -> dict[str, str]:
    """Le um .env simples (CHAVE=valor, # comenta)."""
    valores: dict[str, str] = {}
    try:
        linhas = Path(arquivo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return valores
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.strip().strip('"').strip("'")
        if valor:
            valores[chave.strip().upper()] = valor
    return valores


def credencial(chave: str, env: Path | str | None = None) -> str:
    """Uma credencial do ambiente ou do .env, decifrada se estiver cifrada."""
    valor = os.environ.get(chave) or (ler_env(env).get(chave) if env else "")
    if not valor:
        return ""
    try:
        return cofre.revelar(valor)
    except cofre.ErroDoCofre:
        return ""            # cifrada por outra conta/maquina: nao serve aqui


def credenciais_dashboard(env: Path | str | None = None) -> dict[str, str] | None:
    email = credencial("DASHBOARD_EMAIL", env)
    senha = credencial("DASHBOARD_SENHA", env)
    return {"email": email, "senha": senha} if email and senha else None


def credenciais_tomticket(env: Path | str | None = None) -> dict[str, str] | None:
    dados = {c: credencial(c, env) for c in
             ("TOMTICKET_CONTA", "TOMTICKET_EMAIL", "TOMTICKET_SENHA")}
    return dados if all(dados.values()) else None
