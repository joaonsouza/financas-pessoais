"""Anonimização de Documentos de PDF (parte de `anonimizar_amostra.py`)."""
from __future__ import annotations

import re
import unicodedata

from app.importers.documento import refazer_palavras
from app.importers.normalizacao import valor_br
from app.importers.pdf_itau_extrato import _RE_CONTA, _RE_LINHA as _RE_LINHA_EXTRATO
from app.importers.pdf_mercadopago import _RE_CARTAO, _RE_LINHA as _RE_LINHA_MP, _RE_TOTAL_BLOCO

FAKE_TITULAR = ["Maria", "Teste", "Exemplo", "Silva"]
_RS = re.compile(r"(R\$\s*)(-?)(\d{1,3}(?:\.\d{3})*,\d{2})")
_PARCELA_FIM = re.compile(r"^(.*?)(\s*Parcela\s+\d+\s+de\s+\d+)?$")


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c))


def fmt_br(c: int) -> str:
    inteiro, frac = divmod(abs(c), 100)
    return ("-" if c < 0 else "") + f"{inteiro:,}".replace(",", ".") + f",{frac:02d}"


def escalar_rs(linha: str, escalar) -> str:
    """Escala todos os "R$ x" da linha."""
    return _RS.sub(lambda m: m[1] + m[2] + fmt_br(escalar(valor_br(m[3]))), linha)


def documento_de_linhas(doc: dict, novas: list[list[str]]) -> dict:
    """Refaz linhas e palavras de cada página a partir dos novos textos (posições sintéticas)."""
    paginas = []
    for pagina, textos in zip(doc["paginas"], novas):
        linhas, palavras = [], []
        for original, texto in zip(pagina["linhas"], textos):
            linhas.append({"texto": texto, "x0": original["x0"], "top": original["top"]})
            palavras += refazer_palavras(texto, original["x0"], original["top"])
        paginas.append({"numero": pagina["numero"], "largura": pagina["largura"],
                        "altura": pagina["altura"], "palavras": palavras, "linhas": linhas})
    return {"paginas": paginas}


def _trocar_nomes(linha: str, fake_por_token: dict[str, str]) -> str:
    return re.sub(
        r"[^\W\d_]+", lambda m: fake_por_token.get(_sem_acento(m.group()).casefold(), m.group()), linha
    )


def _tokens_titular(texto: str) -> dict[str, str]:
    tokens = [t for t in re.findall(r"[^\W\d_]+", texto) if len(t) >= 3]
    return {_sem_acento(t).casefold(): FAKE_TITULAR[i % len(FAKE_TITULAR)] for i, t in enumerate(tokens)}


def anonimizar_mp(doc: dict, escalar, ficticio) -> dict:
    textos_originais = [[l["texto"] for l in p["linhas"]] for p in doc["paginas"]]
    nomes_linhas = textos_originais[0][:2]
    nomes = _tokens_titular(" ".join(nomes_linhas))
    loja = ficticio("LOJA FICTICIA {n:02d}")

    novas: list[list[str]] = []
    secao = None
    compras: list[tuple[int, int]] = []
    for pi, pagina in enumerate(textos_originais):
        saida = []
        for li, original in enumerate(pagina):
            t = original
            if (li < 2 and (pi == 0 or original in nomes_linhas)) or "Olá," in t:
                t = _trocar_nomes(t, nomes)
            t = re.sub(r"\[\*+\d{4}\]", "[************1234]", t)
            if original.startswith("Movimentações na fatura"):
                secao = "mov"
            elif _RE_CARTAO.search(original):
                secao = "cartao"
            elif _RE_TOTAL_BLOCO.match(original):
                secao = None
            elif secao == "cartao" and (m := _RE_LINHA_MP.match(original)):
                nome, parcela = _PARCELA_FIM.match(m[3]).groups()
                t = t.replace(m[3], loja(nome) + (parcela or ""), 1)
                compras.append((pi, li))
            saida.append(escalar_rs(t, escalar))
        novas.append(saida)

    # soma das compras escaladas = total escalado: o resíduo vai na maior compra
    todas = [t for p in novas for t in p]
    rotulo = next(i for i, t in enumerate(todas) if t.startswith("Total a pagar"))
    total = valor_br(_RS.search(todas[rotulo + 1])[3])
    valores = {(pi, li): valor_br(_RS.search(novas[pi][li])[3]) for pi, li in compras}
    maior = max(valores, key=valores.get)
    delta = total - sum(valores.values())
    pi, li = maior
    novas[pi][li] = _RS.sub(lambda m: m[1] + fmt_br(valores[maior] + delta), novas[pi][li], count=1)
    for p in novas:
        for i, t in enumerate(p):
            if _RE_TOTAL_BLOCO.match(t):
                p[i] = f"Total R$ {fmt_br(total)}"
    return documento_de_linhas(doc, novas)


def anonimizar_itau_extrato(doc: dict, escalar, ficticio) -> dict:
    linhas = [[l["texto"] for l in p["linhas"]] for p in doc["paginas"]]
    cabecalho = next(t for p in linhas for t in p if _RE_CONTA.search(t))
    titular_real = _sem_acento(re.split(r"\s\d{3}\.?\d{3}", cabecalho)[0]).upper().strip()
    titular_fake = "MARIA TESTE EXEMPLO DA SILVA"
    pessoa, empresa = ficticio("PESSOA{n:02d}"), ficticio("EMPRESA{n:02d}")
    fixos = ("SALDO DO DIA", "IOF", "JUROS LIMITE DA CONTA", "REMUNERACAO/SALARIO", "REND PAGO APLIC AUT",
             "FATURA UNICLASS SIGNATURE", "PIX QRS NU PAGAMENT", "PIX QRS MERCADO PAG")

    def desc_anonima(desc: str) -> str:
        norm = _sem_acento(desc).upper()
        if any(norm.startswith(f) for f in fixos):
            return desc
        if norm.startswith("ITAU VISA"):
            return "ITAU VISA 0000-0000"
        for prefixo in ("PIX TRANSF ", "PIX QRS ", "PAG BOLETO ", "PAGAMENTOCONTA ", "INT /"):
            if norm.startswith(prefixo):
                m = re.match(r"^(.*?)(\d{2}/\d{2})?$", desc[len(prefixo):])
                nome, colado = m[1].strip(), m[2] or ""
                if prefixo == "PIX TRANSF " and len(nome) >= 4 and titular_real.startswith(_sem_acento(nome).upper()):
                    fake = titular_fake[: len(nome)]
                elif prefixo == "PIX TRANSF ":
                    fake = pessoa(nome)
                else:
                    fake = empresa(re.sub(r"\s*\d{6,}$", "", nome))
                extra = " 000000000" if re.search(r"\d{6,}$", nome) else ""
                return f"{prefixo}{fake}{extra}{colado}"
        return empresa(desc)

    registros = []  # (pagina, indice, data, descricao, valor, match)
    for pi, pagina in enumerate(linhas):
        for li, t in enumerate(pagina):
            m = _RE_LINHA_EXTRATO.match(t)
            if m:
                registros.append((pi, li, (int(m[3]), int(m[2]), int(m[1])), m[4].strip(), valor_br(m[5]), m))
    eh_saldo = lambda desc: desc.upper().startswith("SALDO DO DIA")  # noqa: E731
    saldos = {d: v for _, _, d, desc, v, _ in registros if eh_saldo(desc)}
    lancs = [(d, v) for _, _, d, desc, v, _ in registros if not eh_saldo(desc)]
    datas = sorted(saldos)
    novos = {datas[0]: escalar(saldos[datas[0]])}
    for p, d in zip(datas, datas[1:]):
        grupo = [v for dd, v in lancs if p < dd <= d]
        posteriores = [v for dd, v in lancs if dd > d]
        diff = saldos[d] - saldos[p]
        if diff == sum(grupo):
            novos[d] = novos[p] + sum(escalar(v) for v in grupo)
        elif d == datas[-1] and diff == sum(grupo) + sum(posteriores):
            novos[d] = novos[p] + sum(escalar(v) for v in grupo + posteriores)
        else:
            novos[d] = novos[p] + escalar(diff)

    saida = [list(p) for p in linhas]
    for pi, li, d, desc, v, m in registros:
        novo = novos[d] if eh_saldo(desc) else escalar(v)
        saida[pi][li] = f"{m[1]}/{m[2]}/{m[3]} {desc_anonima(desc)} {fmt_br(novo)}"
    for pi, pagina in enumerate(saida):
        for li, t in enumerate(pagina):
            if _RE_CONTA.search(t):
                pagina[li] = f"{titular_fake.title()} 000.000.000-00 agência: 1234 conta: 56789-0"
            elif li and pagina[li - 1].lower().startswith("saldo em conta"):
                outros = [escalar(valor_br(v[2])) for v in _RS.findall(t)[1:]]
                pagina[li] = " ".join(f"R$ {fmt_br(v)}" for v in [novos[datas[-1]], *outros])
    for pi, pagina in enumerate(saida):  # texto legal depois de "Aviso!" não entra na fixture
        for li, t in enumerate(pagina):
            if t.startswith("Aviso!"):
                saida[pi] = pagina[: li + 1]
                saida[pi][li] = "Aviso! Texto legal removido da fixture anonimizada."
                doc["paginas"][pi]["linhas"] = doc["paginas"][pi]["linhas"][: li + 1]
                break
    return documento_de_linhas(doc, saida)
