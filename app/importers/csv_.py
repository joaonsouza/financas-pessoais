"""Leitores de CSV: perfis Nubank (conta e cartão) e genérico com mapeamento (`PerfilCsv`)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from app.core.dinheiro import parse_reais
from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta, decodificar
from app.importers.normalizacao import extrair_parcela, normalizar_descricao

_FORMATOS_DATA = ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y")


def _linhas(conteudo: bytes, delimitador: str | None = None) -> tuple[list[str], list[dict[str, str]]]:
    texto = decodificar(conteudo)
    if not texto.strip():
        raise LeitorErro("Arquivo CSV vazio.")
    if delimitador is None:
        primeira = texto.splitlines()[0]
        delimitador = max((";", ",", "\t"), key=primeira.count)
    leitor = csv.DictReader(io.StringIO(texto), delimiter=delimitador)
    cabecalho = [c.strip() for c in (leitor.fieldnames or [])]
    leitor.fieldnames = cabecalho
    return cabecalho, list(leitor)


def _data(texto: str, formatos: tuple[str, ...] = _FORMATOS_DATA) -> date:
    texto = (texto or "").strip()
    for formato in formatos:
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValueError(f"data inválida: {texto!r}")


def _valor(texto: str, decimal: str = ".") -> int:
    t = (texto or "").strip()
    if not t:
        raise ValueError("valor vazio")
    if decimal == ",":
        t = t.replace(".", "")
    else:
        t = t.replace(",", "")
        t = t.replace(".", ",") if "," not in t else t
    return parse_reais(t)


def _montar(data: date, original: str, valor: int, id_externo: str | None = None) -> TransacaoBruta:
    original = (original or "").strip()
    limpa, num, total = extrair_parcela(original)
    return TransacaoBruta(
        data=data, descricao=limpa or original, descricao_original=original, valor=valor,
        id_externo=(id_externo or "").strip() or None, parcela_num=num, parcela_total=total,
    )


def _ler(conteudo: bytes, colunas: tuple[str, str, str], tipo_conta: str, banco: str | None,
         *, decimal: str = ".", inverter: bool = False, delimitador: str | None = None,
         formatos: tuple[str, ...] = _FORMATOS_DATA, coluna_id: str | None = None,
         identificador_conta: str | None = None) -> ResultadoLeitura:
    cabecalho, linhas = _linhas(conteudo, delimitador)
    col_data, col_desc, col_valor = colunas
    faltando = [c for c in (col_data, col_desc, col_valor) if c not in cabecalho]
    if faltando:
        raise LeitorErro(f"Colunas não encontradas no CSV: {', '.join(faltando)}.")
    transacoes: list[TransacaoBruta] = []
    avisos: list[str] = []
    for n, linha in enumerate(linhas, start=2):  # linha 1 é o cabeçalho
        if not any((v or "").strip() for v in linha.values() if isinstance(v, str)):
            continue
        try:
            valor = _valor(linha[col_valor], decimal)
            data = _data(linha[col_data], formatos)
        except (ValueError, TypeError, KeyError):
            avisos.append(f"Linha {n} ignorada: data ou valor inválido.")
            continue
        t = _montar(data, linha.get(col_desc, ""), -valor if inverter else valor,
                    linha.get(coluna_id) if coluna_id else None)
        transacoes.append(t)
    return ResultadoLeitura(
        origem="csv", banco=banco, tipo_conta=tipo_conta, identificador_conta=identificador_conta,
        transacoes=transacoes, avisos=avisos,
    )


def ler_nubank_conta(conteudo: bytes) -> ResultadoLeitura:
    """`Data,Valor,Identificador,Descrição` (valor com sinal)."""
    return _ler(conteudo, ("Data", "Descrição", "Valor"), "corrente", "nubank",
                delimitador=",", coluna_id="Identificador")


def ler_nubank_cartao(conteudo: bytes) -> ResultadoLeitura:
    """`date,title,amount`: no CSV do Nubank a compra é positiva; aqui a compra vira negativa."""
    r = _ler(conteudo, ("date", "title", "amount"), "cartao", "nubank", inverter=True, delimitador=",")
    for t in r.transacoes:
        if t.valor > 0 and normalizar_descricao(t.descricao_original).startswith("PAGAMENTO"):
            t.tipo_sugerido = "pagamento_fatura"
    return r


def ler_c6_cartao(conteudo: bytes) -> ResultadoLeitura:
    """Fatura do C6: `Data de Compra;Nome no Cartão;Final do Cartão;Categoria;Descrição;Parcela;...;Valor (em R$)`.

    Compra vem positiva (vira negativa aqui); pagamento e estorno vêm negativos. `Parcela` é "Única" ou "1/3".
    """
    _, linhas = _linhas(conteudo, ";")
    transacoes: list[TransacaoBruta] = []
    avisos: list[str] = []
    finais: set[str] = set()
    for n, linha in enumerate(linhas, start=2):
        if not any((v or "").strip() for v in linha.values() if isinstance(v, str)):
            continue
        try:
            valor = -_valor(linha["Valor (em R$)"])
            data = _data(linha["Data de Compra"])
        except (ValueError, TypeError, KeyError):
            avisos.append(f"Linha {n} ignorada: data ou valor inválido.")
            continue
        original = (linha.get("Descrição") or "").strip()
        parcela = (linha.get("Parcela") or "").strip()
        _, num, total = extrair_parcela(parcela) if parcela.lower() != "única" else ("", None, None)
        final = (linha.get("Final do Cartão") or "").strip() or None
        if final:
            finais.add(final)
        tipo = None
        if valor > 0 and normalizar_descricao(original).startswith(("PAGAMENTO", "INCLUSAO DE PAGAMENTO")):
            tipo = "pagamento_fatura"
        transacoes.append(TransacaoBruta(
            data=data, descricao=original, descricao_original=" · ".join(p for p in (original, parcela) if p),
            valor=valor, parcela_num=num, parcela_total=total,
            dica_categoria=(linha.get("Categoria") or "").strip() or None, portador=final, tipo_sugerido=tipo,
        ))
    return ResultadoLeitura(
        origem="csv", banco="c6", tipo_conta="cartao",
        identificador_conta=finais.pop() if len(finais) == 1 else None,
        transacoes=transacoes, avisos=avisos,
    )


def ler_generico(conteudo: bytes, perfil, tipo_conta: str = "corrente") -> ResultadoLeitura:
    """CSV com mapeamento. `perfil` é um `PerfilCsv` ou o dict do mapeamento:

    `coluna_data`, `coluna_descricao`, `coluna_valor`, `formato_data` (strptime, opcional),
    `separador_decimal` ("," ou "."), `inverter_sinal` (bool) e `delimitador` (opcional).
    """
    mapa = getattr(perfil, "mapeamento", perfil)
    if not mapa:
        raise LeitorErro("CSV desconhecido: informe o mapeamento de colunas.")
    try:
        colunas = (mapa["coluna_data"], mapa["coluna_descricao"], mapa["coluna_valor"])
    except KeyError as e:
        raise LeitorErro(f"Mapeamento incompleto: falta {e.args[0]}.") from e
    formato = mapa.get("formato_data")
    return _ler(
        conteudo, colunas, tipo_conta, None,
        decimal=mapa.get("separador_decimal", ","), inverter=bool(mapa.get("inverter_sinal")),
        delimitador=mapa.get("delimitador"), formatos=(formato,) if formato else _FORMATOS_DATA,
    )
