"""Gera fixtures anonimizadas a partir das amostras reais (que ficam fora do git).

Uso (na raiz do projeto):
    uv run python scripts/anonimizar_amostra.py ofx  <entrada.ofx> <saida.ofx>
    uv run python scripts/anonimizar_amostra.py pdf-mp      <entrada.pdf> <saida.json>
    uv run python scripts/anonimizar_amostra.py pdf-itau-extrato <entrada.pdf> <saida.json>
    uv run python scripts/anonimizar_amostra.py pdf-itau-fatura  <entrada.pdf> <saida.json>

Troca nomes, CPF, números de conta/cartão e estabelecimentos por fictícios e escala os valores
por um fator secreto, lido de FINANCAS_FATOR_ANONIMIZACAO (somas e saldos continuam consistentes).
Nada pessoal deve sobrar: confira a saída.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

FATOR_DEN = 100
# O fator fica fora do repositório: com ele, quem lê as fixtures recuperaria os valores reais.
# Defina FINANCAS_FATOR_ANONIMIZACAO (inteiro de 1 a 99, em centésimos) antes de rodar.
_VAR_FATOR = "FINANCAS_FATOR_ANONIMIZACAO"


def _fator_num() -> int:
    bruto = os.environ.get(_VAR_FATOR, "")
    if not bruto.isdigit() or not 1 <= int(bruto) <= 99:
        sys.exit(f"defina {_VAR_FATOR} com um inteiro de 1 a 99 (centésimos do valor real)")
    return int(bruto)


def escalar_centavos(c: int) -> int:
    """Escala um valor em centavos pelo fator secreto, arredondando meio para longe do zero."""
    fator_num = _fator_num()
    sinal = -1 if c < 0 else 1
    return sinal * max(1 if c else 0, (abs(c) * fator_num * 2 + FATOR_DEN) // (FATOR_DEN * 2))


class Fictícios:
    """Mapeia textos reais para nomes fictícios estáveis (mesmo real -> mesmo fictício)."""

    def __init__(self, modelo: str):
        self.modelo, self.mapa = modelo, {}

    def __call__(self, real: str) -> str:
        chave = real.strip().casefold()
        if chave not in self.mapa:
            self.mapa[chave] = self.modelo.format(n=len(self.mapa) + 1)
        return self.mapa[chave]


# ------------------------------------------------------------------ OFX

_PARC_FIM = re.compile(r"^(?P<base>.*?)(?P<suf>(?:\s*-\s*(?:Parcela\s*)?\d{1,2}/\d{1,2})?)\s*$", re.I | re.S)
_PREFIXOS = ("Crédito de ", "Parcelamento de Compra - ")


GENERICOS = ("Pagamento recebido",)  # memos do próprio banco, sem dado pessoal


def _memo_anonimo(memo: str, nome) -> str:
    if memo.strip() in GENERICOS:
        return memo
    prefixo = ""
    for p in _PREFIXOS:
        if memo.startswith(p):
            prefixo, memo = p, memo[len(p):]
            break
    aspas = memo.startswith('"') and memo.rstrip().endswith('"')
    memo = memo.strip('"')
    m = _PARC_FIM.match(memo)
    base, suf = m["base"], m["suf"]
    return f'{prefixo}{"\"" if aspas else ""}{nome(base)}{"\"" if aspas else ""}{suf}'


def anonimizar_ofx(texto: str) -> str:
    nome_loja = Fictícios("Loja Ficticia {n:02d}")
    fitids: dict[str, str] = {}

    def troca_tag(tag: str, fn):
        nonlocal texto
        texto = re.sub(
            rf"(<{tag}>)([^<\r\n]*)", lambda m: m.group(1) + fn(m.group(2)), texto
        )

    def fitid(v):
        if v not in fitids:
            fitids[v] = str(uuid.uuid5(uuid.NAMESPACE_OID, f"fitid-{len(fitids)}"))
        return fitids[v]

    troca_tag("MEMO", lambda v: _memo_anonimo(v, nome_loja))
    troca_tag("FITID", fitid)
    troca_tag("ACCTID", lambda v: "00000000-0000-4000-8000-000000000001")
    troca_tag("TRNUID", lambda v: "0")
    troca_tag("TRNAMT", lambda v: _valor_ofx(v))
    troca_tag("BALAMT", lambda v: _valor_ofx(v))
    return texto


def _valor_ofx(v: str) -> str:
    neg = v.strip().startswith("-")
    cent = round(abs(float(v)) * 100)
    cent = escalar_centavos(cent)
    return f"{'-' if neg else ''}{cent // 100}.{cent % 100:02d}"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    modo, entrada, saida = argv[1], Path(argv[2]), Path(argv[3])
    saida.parent.mkdir(parents=True, exist_ok=True)
    if modo == "ofx":
        bruto = entrada.read_bytes()
        try:
            texto = bruto.decode("utf-8")
        except UnicodeDecodeError:
            texto = bruto.decode("cp1252")
        saida.write_text(anonimizar_ofx(texto), encoding="utf-8", newline="\n")
    elif modo in ("pdf-mp", "pdf-itau-extrato", "pdf-itau-fatura"):
        from app.importers.documento import extrair
        from app.importers.pdf_itau_fatura import extrair as extrair_fatura
        from scripts.anonimizar_itau_fatura import anonimizar_itau_fatura
        from scripts.anonimizar_pdf import anonimizar_itau_extrato, anonimizar_mp

        doc = (extrair_fatura if modo == "pdf-itau-fatura" else extrair)(entrada.read_bytes())
        funcao = {"pdf-mp": anonimizar_mp, "pdf-itau-extrato": anonimizar_itau_extrato,
                  "pdf-itau-fatura": anonimizar_itau_fatura}[modo]
        anonimo = funcao(doc, escalar_centavos, Fictícios)
        saida.write_text(json.dumps(anonimo, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    else:
        print(f"modo desconhecido: {modo}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
