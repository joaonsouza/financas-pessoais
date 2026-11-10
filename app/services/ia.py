"""Acesso à Claude API: chave no keyring, modelo configurável, cliente injetável e tradução de erros."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import anthropic
import keyring
from keyring.errors import PasswordDeleteError
from sqlalchemy.orm import Session

from app.core.demo import modo_demo
from app.importers.base import LeitorErro
from app.models import get_config
from app.services.gemini import MODELO_GEMINI_PADRAO, ClienteGemini, obter_chave_gemini

KEYRING_SERVICO = "app-financas"
KEYRING_USUARIO = "anthropic"
MODELO_PADRAO = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]


# ---------- chave (Windows Credential Manager via keyring) ----------

def obter_chave() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICO, KEYRING_USUARIO) or None
    except Exception:  # sem backend de keyring disponível: cai para o ambiente/perfil do SDK
        return None


def salvar_chave(chave: str) -> None:
    chave = chave.strip()
    if not chave:
        raise ValueError("informe a chave da API")
    try:
        keyring.set_password(KEYRING_SERVICO, KEYRING_USUARIO, chave)
    except Exception as exc:
        raise ValueError(f"não foi possível guardar a chave no cofre do sistema: {exc}") from exc


def remover_chave() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICO, KEYRING_USUARIO)
    except PasswordDeleteError:
        pass  # já não existia
    except Exception as exc:
        raise ValueError(f"não foi possível remover a chave do cofre do sistema: {exc}") from exc


def ultimos4_da_chave() -> str | None:
    """Os 4 últimos caracteres da chave, para a tela confirmar qual está configurada (nunca a chave inteira)."""
    chave = obter_chave()
    return chave[-4:] if chave else None


# ---------- modelo e cliente ----------

def obter_modelo(s: Session) -> str:
    return str(get_config(s, "modelo_ia", MODELO_PADRAO) or MODELO_PADRAO)


SEM_IA_NA_DEMO = "Na demonstração as funções de IA ficam desligadas."


def criar_cliente() -> anthropic.Anthropic:
    """Cliente com a chave do keyring; sem chave guardada, o SDK resolve pelo ambiente/perfil padrão."""
    if modo_demo():  # a chave fica no cofre do Windows, a mesma do app real
        raise LeitorErro(SEM_IA_NA_DEMO)
    chave = obter_chave()
    return anthropic.Anthropic(api_key=chave) if chave else anthropic.Anthropic()


@contextmanager
def erros_da_api() -> Iterator[None]:
    """Traduz as falhas da API em `LeitorErro` com mensagem em português."""
    try:
        yield
    except anthropic.RateLimitError as exc:
        raise LeitorErro("A API da Anthropic atingiu o limite de uso. Aguarde um pouco e tente de novo.") from exc
    except anthropic.APIStatusError as exc:
        raise LeitorErro(
            f"A API da Anthropic recusou a chamada (erro {exc.status_code}). "
            "Confira a chave e o modelo em Configurações > IA."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise LeitorErro("Sem conexão com a API da Anthropic. Verifique a internet e tente de novo.") from exc


PROVEDORES = ("anthropic", "gemini")


def obter_provedor(s: Session) -> str:
    """Provedor das funções de texto (categorias). Sem escolha, usa o que tiver chave: Anthropic primeiro."""
    escolhido = get_config(s, "provedor_ia", None)
    if escolhido in PROVEDORES:
        return escolhido
    if obter_chave() is None and obter_chave_gemini() is not None:
        return "gemini"
    return "anthropic"


def obter_modelo_gemini(s: Session) -> str:
    return str(get_config(s, "modelo_gemini", MODELO_GEMINI_PADRAO) or MODELO_GEMINI_PADRAO)


def cliente_e_modelo_de_texto(s: Session) -> tuple[object, str]:
    """Cliente e modelo do provedor escolhido para as funções de texto. Sem chave, `LeitorErro` com o que fazer."""
    if modo_demo():
        raise LeitorErro(SEM_IA_NA_DEMO)
    if obter_provedor(s) == "gemini":
        chave = obter_chave_gemini()
        if chave is None:
            raise LeitorErro("Falta a chave da API do Gemini. Cadastre em Configurações > IA e tente de novo.")
        return ClienteGemini(chave), obter_modelo_gemini(s)
    if obter_chave() is None and not os.environ.get("ANTHROPIC_API_KEY"):
        raise LeitorErro("Falta a chave da API da Anthropic. Cadastre em Configurações > IA e tente de novo.")
    return criar_cliente(), obter_modelo(s)


def chamar_parse(cliente, *, modelo: str, esforco: str, saida, mensagens: list[dict], max_tokens: int = 16000):
    """Uma chamada de saída estruturada. Devolve a resposta da API (`parsed_output` e `stop_reason`)."""
    if isinstance(cliente, ClienteGemini):
        return cliente.parse(modelo=modelo, saida=saida, mensagens=mensagens, max_tokens=max_tokens)
    with erros_da_api():
        return cliente.beta.messages.parse(
            model=modelo,
            max_tokens=max_tokens,
            betas=BETAS,
            fallbacks="default",
            output_config={"effort": esforco},
            output_format=saida,
            messages=mensagens,
        )
