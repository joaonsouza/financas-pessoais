"""Gemini (Google) como segundo provedor de IA para as funções de texto (categorias).

Chave no keyring, cliente por HTTP (`generateContent`) com saída estruturada. A resposta tem o mesmo formato que
o `chamar_parse` da Anthropic devolve (`stop_reason` e `parsed_output`), então o resto do app não muda.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import keyring
from keyring.errors import PasswordDeleteError
from pydantic import ValidationError

from app.importers.base import LeitorErro

KEYRING_SERVICO = "app-financas"
KEYRING_USUARIO_GEMINI = "gemini"
MODELO_GEMINI_PADRAO = "gemini-3.5-flash"
# se o modelo escolhido estiver sobrecarregado (503), tenta estes em ordem
MODELOS_RESERVA = ("gemini-3.1-flash-lite", "gemini-3.5-flash")
ERROS_DE_SOBRECARGA = (500, 502, 503, 504)
URL_BASE = "https://generativelanguage.googleapis.com/v1beta"


# ---------- chave (Windows Credential Manager via keyring) ----------

def obter_chave_gemini() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICO, KEYRING_USUARIO_GEMINI) or None
    except Exception:
        return None


def salvar_chave_gemini(chave: str) -> None:
    chave = chave.strip()
    if not chave:
        raise ValueError("informe a chave da API do Gemini")
    try:
        keyring.set_password(KEYRING_SERVICO, KEYRING_USUARIO_GEMINI, chave)
    except Exception as exc:
        raise ValueError(f"não foi possível guardar a chave no cofre do sistema: {exc}") from exc


def remover_chave_gemini() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICO, KEYRING_USUARIO_GEMINI)
    except PasswordDeleteError:
        pass
    except Exception as exc:
        raise ValueError(f"não foi possível remover a chave do cofre do sistema: {exc}") from exc


def ultimos4_gemini() -> str | None:
    chave = obter_chave_gemini()
    return chave[-4:] if chave else None


# ---------- schema: pydantic (JSON Schema) -> subconjunto OpenAPI que o Gemini aceita ----------

def _para_schema_gemini(no, defs: dict):
    """Resolve $ref, vira anyOf [X, null] em X + nullable, tira enum numérico (o Gemini só aceita enum de
    texto) e descarta o que a API não conhece (title, default, additionalProperties)."""
    if isinstance(no, list):
        return [_para_schema_gemini(x, defs) for x in no]
    if not isinstance(no, dict):
        return no
    if "$ref" in no:
        return _para_schema_gemini(defs[no["$ref"].split("/")[-1]], defs)
    if "anyOf" in no:
        opcoes = [o for o in no["anyOf"] if o.get("type") != "null"]
        if len(opcoes) == 1:
            resultado = dict(_para_schema_gemini(opcoes[0], defs))
            if len(opcoes) != len(no["anyOf"]):
                resultado["nullable"] = True
            return resultado
    saida = {}
    for chave, valor in no.items():
        if chave in ("title", "default", "additionalProperties", "$defs", "anyOf"):
            continue
        if chave == "enum" and any(not isinstance(v, str) for v in valor):
            continue
        saida[chave] = _para_schema_gemini(valor, defs)
    if "enum" in no and "enum" not in saida and no.get("type") in (None, "integer"):
        saida["type"] = "integer"
    return saida


def schema_para_gemini(modelo) -> dict:
    bruto = modelo.model_json_schema()
    return _para_schema_gemini(bruto, bruto.get("$defs", {}))


# ---------- cliente ----------

def _texto_das_mensagens(mensagens: list[dict]) -> str:
    partes = []
    for m in mensagens:
        conteudo = m["content"]
        if isinstance(conteudo, str):
            partes.append(conteudo)
            continue
        for bloco in conteudo:
            if bloco.get("type") != "text":
                raise LeitorErro("Esta função envia arquivos e só funciona com a IA da Anthropic.")
            partes.append(bloco["text"])
    return "\n\n".join(partes)


def _erro_http(exc: httpx.HTTPStatusError) -> LeitorErro:
    status = exc.response.status_code
    if status in (400, 401, 403):
        return LeitorErro("O Gemini recusou a chamada. Confira a chave e o modelo em Configurações > IA.")
    if status == 402:
        return LeitorErro("Os créditos pré-pagos do Gemini acabaram. Recarregue em aistudio.google.com (Billing) "
                          "ou use a Anthropic em Configurações > IA.")
    if status == 404:
        return LeitorErro("O Gemini não achou esse modelo. Confira o nome do modelo em Configurações > IA.")
    if status == 429:
        return LeitorErro("O Gemini atingiu o limite de uso. Aguarde um pouco e tente de novo.")
    return LeitorErro(f"O Gemini respondeu com erro {status}. Tente de novo em instantes.")


class ClienteGemini:
    def __init__(self, chave: str, http: httpx.Client | None = None):
        self.chave = chave
        self.http = http or httpx.Client(timeout=120)

    def _enviar(self, modelo: str, corpo: dict) -> httpx.Response:
        """Uma tentativa por modelo: o escolhido e depois os de reserva, só quando o erro é de sobrecarga."""
        ultimo: httpx.HTTPStatusError | None = None
        for candidato in dict.fromkeys((modelo, *MODELOS_RESERVA)):
            try:
                resposta = self.http.post(f"{URL_BASE}/models/{candidato}:generateContent", json=corpo,
                                          headers={"x-goog-api-key": self.chave})
                resposta.raise_for_status()
                return resposta
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in ERROS_DE_SOBRECARGA:
                    raise _erro_http(exc) from exc
                ultimo = exc
            except httpx.HTTPError as exc:
                raise LeitorErro("Sem conexão com o Gemini. Verifique a internet e tente de novo.") from exc
        raise LeitorErro("O Gemini está sobrecarregado agora (alta demanda). Tente de novo em alguns minutos.") from ultimo

    def parse(self, *, modelo: str, saida, mensagens: list[dict], max_tokens: int = 16000):
        corpo = {
            "contents": [{"role": "user", "parts": [{"text": _texto_das_mensagens(mensagens)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema_para_gemini(saida),
                "maxOutputTokens": max_tokens,
                "temperature": 0,
            },
        }
        resposta = self._enviar(modelo, corpo)
        dados = resposta.json()
        candidato = (dados.get("candidates") or [{}])[0]
        bloqueado = bool(dados.get("promptFeedback", {}).get("blockReason")) or candidato.get(
            "finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION")
        texto = "".join(p.get("text", "") for p in candidato.get("content", {}).get("parts", []))
        try:
            parsed = None if bloqueado or not texto else saida.model_validate_json(texto)
        except ValidationError:
            parsed = None
        return SimpleNamespace(stop_reason="refusal" if bloqueado else "end_turn", parsed_output=parsed)
