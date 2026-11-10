"""Leitor via IA, mascaramento e configurações da chave. Só cliente falso: nenhuma chamada real à API."""
import base64
from datetime import date
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.importers import pdf_ia
from app.importers.base import LeitorErro
from app.importers.mascaramento import mascarar
from app.importers.pdf_ia import ResultadoIA, TransacaoIA, ler_com_ia
from app.services import ia as servico_ia
from tests.factories import nova_conta


# ---------------------------------------------------------------- utilitários

def _pdf(texto: str) -> bytes:
    """PDF mínimo de 1 página; sem texto, a página fica vazia (simula PDF de imagem)."""
    fluxo = f"BT /F1 12 Tf 50 700 Td ({texto}) Tj ET" if texto else ""
    objetos = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(fluxo)} >>\nstream\n{fluxo}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    saida, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objetos, start=1):
        offsets.append(len(saida))
        saida += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(saida)
    saida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    saida += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    saida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return saida


class ClienteFalso:
    """Imita `cliente.beta.messages.parse`: devolve (ou levanta) as respostas na ordem."""

    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.chamadas.append(kwargs)
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _resposta(parsed=None, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed)


def _ia(**extra) -> ResultadoIA:
    dados = dict(
        banco="bancoteste", tipo_conta="cartao", identificador_conta="1234",
        transacoes=[
            TransacaoIA(data="2026-09-15", descricao="LOJA A", descricao_original="LOJA A 03/04", valor=-1000,
                        parcela_num=3, parcela_total=4),
            TransacaoIA(data="2026-09-20", descricao="LOJA B", descricao_original="LOJA B", valor=-2000),
        ],
        competencia_fatura="2026-10", vencimento="2026-10-10", fechamento="2026-10-03", total_informado=3000,
    )
    dados.update(extra)
    return ResultadoIA(**dados)


def _erro_http(classe, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return classe("falha", response=httpx.Response(status, request=req), body=None)


# ---------------------------------------------------------------- mascaramento

def test_mascara_cpf_com_e_sem_pontos():
    assert mascarar("CPF 123.456.789-09 e 12345678909.") == "CPF ***.***.***-** e ***.***.***-**."


def test_mascara_cartao_mantem_quatro_finais():
    assert mascarar("Cartão 5555 4444 3333 2222") == "Cartão **** **** **** 2222"
    assert mascarar("Cartão 5555-4444-3333-2222 ok") == "Cartão **** **** **** 2222 ok"
    assert mascarar("Final 1234.XXXX.XXXX.5678") == "Final **** **** **** 5678"
    assert mascarar("[************9876]") == "[**** **** **** 9876]"


def test_mascara_agencia_conta_email_e_telefone():
    t = mascarar("Fulano agência: 1234 conta: 56789-0 fulano@exemplo.com.br (61) 99999-8888")
    assert "1234" not in t and "56789" not in t and "fulano@" not in t and "99999-8888" not in t
    assert "agência: ****" in t and "conta: ****" in t and "[e-mail]" in t and "[telefone]" in t
    assert mascarar("ag. 4321 c/c 123456-7") == "ag. **** c/c ****"
    assert mascarar("Ag/Conta 1234/12345-6") == "Ag/Conta ****"


def test_mascara_nao_toca_valores_datas_e_descricoes():
    linha = "15/09/2026 SUPERMERCADO EXEMPLO 03/04 1.234,56 -0,01 saldo em conta 1.234,56"
    assert mascarar(linha) == linha


# ---------------------------------------------------------------- leitura

def test_ler_pdf_com_texto_envia_texto_mascarado_e_parametros_exatos():
    pdf = _pdf("Fatura CPF 123.456.789-09 cartao 5555 4444 3333 2222 LOJA A 10,00")
    cliente = ClienteFalso(_resposta(_ia()))
    r = ler_com_ia(pdf, cliente, modelo="modelo-x")

    assert len(cliente.chamadas) == 1
    c = cliente.chamadas[0]
    assert c["model"] == "modelo-x" and c["max_tokens"] == 16000
    assert c["betas"] == ["server-side-fallback-2026-07-01"] and c["fallbacks"] == "default"
    assert c["output_config"] == {"effort": "medium"} and c["output_format"] is ResultadoIA
    blocos = c["messages"][0]["content"]
    assert [b["type"] for b in blocos] == ["text"]
    enviado = blocos[0]["text"]
    assert "123.456.789-09" not in enviado and "5555 4444 3333" not in enviado
    assert "***.***.***-**" in enviado and "**** **** **** 2222" in enviado and "LOJA A" in enviado

    assert (r.origem, r.banco, r.tipo_conta, r.identificador_conta) == ("pdf_ia", "bancoteste", "cartao", "1234")
    assert [(t.data, t.valor) for t in r.transacoes] == [(date(2026, 9, 15), -1000), (date(2026, 9, 20), -2000)]
    assert (r.transacoes[0].parcela_num, r.transacoes[0].parcela_total) == (3, 4)
    assert (r.vencimento, r.fechamento, r.competencia_fatura, r.total_informado) == (
        date(2026, 10, 10), date(2026, 10, 3), "2026-10", 3000)
    assert r.avisos == ["Lido por IA: confira os lançamentos antes de importar."]


def test_pdf_sem_texto_vai_como_documento_base64_antes_do_texto():
    pdf = _pdf("")
    cliente = ClienteFalso(_resposta(_ia()))
    ler_com_ia(pdf, cliente)
    blocos = cliente.chamadas[0]["messages"][0]["content"]
    assert [b["type"] for b in blocos] == ["document", "text"]
    assert blocos[0]["source"] == {
        "type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(pdf).decode()}


def test_soma_divergente_gera_aviso():
    cliente = ClienteFalso(_resposta(_ia(total_informado=9999)))
    r = ler_com_ia(_pdf("x"), cliente)
    assert any("não bate" in a for a in r.avisos)


def test_recusa_tenta_de_novo_e_depois_funciona():
    cliente = ClienteFalso(_resposta(None, stop="refusal"), _resposta(_ia()))
    r = ler_com_ia(_pdf("x"), cliente)
    assert len(cliente.chamadas) == 2 and len(r.transacoes) == 2


def test_recusa_duas_vezes_da_erro_claro():
    cliente = ClienteFalso(_resposta(None, stop="refusal"), _resposta(None, stop="refusal"))
    with pytest.raises(LeitorErro, match="recusou"):
        ler_com_ia(_pdf("x"), cliente)
    assert len(cliente.chamadas) == 2


def test_saida_invalida_tenta_de_novo_e_depois_erro():
    ruim = _ia(transacoes=[TransacaoIA(data="31/02/2026", descricao="A", descricao_original="A", valor=-1)])
    cliente = ClienteFalso(_resposta(ruim), _resposta(ruim))
    with pytest.raises(LeitorErro, match="inválidos"):
        ler_com_ia(_pdf("x"), cliente)
    assert len(cliente.chamadas) == 2
    cliente = ClienteFalso(_resposta(None), _resposta(_ia()))
    assert len(ler_com_ia(_pdf("x"), cliente).transacoes) == 2


@pytest.mark.parametrize("erro, trecho", [
    (_erro_http(anthropic.RateLimitError, 429), "limite de uso"),
    (_erro_http(anthropic.InternalServerError, 500), "erro 500"),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "Sem conexão"),
])
def test_erros_da_api_viram_leitor_erro(erro, trecho):
    cliente = ClienteFalso(erro)
    with pytest.raises(LeitorErro, match=trecho):
        ler_com_ia(_pdf("x"), cliente)
    assert len(cliente.chamadas) == 1  # erro de API não é refeito aqui


def test_pdf_ilegivel_da_erro():
    with pytest.raises(LeitorErro, match="Não foi possível ler o PDF"):
        ler_com_ia(b"%PDF-1.4 lixo", ClienteFalso())


# ---------------------------------------------------------------- chave e cliente

def test_chave_no_keyring_e_cliente(monkeypatch, keyring_falso):
    criados = []
    monkeypatch.setattr(servico_ia.anthropic, "Anthropic", lambda **kw: criados.append(kw) or "cliente")
    assert servico_ia.criar_cliente() == "cliente" and criados == [{}]  # sem chave: o SDK resolve sozinho

    servico_ia.salvar_chave("  sk-ant-segredo-1234  ")
    assert keyring_falso == {("app-financas", "anthropic"): "sk-ant-segredo-1234"}
    assert servico_ia.ultimos4_da_chave() == "1234"
    servico_ia.criar_cliente()
    assert criados[-1] == {"api_key": "sk-ant-segredo-1234"}

    servico_ia.remover_chave()
    servico_ia.remover_chave()  # remover de novo não falha
    assert servico_ia.obter_chave() is None


def test_chave_vazia_e_rejeitada():
    with pytest.raises(ValueError, match="informe a chave"):
        servico_ia.salvar_chave("   ")


def test_keyring_indisponivel_nao_derruba(monkeypatch):
    def quebra(*a, **k):
        raise RuntimeError("sem backend")

    monkeypatch.setattr(servico_ia.keyring, "get_password", quebra)
    assert servico_ia.obter_chave() is None


# ---------------------------------------------------------------- tela de configurações

def test_aba_ia_salva_mostra_e_remove_chave_sem_ecoar(client, db, keyring_falso):
    _pronto(db)
    assert "Nenhuma chave guardada" in client.get("/configuracoes?aba=ia").text

    r = client.post("/configuracoes/ia/chave", data={"chave": "sk-ant-api03-segredo-AB12"}, follow_redirects=False)
    assert r.status_code == 303
    assert keyring_falso[("app-financas", "anthropic")] == "sk-ant-api03-segredo-AB12"
    pagina = client.get("/configuracoes?aba=ia").text
    assert "Chave configurada" in pagina and "AB12" in pagina
    assert "sk-ant-api03-segredo" not in pagina

    assert client.post("/configuracoes/ia/chave/remover", follow_redirects=False).status_code == 303
    assert keyring_falso == {}
    assert "Nenhuma chave guardada" in client.get("/configuracoes?aba=ia").text


def test_aba_ia_chave_vazia_da_erro(client, db):
    _pronto(db)
    r = client.post("/configuracoes/ia/chave", data={"chave": "  "})
    assert r.status_code == 400 and "informe a chave" in r.text


def test_aba_ia_modelo_padrao_e_alteracao(client, db):
    _pronto(db)
    assert "claude-opus-5-5" in client.get("/configuracoes?aba=ia").text
    assert servico_ia.obter_modelo(db) == "claude-opus-5-5"
    client.post("/configuracoes/ia/modelo", data={"modelo": "claude-modelo-novo"})
    db.expire_all()
    assert servico_ia.obter_modelo(db) == "claude-modelo-novo"
    assert 'value="claude-modelo-novo"' in client.get("/configuracoes?aba=ia").text
    client.post("/configuracoes/ia/modelo", data={"modelo": " "})
    db.expire_all()
    assert servico_ia.obter_modelo(db) == "claude-opus-5-5"


def test_modelo_pydantic_espelha_resultado_leitura():
    from dataclasses import fields

    from app.importers.base import ResultadoLeitura

    campos_ia = set(ResultadoIA.model_fields) | {"origem"}
    assert {f.name for f in fields(ResultadoLeitura)} == campos_ia
    assert pdf_ia.TENTATIVAS == 2


def _pronto(db):
    """As telas exigem o setup concluído (há ao menos uma conta)."""
    nova_conta(db)
    db.commit()
