"""Leitor de PDF via Claude API, para bancos desconhecidos ou quando o leitor local falha na validação.

PDF com texto: o texto é mascarado (CPF, cartão, agência/conta, e-mail, telefone) antes de ir para a API.
PDF sem texto (imagem): vai como documento base64; não há como mascarar, e a UI avisa antes de enviar.
O cliente é injetável (`cliente`): os testes usam um cliente falso e nenhuma chamada real é feita.
"""
from __future__ import annotations

import base64
import io
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta
from app.importers.mascaramento import mascarar
from app.services.ia import MODELO_PADRAO, chamar_parse

TENTATIVAS = 2  # a primeira chamada e uma nova tentativa se a saída for recusada ou inválida

_INSTRUCOES = """Você extrai lançamentos de um extrato ou fatura de cartão brasileiro.
Regras:
- Devolva só o que está no documento; não invente lançamentos nem valores.
- Datas no formato AAAA-MM-DD. Faturas trazem só DD/MM: use o ano do período da fatura.
- Valores em centavos, inteiros (R$ 12,34 = 1234). Saída/compra é NEGATIVA; entrada, estorno e crédito são POSITIVOS.
- `descricao` limpa (sem parcela nem data grudada) e `descricao_original` exatamente como veio.
- Parcela "03/12" ou "Parcela 3 de 12": preencha parcela_num e parcela_total.
- tipo_conta: "cartao" para fatura de cartão, "corrente" para extrato de conta.
- tipo_sugerido: "pagamento_fatura" (pagamento da fatura do cartão), "transferencia", "receita", "despesa", "encargo" (juros, IOF, multa) ou null.
- Não inclua linhas de saldo, totais ou subtotais como lançamentos.
- total_informado: total da fatura (cartão) em centavos, positivo. saldo_final_informado: saldo final da conta (extrato).
- competencia_fatura no formato AAAA-MM (mês do fechamento) e vencimento/fechamento em AAAA-MM-DD, quando constarem.
- identificador_conta: só os 4 últimos dígitos do cartão ou a conta já mascarada.
"""


class TransacaoIA(BaseModel):
    data: str = Field(description="AAAA-MM-DD")
    descricao: str
    descricao_original: str
    valor: int = Field(description="centavos; negativo = saída")
    id_externo: str | None = None
    parcela_num: int | None = None
    parcela_total: int | None = None
    dica_categoria: str | None = None
    portador: str | None = None
    tipo_sugerido: Literal["pagamento_fatura", "transferencia", "receita", "despesa", "encargo"] | None = None


class ResultadoIA(BaseModel):
    """Espelha `ResultadoLeitura` (datas como texto ISO, convertidas depois)."""

    banco: str | None = None
    tipo_conta: Literal["cartao", "corrente"]
    identificador_conta: str | None = None
    transacoes: list[TransacaoIA]
    competencia_fatura: str | None = Field(default=None, description="AAAA-MM")
    vencimento: str | None = Field(default=None, description="AAAA-MM-DD")
    fechamento: str | None = Field(default=None, description="AAAA-MM-DD")
    total_informado: int | None = None
    saldo_final_informado: int | None = None
    avisos: list[str] = Field(default_factory=list)


def _data(texto: str | None) -> date | None:
    return date.fromisoformat(texto) if texto else None


def para_resultado(ia: ResultadoIA) -> ResultadoLeitura:
    """Converte a saída da IA no contrato comum; `ValueError` se algum campo for inválido."""
    transacoes = [
        TransacaoBruta(
            data=date.fromisoformat(t.data), descricao=t.descricao.strip(),
            descricao_original=t.descricao_original or t.descricao, valor=t.valor, id_externo=t.id_externo,
            parcela_num=t.parcela_num, parcela_total=t.parcela_total, dica_categoria=t.dica_categoria,
            portador=t.portador, tipo_sugerido=t.tipo_sugerido,
        )
        for t in ia.transacoes
    ]
    avisos = ["Lido por IA: confira os lançamentos antes de importar.", *ia.avisos]
    if ia.tipo_conta == "cartao" and ia.total_informado is not None:
        compras = -sum(t.valor for t in transacoes if t.tipo_sugerido != "pagamento_fatura")
        if compras != ia.total_informado:
            avisos.append("A soma dos lançamentos não bate com o total informado na fatura.")
    return ResultadoLeitura(
        origem="pdf_ia", banco=ia.banco, tipo_conta=ia.tipo_conta, identificador_conta=ia.identificador_conta,
        transacoes=transacoes, competencia_fatura=ia.competencia_fatura, vencimento=_data(ia.vencimento),
        fechamento=_data(ia.fechamento), total_informado=ia.total_informado,
        saldo_final_informado=ia.saldo_final_informado, avisos=avisos,
    )


def texto_do_pdf(pdf_bytes: bytes) -> str:
    """Texto de todas as páginas ('' se o PDF não tem texto extraível). `LeitorErro` se ilegível."""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n\n".join((p.extract_text(x_tolerance=1.5) or "") for p in pdf.pages).strip()
    except Exception as exc:
        raise LeitorErro(f"Não foi possível ler o PDF: {exc}") from exc


def montar_mensagens(pdf_bytes: bytes, texto: str) -> list[dict]:
    """Texto mascarado quando há texto; senão o PDF em base64 antes do bloco de instruções."""
    if texto:
        return [{"role": "user", "content": [
            {"type": "text", "text": f"{_INSTRUCOES}\nTexto do documento (dados pessoais mascarados):\n\n{mascarar(texto)}"},
        ]}]
    documento = {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
    }}
    return [{"role": "user", "content": [documento, {"type": "text", "text": _INSTRUCOES}]}]


def ler_com_ia(pdf_bytes: bytes, cliente, modelo: str = MODELO_PADRAO) -> ResultadoLeitura:
    """Lê o PDF com a IA. Recusa ou saída inválida: uma nova tentativa; depois `LeitorErro`."""
    mensagens = montar_mensagens(pdf_bytes, texto_do_pdf(pdf_bytes))
    motivo = ""
    for _ in range(TENTATIVAS):
        resposta = chamar_parse(cliente, modelo=modelo, esforco="medium", saida=ResultadoIA, mensagens=mensagens)
        if resposta.stop_reason == "refusal":
            motivo = "A IA recusou-se a processar este documento."
            continue
        ia = resposta.parsed_output
        if ia is None:
            motivo = "A IA não devolveu uma resposta no formato esperado."
            continue
        try:
            return para_resultado(ia if isinstance(ia, ResultadoIA) else ResultadoIA.model_validate(ia))
        except (ValueError, ValidationError):
            motivo = "A IA devolveu datas ou valores inválidos."
    raise LeitorErro(f"{motivo} Tente novamente ou use outro arquivo.")
