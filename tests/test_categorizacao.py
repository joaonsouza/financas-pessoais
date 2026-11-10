"""Categorização: regras, histórico, dica do banco, IA em lote (cliente falso) e aprendizado por correção."""
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select

from app.models import RegraCategoria
from app.services.categorizacao import (
    MAPA_DICAS, criar_regra_de_correcao, sugerir_categoria, sugerir_categorias,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento


@pytest.fixture
def cats(session):
    nomes = ["Mercado", "Feira/Açougue/Padaria", "Comer fora/Delivery", "Farmácia", "Combustível/Apps", "Roupas",
             "Lazer", "Outros variáveis"]
    return {n: nova_categoria(session, n) for n in nomes}


class ClienteFalso:
    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.chamadas.append(kwargs)
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _ia(*pares, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, parsed_output=SimpleNamespace(
        itens=[SimpleNamespace(descricao=d, categoria_id=c) for d, c in pares]))


# ---------------------------------------------------------------- regras

def test_regra_contem_sobre_descricao_normalizada(session, cats):
    session.add(RegraCategoria(padrao="UBER", categoria_id=cats["Combustível/Apps"].id))
    session.flush()
    sg = sugerir_categoria(session, "Uber *Trip 03/12")
    assert (sg.categoria_id, sg.origem) == (cats["Combustível/Apps"].id, "regra")
    assert sugerir_categoria(session, "Loja do Zé").categoria_id is None


def test_regra_de_maior_prioridade_e_depois_a_mais_especifica_vencem(session, cats):
    session.add_all([
        RegraCategoria(padrao="IFOOD", categoria_id=cats["Comer fora/Delivery"].id, prioridade=0),
        RegraCategoria(padrao="IFOOD MERCADO", categoria_id=cats["Mercado"].id, prioridade=0),
        RegraCategoria(padrao="MERCADO", categoria_id=cats["Outros variáveis"].id, prioridade=5),
    ])
    session.flush()
    assert sugerir_categoria(session, "IFD*iFood Mercado").categoria_id == cats["Outros variáveis"].id
    session.query(RegraCategoria).filter_by(padrao="MERCADO").delete()
    assert sugerir_categoria(session, "IFD*iFood Mercado").categoria_id == cats["Mercado"].id
    assert sugerir_categoria(session, "iFood restaurante").categoria_id == cats["Comer fora/Delivery"].id


# ---------------------------------------------------------------- histórico

def test_historico_usa_a_categoria_mais_frequente_da_mesma_descricao(session, cats):
    conta = nova_conta(session)
    for i, cat in enumerate(["Mercado", "Mercado", "Feira/Açougue/Padaria"]):
        novo_lancamento(session, conta, date(2026, 9, 1 + i), -1000, categoria=cats[cat], descricao="Loja do Bairro 02/05")
    novo_lancamento(session, conta, date(2026, 9, 9), -500, descricao="Sem Categoria")
    sg = sugerir_categoria(session, "LOJA DO BAIRRO")
    assert (sg.categoria_id, sg.origem) == (cats["Mercado"].id, "historico")
    assert sugerir_categoria(session, "Sem Categoria").categoria_id is None


def test_historico_ignora_cancelados_e_vence_a_dica(session, cats):
    conta = nova_conta(session)
    novo_lancamento(session, conta, date(2026, 9, 1), -1000, categoria=cats["Lazer"], descricao="Evento X", status="cancelado")
    assert sugerir_categoria(session, "Evento X").categoria_id is None
    novo_lancamento(session, conta, date(2026, 9, 2), -1000, categoria=cats["Lazer"], descricao="Drogaria Y")
    sg = sugerir_categoria(session, "Drogaria Y", dica="SAÚDE")
    assert (sg.categoria_id, sg.origem) == (cats["Lazer"].id, "historico")


def test_regra_vence_o_historico(session, cats):
    conta = nova_conta(session)
    novo_lancamento(session, conta, date(2026, 9, 1), -1000, categoria=cats["Lazer"], descricao="Padaria Sol")
    session.add(RegraCategoria(padrao="PADARIA", categoria_id=cats["Feira/Açougue/Padaria"].id))
    session.flush()
    assert sugerir_categoria(session, "Padaria Sol").origem == "regra"


# ---------------------------------------------------------------- dica do banco

@pytest.mark.parametrize("descricao, dica, esperado", [
    ("SUPERMERCADO BOM PRECO", "ALIMENTAÇÃO", "Mercado"),
    ("ATACADAO CENTRAL", "ALIMENTAÇÃO", "Mercado"),
    ("PANIFICADORA PAO DOURADO", "ALIMENTAÇÃO", "Feira/Açougue/Padaria"),
    ("PIZZARIA FORNO BOM", "ALIMENTAÇÃO", "Comer fora/Delivery"),
    ("DROGARIA SAUDE TOTAL", "SAÚDE", "Farmácia"),
    ("AUTO POSTO ESTRELA", "VEÍCULOS", "Combustível/Apps"),
    ("LOJA DE ROUPAS", "VESTUÁRIO", "Roupas"),
    ("PARQUE DIVERSAO", "HOBBY", "Lazer"),
    ("AIRBNB", "TURISMO E ENTRETENIM.", "Lazer"),
])
def test_dica_do_banco_mapeada(session, cats, descricao, dica, esperado):
    sg = sugerir_categoria(session, descricao, dica=dica)
    assert (sg.categoria_id, sg.origem) == (cats[esperado].id, "dica")


def test_dica_diversos_ou_desconhecida_ou_sem_categoria_correspondente_nao_sugere(session, cats):
    assert sugerir_categoria(session, "LOJA QUALQUER", dica="DIVERSOS").categoria_id is None
    assert sugerir_categoria(session, "LOJA QUALQUER", dica="OUTRA COISA").categoria_id is None
    assert sugerir_categoria(session, "LOJA QUALQUER", dica=None).categoria_id is None
    assert "DIVERSOS" in MAPA_DICAS and MAPA_DICAS["DIVERSOS"] is None
    session.query(type(cats["Farmácia"])).filter_by(nome="Farmácia").delete()
    assert sugerir_categoria(session, "DROGARIA SAUDE TOTAL", dica="SAÚDE").categoria_id is None


def test_categoria_inativa_nao_e_sugerida(session, cats):
    cats["Farmácia"].ativa = False
    session.flush()
    assert sugerir_categoria(session, "DROGARIA SAUDE TOTAL", dica="SAÚDE").categoria_id is None


# ---------------------------------------------------------------- IA em lote

def test_ia_so_para_unicas_sem_sugestao_com_parametros_exatos(session, cats):
    itens = [("DROGARIA SAUDE TOTAL", "SAÚDE"), ("LOJA OBSCURA 01", None), ("LOJA OBSCURA 01", None), ("OUTRA LOJA", "DIVERSOS")]
    cliente = ClienteFalso(_ia(("LOJA OBSCURA 01", cats["Lazer"].id), ("OUTRA LOJA", cats["Roupas"].id)))
    r = sugerir_categorias(session, itens, cliente=cliente, modelo="modelo-x")

    assert len(cliente.chamadas) == 1
    c = cliente.chamadas[0]
    assert c["model"] == "modelo-x" and c["output_config"] == {"effort": "low"} and c["max_tokens"] == 16000
    assert c["betas"] == ["server-side-fallback-2026-07-01"] and c["fallbacks"] == "default"
    texto = c["messages"][0]["content"][0]["text"]
    assert texto.count("LOJA OBSCURA 01") == 1 and "DROGARIA SAUDE TOTAL" not in texto  # única e só as sem sugestão
    esquema = c["output_format"].model_json_schema()
    assert str(cats["Lazer"].id) in str(esquema) and "enum" in str(esquema)  # ids restritos às categorias oferecidas

    assert [(sg.categoria_id, sg.origem) for sg in r.sugestoes] == [
        (cats["Farmácia"].id, "dica"), (cats["Lazer"].id, "ia"), (cats["Lazer"].id, "ia"), (cats["Roupas"].id, "ia")]
    assert r.aviso_ia is None


def test_ia_id_fora_da_lista_ou_nulo_vira_sem_categoria(session, cats):
    cliente = ClienteFalso(_ia(("LOJA A", 99999), ("LOJA B", None)))
    r = sugerir_categorias(session, [("LOJA A", None), ("LOJA B", None)], cliente=cliente)
    assert [sg.categoria_id for sg in r.sugestoes] == [None, None]


def test_ia_nao_e_chamada_sem_cliente_ou_sem_pendentes(session, cats):
    assert sugerir_categorias(session, [("LOJA A", None)]).sugestoes[0].categoria_id is None
    cliente = ClienteFalso()
    r = sugerir_categorias(session, [("DROGARIA SAUDE TOTAL", "SAÚDE")], cliente=cliente)
    assert cliente.chamadas == [] and r.sugestoes[0].origem == "dica"


def test_ia_descricao_sai_mascarada_e_volta_para_a_original(session, cats):
    original = "PIX TRANSF FULANO 123.456.789-09"
    cliente = ClienteFalso(_ia(("PIX TRANSF FULANO ***.***.***-**", cats["Lazer"].id)))
    r = sugerir_categorias(session, [(original, None)], cliente=cliente)
    assert "123.456.789-09" not in cliente.chamadas[0]["messages"][0]["content"][0]["text"]
    assert r.sugestoes[0].categoria_id == cats["Lazer"].id


def test_ia_falha_mantem_o_restante_e_avisa(session, cats):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    erro = anthropic.RateLimitError("x", response=httpx.Response(429, request=req), body=None)
    r = sugerir_categorias(session, [("DROGARIA SAUDE TOTAL", "SAÚDE"), ("LOJA A", None)], cliente=ClienteFalso(erro))
    assert [sg.categoria_id for sg in r.sugestoes] == [cats["Farmácia"].id, None]
    assert "limite de uso" in r.aviso_ia

    r = sugerir_categorias(session, [("LOJA A", None)], cliente=ClienteFalso(_ia(stop="refusal")))
    assert r.aviso_ia and r.sugestoes[0].categoria_id is None


# ---------------------------------------------------------------- aprendizado

def test_criar_regra_de_correcao_cria_atualiza_e_passa_a_valer(session, cats):
    regra = criar_regra_de_correcao(session, "Uber *Trip 03/12", cats["Combustível/Apps"].id)
    assert regra.padrao == "UBER *TRIP" and regra.criada_de == "Uber *Trip 03/12"
    assert sugerir_categoria(session, "UBER *TRIP HELP").origem == "regra"

    de_novo = criar_regra_de_correcao(session, "uber *trip", cats["Lazer"].id, prioridade=3)
    assert de_novo.id == regra.id and (de_novo.categoria_id, de_novo.prioridade) == (cats["Lazer"].id, 3)
    assert len(list(session.scalars(select(RegraCategoria)))) == 1
    assert sugerir_categoria(session, "Uber *Trip").categoria_id == cats["Lazer"].id


def test_criar_regra_valida_entrada(session, cats):
    with pytest.raises(ValueError, match="descrição vazia"):
        criar_regra_de_correcao(session, "   ", cats["Mercado"].id)
    with pytest.raises(ValueError, match="categoria não encontrada"):
        criar_regra_de_correcao(session, "LOJA", 99999)
