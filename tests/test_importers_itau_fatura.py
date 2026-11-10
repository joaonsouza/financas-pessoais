import copy
import re
import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.importers import pdf_itau_fatura
from app.importers.base import LeitorErro, detectar_leitor, detectar_pdf
from app.importers.documento import linhas_do_documento

FIX = Path(__file__).parent / "fixtures" / "importacao"
AMOSTRAS = Path(os.environ.get("FINANCAS_AMOSTRAS", r"Z:\Meu Drive\Finanças\App\app-financas-amostras"))


def _fixture() -> dict:
    return json.loads((FIX / "itau_fatura.json").read_text(encoding="utf-8"))


def _p(t: str, x0: float, top: float) -> dict:
    return {"t": t, "x0": x0, "x1": x0 + len(t) * 5.0, "top": top, "bottom": top + 9}


def _resumo(atuais: str = "30,00") -> list[dict]:
    return [
        _p("Totaldafaturaanterior", 122, 115), _p("100,00", 400, 115),
        _p("Saldofinanciado", 358, 143), _p("0,00", 450, 143),
        _p("Lançamentosatuais", 358, 154), _p(atuais, 450, 154),
        _p("Vencimento:10/10/2026", 245, 164),
        _p("=Totaldestafatura", 358, 169), _p(atuais, 450, 169),
        _p("Emissão:03/10/2026", 256, 174),
        _p("Cartão", 70, 209), _p("1234.XXXX.XXXX.5678", 100, 209),
    ]


def _sintetico(extras: list[dict] | None = None, atuais: str = "30,00") -> dict:
    """Página 2 com duas colunas: a esquerda tem LOJAA e a direita, LOJAB (continuação da mesma lista)."""
    pagina2 = [
        _p("Lançamentos:", 151, 80), _p("compras", 210, 80), _p("Lançamentos:", 367, 80),
        _p("TITULARA(final1234)", 151, 90),
        _p("15/09", 151, 100), _p("LOJAA", 178, 100), _p("10,00", 323, 100),
        _p("COMER.CIDADE", 178, 109),
        _p("20/09", 367, 100), _p("LOJAB", 394, 100), _p("03/04", 480, 100), _p("20,00", 539, 100),
        _p("DIVERSOS.OUTRA", 394, 109),
        _p("Lançamentosnocartão(final1234)", 151, 130), _p("30,00", 323, 130),
        *(extras or []),
    ]
    return {"paginas": [
        {"numero": 1, "largura": 595.0, "altura": 842.0, "palavras": _resumo(atuais), "linhas": []},
        {"numero": 2, "largura": 595.0, "altura": 842.0, "palavras": pagina2, "linhas": []},
    ]}


def test_colunas_lidas_da_esquerda_para_a_direita_com_categoria_e_parcela():
    r = pdf_itau_fatura.interpretar(_sintetico())
    assert [t.descricao for t in r.transacoes] == ["LOJAA", "LOJAB"]
    a, b = r.transacoes
    assert (a.data, a.valor, a.dica_categoria, a.portador) == (date(2026, 9, 15), -1000, "COMER", "1234")
    assert (b.data, b.valor, b.dica_categoria) == (date(2026, 9, 20), -2000, "DIVERSOS")
    assert (b.parcela_num, b.parcela_total) == (3, 4)
    assert (r.banco, r.tipo_conta, r.identificador_conta) == ("itau", "cartao", "5678")
    assert (r.vencimento, r.fechamento, r.competencia_fatura) == (date(2026, 10, 10), date(2026, 10, 3), "2026-10")
    assert r.total_informado == 3000 and r.avisos == []


def test_letra_solta_da_outra_coluna_no_corte_e_ignorada():
    extras = [_p("L", 345, 100)]  # cai na coluna esquerda, depois do valor
    r = pdf_itau_fatura.interpretar(_sintetico(extras))
    assert [t.valor for t in r.transacoes] == [-1000, -2000]


def test_estorno_vira_positivo_e_parcela_antiga_usa_o_ano_anterior():
    doc = _sintetico([
        _p("02/12", 151, 150), _p("LOJAC03/04", 178, 150), _p("-5,00", 323, 150),
    ])
    r = pdf_itau_fatura.interpretar(doc)
    c = next(t for t in r.transacoes if t.descricao == "LOJAC")
    assert (c.valor, c.parcela_num, c.parcela_total, c.descricao) == (500, 3, 4, "LOJAC")
    assert c.data == date(2025, 12, 2)  # depois da emissão: ano anterior
    assert any("não bate" in a for a in r.avisos)  # a soma agora é 25,00 contra 30,00


def test_soma_divergente_gera_aviso():
    r = pdf_itau_fatura.interpretar(_sintetico(atuais="99,00"))
    assert r.avisos == ["A soma dos lançamentos não bate com os lançamentos atuais da fatura."]


def test_sem_resumo_da_erro():
    doc = _sintetico()
    doc["paginas"][0]["palavras"] = []
    with pytest.raises(LeitorErro, match="não reconhecida"):
        pdf_itau_fatura.interpretar(doc)


def test_fixture_anonimizada_valida_soma_e_estrutura():
    r = pdf_itau_fatura.interpretar(_fixture())
    assert (r.banco, r.tipo_conta, r.origem) == ("itau", "cartao", "pdf")
    assert len(r.transacoes) == 127 and r.avisos == []
    assert -sum(t.valor for t in r.transacoes) == r.total_informado
    assert (r.vencimento, r.fechamento, r.competencia_fatura) == (date(2026, 10, 10), date(2026, 10, 3), "2026-10")
    assert r.identificador_conta == "2222"
    assert {t.portador for t in r.transacoes} == {"1111", "2222", "3333", "4444"}
    assert all(t.descricao.startswith("LOJAFICTICIA") or t.tipo_sugerido == "encargo" for t in r.transacoes)
    assert sum(1 for t in r.transacoes if t.parcela_total) == 41
    assert sum(1 for t in r.transacoes if t.valor > 0) == 8  # estornos e créditos
    assert {t.dica_categoria for t in r.transacoes if t.dica_categoria} >= {"ALIMENTAÇÃO", "SAÚDE", "VEÍCULOS"}
    iof = [t for t in r.transacoes if t.tipo_sugerido == "encargo"]
    assert len(iof) == 1 and iof[0].descricao == "Repasse de IOF" and iof[0].data == r.fechamento


def test_fixture_e_detectada_como_fatura_itau():
    doc = _fixture()
    primeira = "\n".join(linhas_do_documento({"paginas": doc["paginas"][:1]}))
    assert detectar_pdf(primeira).nome == "pdf_itau_fatura"


def test_fixture_nao_traz_dados_pessoais():
    texto = json.dumps(_fixture(), ensure_ascii=False)
    assert "MARIATESTEEXEMPLODASILVA" in texto
    assert not re.search(r"[1-9]d{7,}", texto)  # sem números longos (CPF, conta, código de barras)


def test_alterar_um_valor_da_fixture_gera_aviso():
    doc = copy.deepcopy(_fixture())
    for pagina in doc["paginas"][1:]:
        for w in pagina["palavras"]:
            if w["t"].startswith("LOJAFICTICIA01"):
                alvo = pagina["palavras"].index(w)
                valor = next(p for p in pagina["palavras"][alvo:] if p["top"] == w["top"] and "," in p["t"])
                valor["t"] = "9.999,99"
                break
        else:
            continue
        break
    assert any("não bate" in a for a in pdf_itau_fatura.interpretar(doc).avisos)


@pytest.mark.amostra_real
def test_amostra_real_fatura_itau():
    arq = AMOSTRAS / "Fatura_Itau_20261005-220634.pdf"
    if not arq.exists():
        pytest.skip("amostra real ausente")
    conteudo = arq.read_bytes()
    assert detectar_leitor("fatura.pdf", conteudo).nome == "pdf_itau_fatura"
    r = pdf_itau_fatura.ler(conteudo)
    assert r.avisos == []
    assert -sum(t.valor for t in r.transacoes) == r.total_informado and len(r.transacoes) > 0
