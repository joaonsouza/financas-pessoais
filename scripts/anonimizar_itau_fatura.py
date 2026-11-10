"""Anonimização da fatura Itaú, palavra a palavra (parte de `anonimizar_amostra.py`).

As posições (x/y) das palavras são mantidas, porque o leitor separa as colunas pelo x. Só as 4 primeiras
páginas entram (as demais são texto de produtos e avisos). Totais e subtotais são recalculados.
"""
from __future__ import annotations

import re

from app.importers.documento import agrupar_linhas, agrupar_palavras
from app.importers.normalizacao import valor_br
from app.importers.pdf_itau_fatura import (
    _RE_CATEGORIA, _RE_DATA_PALAVRA, _sem_espaco, eventos,
)
from scripts.anonimizar_pdf import fmt_br

_DINHEIRO = re.compile(r"(?<![\d,.])-?\d{1,3}(?:\.\d{3})*,\d{2}(?![\d%])")
_DIGITOS_LONGOS = re.compile(r"\d{8,}")
_PARCELA_COLADA = re.compile(r"^(.*?)(\d{2}/\d{2})$")
_FINAIS_FICTICIOS = ["1111", "2222", "3333", "4444", "5555", "6666"]
_NOME_FICTICIO = "MARIATESTEEXEMPLODASILVA"


def _txt(linha: list[dict]) -> str:
    return " ".join(w["t"] for w in linha if w["t"])


def _dinheiro(palavras: list[dict]) -> list[dict]:
    return [w for w in palavras if w["t"] and _DINHEIRO.search(w["t"])]


def _definir_dinheiro(palavra: dict, centavos: int) -> None:
    palavra["t"] = _DINHEIRO.sub(fmt_br(centavos), palavra["t"], count=1)


def _remover(palavras: list[dict], a: int = 0, b: int | None = None) -> None:
    for w in palavras[a:b]:
        w["t"] = None


def anonimizar_itau_fatura(doc: dict, escalar, ficticio) -> dict:
    paginas = [dict(p, palavras=[dict(w) for w in p["palavras"] if w["top"] < 800]) for p in doc["paginas"][:4]]
    doc = {"paginas": paginas}
    loja = ficticio("LOJAFICTICIA{n:02d}")
    finais: dict[str, str] = {}

    def final_ficticio(real: str) -> str:
        return finais.setdefault(real, _FINAIS_FICTICIOS[len(finais) % len(_FINAIS_FICTICIOS)])

    # 1) números: sequências longas viram zeros e todo valor monetário é escalado
    for pagina in paginas:
        for w in pagina["palavras"]:
            t = _DIGITOS_LONGOS.sub(lambda m: "0" * len(m.group()), w["t"])
            w["t"] = _DINHEIRO.sub(lambda m: fmt_br(escalar(valor_br(m.group()))), t)

    evs = list(eventos(doc))

    # 2) lançamentos, categorias, cabeçalhos de portador e linhas soltas
    bloco: list[int] = []
    for tipo, texto, palavras, dados in evs:
        if tipo == "lanc":
            valor_w = _dinheiro(palavras)[-1]
            i_valor = palavras.index(valor_w)
            nome = palavras[1:i_valor]
            parcela_sep = bool(nome) and bool(_RE_DATA_PALAVRA.match(nome[-1]["t"]))
            partes = nome[:-1] if parcela_sep else nome
            colada = _PARCELA_COLADA.match("".join(w["t"] for w in partes))
            base = colada[1] if colada else "".join(w["t"] for w in partes)
            partes[0]["t"] = loja(base) + (colada[2] if colada else "")
            _remover(partes[1:])
            _remover(palavras, i_valor + 1)  # letra solta da outra coluna
            dados["valor"] = -valor_br(valor_w["t"])
            bloco.append(dados["valor"])
        elif tipo == "categoria":
            palavras[0]["t"] = f"{dados['m'][1].strip()}.CIDADE"
            _remover(palavras, 1)
        elif tipo == "portador":
            ficticio_final = final_ficticio(dados["m"][1])
            numero = list(finais.values()).index(ficticio_final) + 1
            palavras[0]["t"] = f"TITULAR{numero:02d}(final{ficticio_final})"
            _remover(palavras, 1)
            bloco = []
        elif tipo == "repasse_iof":
            dados["valor"] = -valor_br(_dinheiro(palavras)[-1]["t"])
        elif tipo == "subtotal":
            dados["soma_bloco"] = -sum(bloco)
            bloco = []
        elif tipo == "outro" and not texto.startswith("Dólar") and "$" not in texto:
            i = next((k for k, w in enumerate(palavras) if any(c.isdigit() for c in w["t"])), len(palavras))
            palavras[0]["t"] = "CIDADE"
            _remover(palavras, 1, i)
        elif tipo == "inativo" and (re.match(r"^\d{2}/\d{2}\s", texto) or _RE_CATEGORIA.match(texto)):
            _remover(palavras)

    soma = -sum(d["valor"] for t, _, _, d in evs if t in ("lanc", "repasse_iof"))
    soma_inter, repasse, internacional = 0, 0, False
    for tipo, texto, _, dados in evs:
        if tipo == "cabecalho":
            internacional = _sem_espaco(texto).startswith("Lançamentosinternacionais")
        elif tipo == "lanc" and internacional:
            soma_inter -= dados["valor"]
        elif tipo == "repasse_iof":
            repasse = -dados["valor"]
    for tipo, texto, palavras, dados in evs:
        if tipo == "subtotal":
            palavras[0]["t"] = f"Lançamentos no cartão (final{final_ficticio(dados['m'][1])}) {fmt_br(dados['soma_bloco'])}"
            _remover(palavras, 1)
        elif tipo == "total_atuais":
            _definir_dinheiro(_dinheiro(palavras)[-1], soma)
        elif tipo == "total_inter":
            alvo = soma_inter if _sem_espaco(texto).startswith("Totaltransações") else soma_inter + repasse
            _definir_dinheiro(_dinheiro(palavras)[-1], alvo)

    # 3) página 1: remetente, titular, cartão e resumo
    p1 = paginas[0]["palavras"]
    for w in p1:
        if w["top"] < 200 and (w["x0"] < 240 or w["top"] < 100):
            w["t"] = _NOME_FICTICIO if round(w["top"]) == 94 and w["x0"] < 240 else None
        elif re.match(r"^\d{4}\.X+\.X+\.\d{4}$", w["t"] or ""):
            w["t"] = f"0000.XXXX.XXXX.{final_ficticio(w['t'][-4:])}"
    linhas = agrupar_palavras([w for w in p1 if w["t"]])
    financiado, apos_total_da_fatura = 0, False
    for linha in linhas:
        s = _sem_espaco(_txt(linha))
        if s.startswith("Titular") and len(linha) > 1:
            linha[1]["t"] = _NOME_FICTICIO
            _remover(linha, 2)
        elif "Saldofinanciado" in s and _dinheiro(linha):
            financiado = valor_br(_dinheiro(linha)[-1]["t"])
    for linha in linhas:
        s = _sem_espaco(_txt(linha))
        if "Lançamentosatuais" in s and _dinheiro(linha):
            _definir_dinheiro(_dinheiro(linha)[-1], soma)
        elif "Totaldestafatura" in s and _dinheiro(linha):
            _definir_dinheiro(_dinheiro(linha)[-1], soma + financiado)
        elif apos_total_da_fatura and _dinheiro(linha):
            _definir_dinheiro(_dinheiro(linha)[0], soma + financiado)
        apos_total_da_fatura = s.startswith("Ototaldasuafatura")

    novas = []
    for pagina in paginas:
        palavras = [w for w in pagina["palavras"] if w["t"]]
        novas.append({"numero": pagina["numero"], "largura": pagina["largura"], "altura": pagina["altura"],
                      "palavras": palavras, "linhas": agrupar_linhas(palavras)})
    return {"paginas": novas}
