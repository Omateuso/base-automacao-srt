"""
Cofre: guarda segredo em disco sem deixa-lo legivel.

O QUE ISTO RESOLVE E O QUE NAO RESOLVE - vale ler antes de confiar.

Criptografar uma senha que o proprio programa precisa decifrar sozinho nao a
torna secreta: a chave tem que morar em algum lugar que o programa alcance, e
quem le o arquivo normalmente alcanca a chave tambem. Isso e ofuscacao, e
ofuscacao so engana quem passa os olhos.

O que muda o jogo aqui e a DPAPI do Windows: ela cifra usando a credencial da
SUA conta de usuario, e a chave nunca aparece no nosso codigo. O texto cifrado
so volta a ser senha para o mesmo usuario, na mesma maquina. Entao:

  * um .env copiado para outro computador, mandado por e-mail, subido num
    repositorio ou tirado de um backup vira lixo ilegivel - que e exatamente o
    vazamento que preocupa;
  * quem esta logado como voce nesta maquina continua conseguindo decifrar,
    porque o programa tambem consegue. Contra esse, o que protege e a senha do
    Windows, nao o cofre.

E o mesmo mecanismo que o Chrome usa para os cookies dele, nesta mesma pasta de
perfil.

PARA PRODUCAO, EM OUTRO PROJETO: nao guarde a senha de uma pessoa. Use uma
conta de servico com token revogavel, injete o segredo pelo ambiente (a esteira
de deploy, um cofre de segredos) ou use o gerenciador de credenciais do
sistema. O cofre daqui e para o caso desta maquina: um programa de mesa que
precisa entrar sozinho na conta de quem o usa.

Fora do Windows - ou se a DPAPI falhar - `proteger` devolve o texto como veio,
e quem chama continua funcionando. `esta_protegido` existe para a tela poder
dizer a verdade sobre o que ha no arquivo.
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

PREFIXO = "dpapi:"
_DESCRICAO = "PHDS"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _entrada(dados: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(dados, len(dados))
    return _Blob(len(dados), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _saida(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _crypt32():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL("crypt32")
    except OSError:
        return None


def disponivel() -> bool:
    """A DPAPI pode ser usada nesta maquina?"""
    return _crypt32() is not None


def esta_protegido(valor: str) -> bool:
    return str(valor or "").startswith(PREFIXO)


def proteger(segredo: str) -> str:
    """Devolve "dpapi:<base64>" - ou o proprio texto, se nao der para cifrar."""
    if not segredo or esta_protegido(segredo):
        return segredo
    crypt32 = _crypt32()
    if crypt32 is None:
        return segredo

    dentro = _entrada(segredo.encode("utf-8"))
    fora = _Blob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(dentro), _DESCRICAO, None, None, None, 0,
        ctypes.byref(fora))
    if not ok:
        return segredo
    try:
        cifrado = _saida(fora)
    finally:
        ctypes.windll.kernel32.LocalFree(fora.pbData)
    return PREFIXO + base64.b64encode(cifrado).decode("ascii")


def revelar(valor: str) -> str:
    """O caminho de volta. Texto sem o prefixo passa direto, como estava."""
    if not esta_protegido(valor):
        return valor
    crypt32 = _crypt32()
    if crypt32 is None:
        raise ErroDoCofre(
            "este segredo foi protegido pela DPAPI do Windows e so pode ser "
            "lido no mesmo Windows, pela mesma conta de usuario.")

    try:
        cifrado = base64.b64decode(valor[len(PREFIXO):].encode("ascii"))
    except (ValueError, UnicodeEncodeError) as erro:
        raise ErroDoCofre("o segredo guardado esta corrompido.") from erro

    dentro = _entrada(cifrado)
    fora = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(dentro), None, None, None, None, 0, ctypes.byref(fora))
    if not ok:
        raise ErroDoCofre(
            "nao consegui decifrar o segredo. Ele foi guardado por outra conta "
            "de usuario ou em outra maquina - grave a senha de novo nesta.")
    try:
        return _saida(fora).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(fora.pbData)


class ErroDoCofre(RuntimeError):
    pass


def restringir_ao_usuario(arquivo) -> bool:
    """Tira a heranca de permissoes e deixa so voce ler o arquivo.

    Os dados moram em Documentos, onde a heranca costuma dar leitura tambem
    aos Administradores da maquina. Para um arquivo que guarda cookie de
    sessao ou senha cifrada, o certo e o dono e mais ninguem. Melhor esforco:
    se o icacls falhar, o programa segue - a protecao principal continua sendo
    a cifra e o tempo de vida curto da sessao.
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        try:
            os.chmod(arquivo, 0o600)
            return True
        except OSError:
            return False

    usuario = os.environ.get("USERNAME") or ""
    if not usuario:
        return False
    try:
        subprocess.run(
            ["icacls", str(arquivo), "/inheritance:r",
             "/grant:r", f"{usuario}:F"],
            capture_output=True, timeout=20, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def proteger_no_arquivo(arquivo, chaves: tuple[str, ...]) -> list[str]:
    """Cifra, no proprio .env, os valores das chaves indicadas.

    Reescreve so as linhas dessas chaves; comentarios, ordem e o resto do
    arquivo ficam como estavam. Devolve os nomes das chaves que foram
    cifradas agora - lista vazia significa que ja estava tudo protegido.
    """
    from pathlib import Path as _Path

    arquivo = _Path(arquivo)
    try:
        linhas = arquivo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    mexidas = []
    saida = []
    for linha in linhas:
        crua = linha.strip()
        if crua and not crua.startswith("#") and "=" in crua:
            chave, _, valor = crua.partition("=")
            chave = chave.strip().upper()
            valor = valor.strip().strip('"').strip("'")
            if chave in chaves and valor and not esta_protegido(valor):
                cifrado = proteger(valor)
                if cifrado != valor:
                    saida.append(f"{chave}={cifrado}")
                    mexidas.append(chave)
                    continue
        saida.append(linha)

    if mexidas:
        arquivo.write_text("\n".join(saida) + "\n", encoding="utf-8")
    return mexidas
