from datetime import date

from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _base(db):
    itau = nova_conta(db, "Itaú")
    nubank = nova_conta(db, "Nubank")
    mercado = nova_categoria(db, "Mercado", cor="#22c55e")
    salario = nova_categoria(db, "Salário", tipo="receita", cor="#3b82f6")
    novo_lancamento(db, itau, date(2026, 10, 2), -1250, categoria=mercado, descricao="Padaria")
    novo_lancamento(db, nubank, date(2026, 10, 5), 500000, categoria=salario, descricao="Pagamento empresa")
    novo_lancamento(db, itau, date(2026, 10, 3), -30000, descricao="Conserto", status="pendente")
    db.commit()
    return itau, nubank


def _tabela(texto: str) -> str:
    """As tabelas da lista (pendentes e depois efetivados), do começo da primeira ao fim da última."""
    marcas = [texto.find(f'id="{i}"') for i in ("tabela-pendentes", "tabela-lancamentos")]
    inicio = min(m for m in marcas if m >= 0)
    return texto[inicio:texto.rindex("</table>") + len("</table>")]


def _ordem(texto: str, nomes=("Padaria", "Pagamento empresa", "Conserto")) -> list[str]:
    tabela = _tabela(texto)
    return sorted(nomes, key=lambda n: tabela.index(f">{n}<"))


def test_tabela_tem_colunas(client, db):
    _base(db)
    tabela = _tabela(client.get("/lancamentos?mes=2026-10").text)
    for coluna in ("Situação", "Data", "Descrição", "Categoria", "Conta", "Valor"):
        assert f">{coluna}<" in tabela
    assert "Sem categoria" in tabela
    assert "Itaú" in tabela and "Nubank" in tabela


def test_padrao_pendentes_primeiro_e_cada_secao_por_data_crescente(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10").text
    assert _ordem(texto) == ["Conserto", "Padaria", "Pagamento empresa"]
    assert 'aria-sort="ascending"' in _tabela(texto)
    assert "Pendentes" in texto and "Efetivados" in texto


def test_secoes_separam_pendentes_de_efetivados(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10").text
    pendentes = texto[texto.index('id="tabela-pendentes"'):texto.index('id="tabela-lancamentos"')]
    efetivados = texto[texto.index('id="tabela-lancamentos"'):]
    assert ">Conserto<" in pendentes and ">Padaria<" not in pendentes
    assert ">Padaria<" in efetivados and ">Conserto<" not in efetivados.split("</table>")[0]


def test_ordenar_por_valor_usa_o_tamanho_do_valor(client, db):
    _base(db)
    # pendentes continuam antes dos efetivados; dentro de cada seção vale a ordem pedida
    texto = client.get("/lancamentos?mes=2026-10&ordem=valor&dir=desc").text
    assert _ordem(texto) == ["Conserto", "Pagamento empresa", "Padaria"]
    texto = client.get("/lancamentos?mes=2026-10&ordem=valor&dir=asc").text
    assert _ordem(texto) == ["Conserto", "Padaria", "Pagamento empresa"]


def test_ordenar_por_descricao_conta_categoria_situacao(client, db):
    _base(db)
    assert _ordem(client.get("/lancamentos?mes=2026-10&ordem=descricao&dir=asc").text) == [
        "Conserto", "Padaria", "Pagamento empresa"]
    assert _ordem(client.get("/lancamentos?mes=2026-10&ordem=conta&dir=asc").text)[-1] == "Pagamento empresa"
    # sem categoria vai para o fim da própria seção (a pendente "Conserto" é só ela na seção)
    assert _ordem(client.get("/lancamentos?mes=2026-10&ordem=categoria&dir=asc").text) == [
        "Conserto", "Padaria", "Pagamento empresa"]
    assert _ordem(client.get("/lancamentos?mes=2026-10&ordem=situacao&dir=asc").text)[0] == "Conserto"


def test_links_do_cabecalho_mantem_filtros(client, db):
    itau, _ = _base(db)
    tabela = _tabela(client.get(f"/lancamentos?mes=2026-10&conta={itau.id}").text)
    # a coluna ativa (Data, crescente) inverte; as outras começam na direção própria
    assert f'href="?mes=2026-10&amp;conta={itau.id}&amp;ordem=data&amp;dir=desc"' in tabela
    assert f'href="?mes=2026-10&amp;conta={itau.id}&amp;ordem=valor&amp;dir=desc"' in tabela


def test_cards_de_tipo_mantem_ordem(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&ordem=valor&dir=asc").text
    assert 'href="?mes=2026-10&amp;tipo=receita&amp;ordem=valor&amp;dir=asc"' in texto


def test_ordem_invalida_cai_no_padrao(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&ordem=xyz&dir=lado").text
    assert _ordem(texto) == ["Conserto", "Padaria", "Pagamento empresa"]


def test_lista_do_celular_continua_por_dia(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&ordem=valor&dir=asc").text
    assert 'id="lanc-' in texto
    assert "02/10/2026" in texto


def test_fatura_aparece_na_tabela(client, db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta, fechamento=3, vencimento=10)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, cartao, date(2026, 9, 20), -10000, descricao="Mercado A", fatura_id=fatura.id)
    db.commit()
    tabela = _tabela(client.get("/lancamentos?mes=2026-10").text)
    assert "Fatura Cartão Nubank" in tabela
    assert f'href="/cartoes/{cartao.id}?fatura=2026-10"' in tabela


def test_alternar_na_tabela_devolve_linha_da_tabela(client, db):
    _base(db)
    from sqlalchemy import select

    from app.models import Lancamento
    lanc = db.scalars(select(Lancamento).where(Lancamento.descricao == "Conserto")).one()
    resposta = client.post(f"/lancamentos/{lanc.id}/alternar?formato=tabela")
    assert resposta.status_code == 200
    assert resposta.text.lstrip().startswith("<tr")
    assert f'id="lanc-tab-{lanc.id}"' in resposta.text
    # sem o parâmetro continua devolvendo o cartão da lista
    resposta = client.post(f"/lancamentos/{lanc.id}/alternar")
    assert f'id="lanc-{lanc.id}"' in resposta.text
