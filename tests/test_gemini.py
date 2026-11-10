import json

import httpx
import pytest

from app.importers.base import LeitorErro
from app.services.categorizacao import _modelo_saida
from app.services.gemini import ClienteGemini, schema_para_gemini
from app.services.ia import chamar_parse, cliente_e_modelo_de_texto, obter_provedor
from app.models import set_config

MSG = [{"role": "user", "content": [{"type": "text", "text": "classifique"}]}]


def _cliente(resposta: dict, status: int = 200, capturado: list | None = None) -> ClienteGemini:
    def tratar(req: httpx.Request) -> httpx.Response:
        if capturado is not None:
            capturado.append(req)
        return httpx.Response(status, json=resposta)

    return ClienteGemini("AIzaXYZ", httpx.Client(transport=httpx.MockTransport(tratar)))


def _ok(itens):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"itens": itens})}]}}]}


def test_schema_sem_ref_sem_enum_numerico_e_com_nullable():
    esquema = schema_para_gemini(_modelo_saida([1, 2, 3]))
    texto = json.dumps(esquema)
    assert "$ref" not in texto and "$defs" not in texto and "anyOf" not in texto and "title" not in texto
    item = esquema["properties"]["itens"]["items"]
    assert item["properties"]["categoria_id"] == {"type": "integer", "nullable": True}
    assert set(item["required"]) == {"descricao", "categoria_id"}


def test_parse_devolve_o_mesmo_formato_da_anthropic_e_manda_chave_no_header():
    reqs: list = []
    cliente = _cliente(_ok([{"descricao": "Padaria", "categoria_id": 2}]), capturado=reqs)
    resp = chamar_parse(cliente, modelo="gemini-flash-latest", esforco="low", saida=_modelo_saida([1, 2]),
                        mensagens=MSG)
    assert resp.stop_reason == "end_turn" and resp.parsed_output.itens[0].categoria_id == 2
    req = reqs[0]
    assert req.headers["x-goog-api-key"] == "AIzaXYZ" and req.url.path.endswith("/models/gemini-flash-latest:generateContent")
    corpo = json.loads(req.content)
    assert corpo["contents"][0]["parts"][0]["text"] == "classifique"
    assert corpo["generationConfig"]["responseMimeType"] == "application/json"


def test_resposta_bloqueada_vira_refusal():
    cliente = _cliente({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
    resp = cliente.parse(modelo="m", saida=_modelo_saida([1]), mensagens=MSG)
    assert resp.stop_reason == "refusal" and resp.parsed_output is None


def test_json_invalido_vira_sem_resultado():
    cliente = _cliente({"candidates": [{"content": {"parts": [{"text": "isto não é json"}]}}]})
    assert cliente.parse(modelo="m", saida=_modelo_saida([1]), mensagens=MSG).parsed_output is None


@pytest.mark.parametrize("status,trecho", [(400, "recusou"), (403, "recusou"), (402, "créditos"), (404, "não achou"), (429, "limite")])
def test_erros_http_viram_mensagem_em_portugues(status, trecho):
    with pytest.raises(LeitorErro, match=trecho):
        _cliente({"error": {}}, status=status).parse(modelo="m", saida=_modelo_saida([1]), mensagens=MSG)


def test_sem_conexao():
    def falha(req):
        raise httpx.ConnectError("sem rede")

    cliente = ClienteGemini("k", httpx.Client(transport=httpx.MockTransport(falha)))
    with pytest.raises(LeitorErro, match="Sem conexão"):
        cliente.parse(modelo="m", saida=_modelo_saida([1]), mensagens=MSG)


def test_pdf_nao_vai_para_o_gemini():
    com_pdf = [{"role": "user", "content": [{"type": "document", "source": {}}]}]
    with pytest.raises(LeitorErro, match="Anthropic"):
        _cliente({}).parse(modelo="m", saida=_modelo_saida([1]), mensagens=com_pdf)


def test_provedor_automatico_e_escolhido(session, keyring_falso):
    assert obter_provedor(session) == "anthropic"
    keyring_falso[("app-financas", "gemini")] = "AIzaZZZZ"
    assert obter_provedor(session) == "gemini"  # só tem a chave do Gemini
    keyring_falso[("app-financas", "anthropic")] = "sk-ant-x"
    assert obter_provedor(session) == "anthropic"
    set_config(session, "provedor_ia", "gemini")
    cliente, modelo = cliente_e_modelo_de_texto(session)
    assert isinstance(cliente, ClienteGemini) and modelo == "gemini-3.5-flash"


def test_gemini_sem_chave_avisa(session):
    set_config(session, "provedor_ia", "gemini")
    with pytest.raises(LeitorErro, match="chave da API do Gemini"):
        cliente_e_modelo_de_texto(session)


def test_tela_ia_salva_chave_modelo_e_provedor(client, db, keyring_falso):
    from tests.factories import nova_conta
    nova_conta(db)
    db.commit()
    assert client.post("/configuracoes/ia/gemini/chave", data={"chave": "AIza1234"},
                       follow_redirects=False).status_code == 303
    assert keyring_falso[("app-financas", "gemini")] == "AIza1234"
    client.post("/configuracoes/ia/provedor", data={"provedor": "gemini"})
    client.post("/configuracoes/ia/gemini/modelo", data={"modelo": "gemini-2.5-pro"})
    texto = client.get("/configuracoes?aba=ia").text
    assert "termina em" in texto and "1234" in texto and 'value="gemini-2.5-pro"' in texto
    assert client.post("/configuracoes/ia/provedor", data={"provedor": "xpto"}).status_code == 400
    client.post("/configuracoes/ia/gemini/chave/remover")
    assert ("app-financas", "gemini") not in keyring_falso


def test_503_tenta_modelo_reserva_e_depois_avisa():
    chamados = []

    def tratar(req: httpx.Request) -> httpx.Response:
        modelo = req.url.path.split("/models/")[1].split(":")[0]
        chamados.append(modelo)
        if modelo == "gemini-3.5-flash":
            return httpx.Response(503, json={"error": {"message": "high demand"}})
        return httpx.Response(200, json=_ok([{"descricao": "x", "categoria_id": 1}]))

    cliente = ClienteGemini("k", httpx.Client(transport=httpx.MockTransport(tratar)))
    resp = cliente.parse(modelo="gemini-3.5-flash", saida=_modelo_saida([1]), mensagens=MSG)
    assert chamados == ["gemini-3.5-flash", "gemini-3.1-flash-lite"] and resp.parsed_output.itens[0].categoria_id == 1

    sempre = ClienteGemini("k", httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(503, json={"error": {}}))))
    with pytest.raises(LeitorErro, match="sobrecarregado"):
        sempre.parse(modelo="gemini-3.5-flash", saida=_modelo_saida([1]), mensagens=MSG)


def test_erro_que_nao_e_sobrecarga_nao_tenta_reserva():
    chamados = []

    def tratar(req):
        chamados.append(req.url.path)
        return httpx.Response(403, json={"error": {}})

    cliente = ClienteGemini("k", httpx.Client(transport=httpx.MockTransport(tratar)))
    with pytest.raises(LeitorErro, match="recusou"):
        cliente.parse(modelo="gemini-3.5-flash", saida=_modelo_saida([1]), mensagens=MSG)
    assert len(chamados) == 1
