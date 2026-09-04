"""
Os chamados da manutencao dentro de um site - exemplo completo e rodavel.

    python -m pip install fastapi uvicorn
    set TOMTICKET_TOKEN=...
    set TOMTICKET_DEPARTAMENTO=MANUTENÇÃO - SRT
    python exemplo_web.py

    GET  http://127.0.0.1:8000/chamados          a lista, pronta para o mapa
    GET  http://127.0.0.1:8000/chamados/estado   quando foi a ultima leitura
    POST http://127.0.0.1:8000/chamados/sincronizar   forca uma leitura agora

O DESENHO, EM UMA FRASE
-----------------------
O token nunca sai do servidor: o navegador fala com o SEU backend, o backend
fala com o TomTicket.

    navegador  ->  seu backend  ->  api.tomticket.com
               <-   (JSON seu)  <-

Poe o token no front e ele vira publico - qualquer visitante abre o DevTools e
tem acesso a base de chamados inteira da empresa. Nao ha meio-termo aqui.

POR QUE NAO CHAMAR A API A CADA REQUISICAO
------------------------------------------
Se cada visita ao site virasse uma chamada ao TomTicket, dez pessoas com o mapa
aberto ja passariam do limite de 3 requisicoes por segundo, e a lista ficaria
lenta na hora em que mais gente esta usando. Uma tarefa de fundo mantem a foto
em memoria; as requisicoes do site leem a foto. O TomTicket ve UM cliente, nao
um por aba aberta.

ONDE ISTO ENCOSTA NO SEU PROJETO
--------------------------------
`coletor.abertos` e uma lista de `Chamado` (ver base/chamados.py). Para virar
rota, o que falta e o endereco de cada unidade - que o TomTicket nao tem. O
lugar de resolver isso e do seu lado: uma tabela `unidade -> lat/lon` no seu
banco, casada por `chamado.unidade`. Ha um esboco em `/chamados` abaixo.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime

from base.chamados import ABERTAS
from base.coletor import Coletor
from base.registro import RegistroConsole
from base.tomticket_api import ApiTomTicket, ErroApi, TokenRecusado

INTERVALO = int(os.environ.get("TOMTICKET_INTERVALO", "120"))


# ---------------------------------------------------------------------------
# 1. A coleta - vale para qualquer framework
# ---------------------------------------------------------------------------

def montar_coletor() -> Coletor:
    """Liga na API, resolve o departamento pelo NOME e devolve o coletor."""
    registro = RegistroConsole()

    token = os.environ.get("TOMTICKET_TOKEN", "")
    if not token:
        raise SystemExit(
            "Falta TOMTICKET_TOKEN no ambiente. Gere um token em\n"
            "Configuracoes > API no TomTicket e exporte antes de subir.")

    api = ApiTomTicket(token, registro=registro)
    if not api.conferir_token():
        raise SystemExit(
            "O TomTicket recusou o token. Confira o valor e se o plano da "
            "conta inclui a API.")

    nome = os.environ.get("TOMTICKET_DEPARTAMENTO", "MANUTENÇÃO - SRT")
    identificador = os.environ.get("TOMTICKET_DEPARTAMENTO_ID", "")
    if not identificador:
        identificador = api.id_do_departamento(nome)
        if not identificador:
            raise SystemExit(
                f"Nao achei o departamento {nome!r} na conta. Rode\n"
                f"    python -c \"from base.tomticket_api import *; "
                f"print(ApiTomTicket('SEU_TOKEN').departamentos())\"\n"
                f"e use TOMTICKET_DEPARTAMENTO_ID com o id certo.")
    registro.log(f"Departamento {nome} = {identificador}.")

    return Coletor(api, identificador, registro=registro)


def manter_em_dia(coletor: Coletor, segundos: int = INTERVALO) -> threading.Thread:
    """Sobe a tarefa de fundo que mantem a foto atual.

    Numa aplicacao com varios processos (gunicorn com workers, por exemplo),
    NAO suba uma destas por worker: seriam N clientes batendo no TomTicket e
    N fotos divergentes. Nesse caso, rode a coleta em um processo separado e
    guarde o resultado onde todos leem - Redis, ou uma tabela do seu banco.
    """
    def laco():
        coletor.rodar(segundos=segundos)

    thread = threading.Thread(target=laco, name="coleta-tomticket", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# 2. O site - FastAPI aqui, mas a ideia vale igual em Flask, Django ou Node
# ---------------------------------------------------------------------------

def montar_app(coletor: Coletor):
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError:
        raise SystemExit(
            "Falta o FastAPI para este exemplo:\n"
            "    python -m pip install fastapi uvicorn")

    app = FastAPI(title="Chamados da manutenção")

    # O endereco de cada unidade NAO vem do TomTicket. No seu projeto isto
    # aqui e uma consulta ao seu banco; no exemplo, um dicionario vazio.
    ENDERECOS: dict = {}

    def com_endereco(dados: dict) -> dict:
        local = ENDERECOS.get(dados.get("unidade"))
        if local:
            dados = dict(dados, latitude=local[0], longitude=local[1])
        else:
            dados = dict(dados, latitude=None, longitude=None,
                         sem_endereco=True)
        return dados

    @app.get("/chamados")
    def chamados(prioridade: str | None = None):
        """A lista aberta, pronta para virar paradas no mapa."""
        saida = [com_endereco(c) for c in coletor.como_json()]
        if prioridade:
            alvo = prioridade.strip().lower()
            saida = [c for c in saida
                     if (c.get("prioridade") or "").lower() == alvo]
        return {"total": len(saida), "chamados": saida}

    @app.get("/chamados/estado")
    def estado():
        """Quando foi a ultima leitura e o que ela mudou.

        Vale expor: se a coleta parar, o mapa continua desenhando a ultima foto
        e ninguem percebe. Com isto, o front mostra "atualizado ha 2 min" - e a
        pessoa ve quando esse numero para de andar.
        """
        ultima = coletor.ultima
        return {
            "ultima_leitura": coletor.ultimo_sync.isoformat(timespec="seconds")
                              if coletor.ultimo_sync else None,
            "chamados_abertos": len(coletor.chamados),
            "resumo": ultima.resumo if ultima else "ainda não sincronizou",
            "erro": ultima.erro if ultima else "",
            "situacoes_consideradas_abertas": list(ABERTAS),
        }

    @app.post("/chamados/sincronizar")
    def sincronizar(completa: bool = False):
        resultado = coletor.sincronizar(completa=completa)
        if resultado.erro:
            raise HTTPException(status_code=502, detail=resultado.erro)
        return {"resumo": resultado.resumo, "novos": resultado.novos,
                "fechados": resultado.fechados}

    return app


def main() -> int:
    try:
        coletor = montar_coletor()
    except (ErroApi, TokenRecusado) as erro:
        print(f"[!] {erro}")
        return 1

    print("Primeira leitura...")
    print("  ", coletor.sincronizar().resumo)
    manter_em_dia(coletor)

    app = montar_app(coletor)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
