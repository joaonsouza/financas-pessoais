# App de Finanças

App local de finanças pessoais (FastAPI + SQLite + HTMX). Roda só em `127.0.0.1`, sem login: não exponha a porta na rede.

## Rodar

Dê dois cliques em `iniciar.bat` (ou rode `uv run uvicorn app.main:criar_app --factory --host 127.0.0.1 --port 8000`).

- Banco: `data\financas.db` na raiz do repo, ignorado pelo git (a variável `FINANCAS_DB` muda o caminho).
- Backup diário: pasta configurável em Configurações › Backup (padrão `Z:\Meu Drive\Finanças\App\app-financas-backups`).

## Demonstração

Para mostrar o app sem expor os dados reais: dois cliques em `iniciar_demo.bat`. Ele cria `data\demo.db` com uma pessoa
fictícia (dados novos a cada início; `uv run python -m scripts.gerar_demo --semente N` repete um conjunto) e sobe em
`http://127.0.0.1:8001`, com a faixa "Demonstração", sem backup e sem IA. O app de verdade (porta 8000) não é tocado.

## Desenvolvimento

```bash
uv sync
uv run pytest -q
cmd //c build_css.bat   # regera app/web/static/app.css depois de mexer em templates
```

O Tailwind standalone fica em `tools/tailwindcss.exe` (fora do git). Para baixar:
`curl -L -o tools/tailwindcss.exe https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe`
