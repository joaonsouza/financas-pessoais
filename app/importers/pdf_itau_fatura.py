"""Leitor da fatura do cartão Itaú (PDF). Etapas: `extrair` (pdfplumber) e `interpretar` (puro).

A fatura traz duas colunas de lançamentos lado a lado. Cada página é cortada pelo x das palavras
(coluna esquerda e depois direita) e cada lançamento ocupa 2 linhas: `DD/MM ESTABELECIMENTO [NN/NN] VALOR`
e `CATEGORIA.CIDADE` (a categoria do Itaú vira `dica_categoria`).
"""
from __future__ import annotations

import re
from datetime import date

from app.core.periodo import competencia_de
from app.importers.base import LeitorErro, ResultadoLeitura, TransacaoBruta
from app.importers.documento import Documento, agrupar_linhas, agrupar_palavras, extrair as extrair_documento
from app.importers.normalizacao import extrair_parcela, valor_br

_VALOR = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
_DATA = r"(\d{2})/(\d{2})/(\d{4})"
# o "(?:\s+[A-Z])?" final tolera uma letra solta da outra coluna que cai no corte (ex.: "... 12,34 L")
# o final tolera uma letra solta da outra coluna que cai no corte (ex.: "... 12,34 L")
_RE_LANC = re.compile(rf"^(\d{{2}})/(\d{{2}})\s+(.+?)\s+({_VALOR})(?:\s+[A-Z])?$")
_RE_PORTADOR = re.compile(r"\(final\s*(\d{4})\)\s*$", re.I)
_RE_SUBTOTAL = re.compile(rf"^Lançamentosnocartão\(final(\d{{4}})\)({_VALOR})$", re.I)
_RE_CATEGORIA = re.compile(r"^([A-ZÀ-Ú][A-ZÀ-Ú ]*?)\s*\.\s*(.*)$")
_RE_DATA_PALAVRA = re.compile(r"^\d{2}/\d{2}$")
_RE_VALOR_PALAVRA = re.compile(rf"^{_VALOR}$")
_CABECALHO = re.compile(r"^DATA\s+ESTABELECIMENTO", re.I)


def extrair(pdf_bytes: bytes) -> Documento:
    # as palavras vêm coladas com a tolerância padrão: 1,5 separa data, nome, parcela e valor
    return extrair_documento(pdf_bytes, x_tolerance=1.5)


def _sem_espaco(texto: str) -> str:
    return re.sub(r"\s+", "", texto)


def _data(d: str, m: str, a: str) -> date:
    return date(int(a), int(m), int(d))


def _ano(dia: int, mes: int, ref: date) -> date:
    """A fatura traz só DD/MM: a data é a mais recente que não passa da referência (emissão)."""
    for ano in (ref.year, ref.year - 1):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            continue
        if d <= ref:
            return d
    raise ValueError("data fora do período")


def _divisor(palavras: list[dict]) -> float:
    """x que separa a coluna esquerda da direita: o 2º "DATA" do cabeçalho ou o meio do conteúdo."""
    datas = sorted({round(p["x0"]) for p in palavras if p["t"] == "DATA"})
    if len(datas) >= 2 and datas[-1] - datas[0] > 100:
        return datas[-1] - 8
    alvos = [p for p in palavras if _RE_DATA_PALAVRA.match(p["t"]) or _RE_VALOR_PALAVRA.match(p["t"])]
    if not alvos:
        return 1e9
    return (min(p["x0"] for p in alvos) + max(p["x1"] for p in alvos)) / 2


def colunas_de_palavras(pagina: dict) -> list[list[list[dict]]]:
    """Coluna esquerda e depois a direita, cada uma como lista de linhas (lista de palavras)."""
    palavras = pagina["palavras"]
    corte = _divisor(palavras)
    esquerda = [p for p in palavras if p["x0"] < corte]
    direita = [p for p in palavras if p["x0"] >= corte]
    return [agrupar_palavras(esquerda), agrupar_palavras(direita)]


def eventos(doc: Documento):
    """Percorre as páginas de lançamentos (coluna esquerda, depois direita) classificando cada linha.

    Devolve `(tipo, texto, palavras, dados)`. Tipos: lanc (dados["m"] = match, dados["portador"]), categoria,
    portador, subtotal, repasse_iof, total_inter, total_atuais, cabecalho, outro (linha de seção ativa sem
    classificação) e inativo (fora das seções de lançamentos atuais).
    """
    ativo = internacional = False
    portador: str | None = None
    for pagina in doc["paginas"][1:]:
        for coluna in colunas_de_palavras(pagina):
            aguardando_categoria = False
            for palavras in coluna:
                linha = " ".join(p["t"] for p in palavras)
                s = _sem_espaco(linha)
                dados = {"portador": portador}
                if s.startswith("Lançamentosinternacionais"):
                    ativo, internacional, aguardando_categoria = True, True, False
                    yield "cabecalho", linha, palavras, dados
                    continue
                if s.startswith("Lançamentos:"):
                    ativo, internacional, aguardando_categoria = True, False, False
                    yield "cabecalho", linha, palavras, dados
                    continue
                if (s.endswith("Continua...") or "Totaldoslançamentosatuais" in s
                        or s.startswith("Comprasparceladas-próximasfaturas")):
                    ativo = internacional = False
                    yield ("total_atuais" if "Totaldoslançamentosatuais" in s else "inativo"), linha, palavras, dados
                    continue
                if not ativo:
                    yield "inativo", linha, palavras, dados
                    continue
                if _RE_SUBTOTAL.match(s):
                    aguardando_categoria = False
                    yield "subtotal", linha, palavras, {**dados, "m": _RE_SUBTOTAL.match(s)}
                    continue
                if _CABECALHO.match(linha):
                    aguardando_categoria = False
                    yield "cabecalho", linha, palavras, dados
                    continue
                if s.startswith("Totallançamentosinter"):
                    ativo = internacional = False
                    yield "total_inter", linha, palavras, dados
                    continue
                if s.startswith("Totaltransaçõesinter"):
                    aguardando_categoria = False
                    yield "total_inter", linha, palavras, dados
                    continue
                if internacional and s.startswith("RepassedeIOF"):
                    aguardando_categoria = False
                    yield "repasse_iof", linha, palavras, {**dados, "m": re.search(rf"({_VALOR})$", s)}
                    continue
                m = _RE_LANC.match(linha)
                if not m and (m_port := _RE_PORTADOR.search(s)):
                    portador, aguardando_categoria = m_port[1], False
                    yield "portador", linha, palavras, {"portador": portador, "m": m_port}
                    continue
                if m:
                    aguardando_categoria = True
                    yield "lanc", linha, palavras, {**dados, "m": m}
                    continue
                c = _RE_CATEGORIA.match(linha) if aguardando_categoria else None
                aguardando_categoria = False
                if c:
                    yield "categoria", linha, palavras, {**dados, "m": c}
                else:
                    yield "outro", linha, palavras, dados


def _resumo(linhas: list[str]) -> dict:
    achados: dict = {}
    valores = {
        "anterior": rf"Totaldafaturaanterior({_VALOR})",
        "financiado": rf"Saldofinanciado({_VALOR})",
        "atuais": rf"Lançamentosatuais({_VALOR})",
        "total": rf"Totaldestafatura({_VALOR})",
    }
    datas = {
        "vencimento": rf"Vencimento:{_DATA}", "emissao": rf"Emissão:{_DATA}",
        "proximo": rf"Previsãoprox\.Fechamento:{_DATA}",
    }
    for linha in linhas:
        s = _sem_espaco(linha)
        for chave, padrao in valores.items():
            if chave not in achados and (m := re.search(padrao, s)):
                achados[chave] = valor_br(m[1])
        for chave, padrao in datas.items():
            if chave not in achados and (m := re.search(padrao, s)):
                achados[chave] = _data(*m.groups())
        if "final_cartao" not in achados and (m := re.search(r"Cartão\d{4}\.X+\.X+\.(\d{4})", s)):
            achados["final_cartao"] = m[1]
    return achados


def interpretar(doc: Documento) -> ResultadoLeitura:
    pagina1 = doc["paginas"][0]
    resumo = _resumo([l["texto"] for l in agrupar_linhas(pagina1["palavras"])])
    if "total" not in resumo or "vencimento" not in resumo:
        raise LeitorErro("Fatura Itaú não reconhecida: resumo (Total desta fatura / Vencimento) não encontrado.")
    avisos: list[str] = []
    vencimento = resumo["vencimento"]
    # o fechamento é a data de emissão da fatura
    fechamento = resumo.get("emissao")
    if fechamento is None:
        avisos.append("Data de emissão (fechamento) não encontrada na fatura.")
    referencia = fechamento or vencimento

    transacoes: list[TransacaoBruta] = []
    anterior: TransacaoBruta | None = None
    for tipo, _, _, dados in eventos(doc):
        m = dados.get("m")
        if tipo == "lanc":
            try:
                data = _ano(int(m[1]), int(m[2]), referencia)
            except ValueError:
                avisos.append(f"Linha ignorada, data inválida: {m[1]}/{m[2]}.")
                anterior = None
                continue
            original = m[3].strip()
            limpa, num, total = extrair_parcela(original)
            # compra é saída (negativa); estorno vem negativo na fatura e vira positivo
            anterior = TransacaoBruta(
                data=data, descricao=limpa or original, descricao_original=original, valor=-valor_br(m[4]),
                parcela_num=num, parcela_total=total, portador=dados["portador"],
            )
            transacoes.append(anterior)
        elif tipo == "categoria" and anterior is not None:
            anterior.dica_categoria = m[1].strip()
        elif tipo == "repasse_iof" and m:
            transacoes.append(TransacaoBruta(
                data=referencia, descricao="Repasse de IOF", descricao_original="Repasse de IOF",
                valor=-valor_br(m[1]), portador=dados["portador"], tipo_sugerido="encargo"))
            anterior = None
        else:
            anterior = None

    soma = -sum(t.valor for t in transacoes)
    atuais = resumo.get("atuais")
    if not transacoes:
        avisos.append("Nenhum lançamento encontrado na fatura.")
    elif atuais is None:
        avisos.append("Lançamentos atuais não encontrados no resumo da fatura.")
    elif soma != atuais:
        avisos.append("A soma dos lançamentos não bate com os lançamentos atuais da fatura.")
    return ResultadoLeitura(
        origem="pdf", banco="itau", tipo_conta="cartao", identificador_conta=resumo.get("final_cartao"),
        transacoes=transacoes,
        competencia_fatura=competencia_de(fechamento) if fechamento else None,
        vencimento=vencimento, fechamento=fechamento,
        total_informado=atuais if atuais is not None else resumo["total"], avisos=avisos,
    )


def ler(pdf_bytes: bytes) -> ResultadoLeitura:
    return interpretar(extrair(pdf_bytes))
