"""Tela inicial: o cartão em que a compra de hoje vence mais tarde ganha o selo "Melhor hoje"."""
from tests.factories import nova_conta, novo_cartao

SELO = "★ Melhor hoje"
LINHA = '<li class="py-4 flex items-center gap-3">'


def _linha_do_cartao(html: str, nome: str) -> str:
    """O trecho da lista de cartões que pertence ao cartão `nome`."""
    trechos = [t for t in html.split(LINHA)[1:] if f">{nome}</span>" in t]
    assert len(trechos) == 1, f"cartão {nome!r} não encontrado no bloco"
    return trechos[0].split("</li>")[0]


def _cenario(db, *, sugerir_longo: bool = True):
    corrente = nova_conta(db, "Conta Exemplo")
    longo = novo_cartao(db, "Cartão Prazo Longo", pagadora=corrente, fechamento=3, vencimento=10)
    curto = novo_cartao(db, "Cartão Prazo Curto", pagadora=corrente, fechamento=20, vencimento=27)
    longo.sugerir_compra = sugerir_longo
    db.commit()
    return longo, curto


def test_selo_no_cartao_de_vencimento_mais_distante(client, db):
    _cenario(db)
    html = client.get("/?mes=2026-10").text
    assert html.count(SELO) == 1
    linha = _linha_do_cartao(html, "Cartão Prazo Longo")
    assert SELO in linha
    assert "A compra de hoje só vence em 10/11/2026 (36 dias)" in linha
    assert SELO not in _linha_do_cartao(html, "Cartão Prazo Curto")


def test_selo_aparece_em_outro_mes_selecionado(client, db):
    _cenario(db)
    html = client.get("/?mes=2026-12").text
    assert SELO in _linha_do_cartao(html, "Cartão Prazo Longo")


def test_cartao_fora_da_sugestao_nao_ganha_selo(client, db):
    _cenario(db, sugerir_longo=False)
    html = client.get("/?mes=2026-10").text
    assert html.count(SELO) == 1
    assert SELO in _linha_do_cartao(html, "Cartão Prazo Curto")
    assert SELO not in _linha_do_cartao(html, "Cartão Prazo Longo")


def test_nenhum_cartao_na_sugestao_nao_ha_selo(client, db):
    longo, curto = _cenario(db)
    for c in (longo, curto):
        c.sugerir_compra = False
    db.commit()
    html = client.get("/?mes=2026-10").text
    assert "Cartão Prazo Longo" in html and "Melhor hoje" not in html
