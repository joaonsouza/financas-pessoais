"""Conversão de contas/transações da Pluggy (Open Finance) em `ResultadoLeitura`: mais uma fonte do pipeline."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.periodo import competencia_de
from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.importers.normalizacao import extrair_parcela, normalizar_descricao

_PAGAMENTO = ("PAGAMENTO RECEBIDO", "PAGAMENTO DE FATURA", "PAGAMENTO FATURA", "PAGAMENTO EFETUADO")


def identificador_da_conta(account_id: str) -> str:
    return f"pluggy:{account_id}"


def centavos(valor) -> int:
    """Reais (Decimal, int, float ou texto) em centavos, sem passar por float binário."""
    return int((Decimal(str(valor)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _data(texto) -> date | None:
    if not texto:
        return None
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None


def _fatura_unica(bruto: list[dict], faturas: list[dict]) -> dict | None:
    """A fatura da Pluggy, quando todas as transações do cartão pertencem a uma só."""
    ids = {(t.get("creditCardMetadata") or {}).get("billId") for t in bruto}
    if len(ids) != 1 or None in ids:
        return None
    return next((f for f in faturas if f.get("id") in ids), None)


def resultado_de_conta(conta_json: dict, transacoes_json: list[dict],
                       faturas_json: list[dict] | None = None, *,
                       representa_fatura: bool = True) -> ResultadoLeitura:
    """`representa_fatura=False` quando as transações são só uma janela de datas: o total da fatura não vale
    como validação da soma (nem é gravado na fatura)."""
    cartao = conta_json.get("type") == "CREDIT"
    avisos: list[str] = []
    brutas: list[dict] = []
    transacoes: list[TransacaoBruta] = []
    for t in transacoes_json:
        if t.get("status") != "POSTED":  # PENDING ainda pode mudar de valor ou sumir
            continue
        data = _data(t.get("date"))
        if data is None or t.get("amount") is None or t.get("type") not in ("DEBIT", "CREDIT"):
            avisos.append(f"Transação {t.get('id', '?')} ignorada: data, valor ou tipo ilegível.")
            continue
        valor = abs(centavos(t["amount"]))
        if t["type"] == "DEBIT":  # o sinal vem do tipo, nunca do sinal cru do amount
            valor = -valor
        meta = t.get("creditCardMetadata") or {}
        descricao = t.get("description") or t.get("descriptionRaw") or ""
        original = t.get("descriptionRaw") or descricao
        num, total = meta.get("installmentNumber"), meta.get("totalInstallments")
        if not (num and total and int(total) > 1 and 1 <= int(num) <= int(total)):
            descricao, num, total = extrair_parcela(descricao)  # sem metadados: tenta o sufixo "03/10"
        tipo = None
        if cartao and valor > 0 and any(normalizar_descricao(original).startswith(p) for p in _PAGAMENTO):
            tipo = "pagamento_fatura"
        brutas.append(t)
        transacoes.append(TransacaoBruta(
            data=data, descricao=descricao or original, descricao_original=original, valor=valor,
            id_externo=t.get("id"), parcela_num=int(num) if num else None,
            parcela_total=int(total) if total else None,
            dica_categoria=t.get("category") or None, tipo_sugerido=tipo))

    res = ResultadoLeitura(
        origem="openfinance", banco=None, tipo_conta="cartao" if cartao else "corrente",
        identificador_conta=identificador_da_conta(str(conta_json["id"])), transacoes=transacoes, avisos=avisos)
    if cartao:
        fatura = _fatura_unica(brutas, faturas_json or [])
        if fatura:
            res.vencimento, res.fechamento = _data(fatura.get("dueDate")), _data(fatura.get("billClosingDate"))
            if res.fechamento:  # competência = mês de fechamento (regra do app)
                res.competencia_fatura = competencia_de(res.fechamento)
            if representa_fatura and fatura.get("totalAmount") is not None:
                res.total_informado = centavos(fatura["totalAmount"])
    elif conta_json.get("balance") is not None:
        res.saldo_final_informado = centavos(conta_json["balance"])
    return res
