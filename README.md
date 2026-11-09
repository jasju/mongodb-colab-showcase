# MongoDB + Airbnb Rio: notebook de demonstração

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/jasju/mongodb-colab-showcase/blob/main/mongodb_airbnb_rio.ipynb)

Notebook em PT-BR que demonstra as principais operações do MongoDB sobre um subset do
[Inside Airbnb](https://insideairbnb.com/get-the-data/) do Rio de Janeiro. Foi pensado para o
**MongoDB Atlas free tier (M0)** e para rodar no **Google Colab**.

O notebook cobre CRUD, índices com `explain()`, validação com `$jsonSchema`, aggregation, consultas
geoespaciais (`$geoNear`/`$geoWithin`), busca textual, `$facet`, window functions, `$lookup`, detecção
de outliers, views e transações.

| Arquivo | O que é |
|---|---|
| `mongodb_airbnb_rio.ipynb` | O notebook (gerado por `scripts/build_notebook.py`) |
| `data/listings_rio_subset.csv` | 1.500 anúncios (dump 2026-09-22), 30 colunas, ~1,7 MB. O notebook baixa este arquivo direto do GitHub |
| `scripts/make_subset.py` | Baixa a base completa para um diretório temporário e gera o subset (seed fixa) |
| `scripts/build_notebook.py` | Regera o `.ipynb` |

## Como usar no Google Colab

1. **Atlas:** crie um cluster M0 e um *Database User* (usuário e senha) e copie a connection string
   (*Connect → Drivers*).
2. **Credencial da Admin API**, para o notebook incluir o IP do Colab na IP Access List. Escolha uma das opções:
   - **Service Account** (recomendado): *Organization → Access Manager → Service Accounts*, com role
     *Project Owner* no projeto → `ATLAS_CLIENT_ID` / `ATLAS_CLIENT_SECRET`;
   - **API Key** (legado): *Project → Access Manager → API Keys*, role *Project Owner* →
     `ATLAS_PUBLIC_KEY` / `ATLAS_PRIVATE_KEY`.
   - Se a organização exigir *IP Access List for the Atlas Administration API*, a credencial só aceita
     chamadas de IPs liberados. Nesse caso, desative a exigência ou crie a credencial sem essa restrição,
     porque o IP do Colab muda a cada sessão.
3. Abra o notebook pelo badge acima.
4. **Colab → 🔑 Secrets:** cadastre `MONGODB_URI`, o par de credenciais e, opcionalmente, `ATLAS_PROJECT_ID`
   (*Project Settings → Project ID*). Ligue *Notebook access* em cada um.
5. Rode tudo (*Runtime → Run all*). O CSV é baixado do repositório automaticamente.

A entrada criada na access list é temporária (expira em 6 h). A última célula remove a entrada na hora.

## Regenerando os artefatos

```bash
pip install pandas nbformat
```

```bash
python -I scripts/make_subset.py
```

```bash
python -I scripts/build_notebook.py mongodb_airbnb_rio.ipynb
```

O `make_subset.py` aceita `--source` (URL ou arquivo `.csv.gz` local) e `--out`.

## Rodando localmente (sem Atlas)

```bash
docker run -d --rm --name mongo-demo -p 27017:27017 mongo:8 --replSet rs0
```

```bash
docker exec mongo-demo mongosh --quiet --eval 'rs.initiate()'
```

```bash
MONGODB_URI='mongodb://localhost:27017/?directConnection=true' jupyter lab mongodb_airbnb_rio.ipynb
```

Fora do Colab, os secrets vêm de variáveis de ambiente com os mesmos nomes. Sem as credenciais da Admin API,
a etapa da access list é pulada. Se existir `data/listings_rio_subset.csv` local (clone do repositório),
ele é usado no lugar do download.

Dados: [Inside Airbnb](https://insideairbnb.com/), licença CC BY 4.0.
