"""Gera mongodb_airbnb_rio.ipynb.

Uso: python -I scripts/build_notebook.py mongodb_airbnb_rio.ipynb
"""
import sys

import nbformat as nbf

cells = []


def md(s):
    cells.append(nbf.v4.new_markdown_cell(s.strip("\n")))


def code(s):
    cells.append(nbf.v4.new_code_cell(s.strip("\n")))


# ---------------------------------------------------------------- 0. Intro
md(r"""
# MongoDB na prática com Airbnb Rio de Janeiro 🏖️

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/jasju/mongodb-colab-showcase/blob/main/mongodb_airbnb_rio.ipynb)

Este notebook demonstra as **principais operações do MongoDB** (CRUD, índices, validação de schema,
aggregation pipeline, transações) e **queries avançadas** (geoespacial, busca textual, `$facet`,
window functions, `$lookup`, detecção de outliers) sobre um subset do
[Inside Airbnb](https://insideairbnb.com/get-the-data/) do Rio de Janeiro (dump de 2026-09-22).

**Base:** [`data/listings_rio_subset.csv`](https://github.com/jasju/mongodb-colab-showcase/blob/main/data/listings_rio_subset.csv),
baixado automaticamente do repositório, com 1.500 anúncios (100 de cada um dos 12 bairros com mais anúncios
e 300 aleatórios dos demais), 30 colunas. Os tipos estão *crus* de propósito (preço `"$1,234.00"`,
booleanos `"t"/"f"`, datas como texto). A limpeza é feita **dentro do MongoDB**.

**Pensado para o Atlas free tier (M0):** 512 MB de storage e operações limitadas. O dataset inteiro com
índices ocupa poucos MB.

### Antes de rodar no Colab
Em **🔑 Secrets** (barra lateral), cadastre e habilite *Notebook access*. Não é preciso enviar o CSV:
o notebook baixa o arquivo do GitHub.


| Secret | Obrigatório | Descrição |
|---|---|---|
| `MONGODB_URI` | ✅ | Connection string `mongodb+srv://usuario:senha@cluster.xxxxx.mongodb.net/` |
| `ATLAS_CLIENT_ID` + `ATLAS_CLIENT_SECRET` | um dos pares | **Service Account** (recomendado) |
| `ATLAS_PUBLIC_KEY` + `ATLAS_PRIVATE_KEY` | um dos pares | **API Key** programática (legado) |
| `ATLAS_PROJECT_ID` | opcional | ID do projeto (Group ID). Se faltar, é descoberto quando a credencial enxerga um único projeto |

A credencial da Admin API precisa da role **Project Owner** (ou *Project IP Access List Admin*)
no projeto, para poder editar a IP Access List.
""")

# ---------------------------------------------------------------- 1. Setup
md("## 1. Setup")
code(r"""
%pip install -q "pymongo>=4.8" requests pandas matplotlib
""")
code(r"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from pprint import pprint

import matplotlib.pyplot as plt
import pandas as pd
import requests
from requests.auth import HTTPDigestAuth
from pymongo import MongoClient, ASCENDING, DESCENDING, TEXT, GEOSPHERE, UpdateOne, InsertOne, DeleteOne
from pymongo.errors import DuplicateKeyError, WriteError, OperationFailure

try:
    from google.colab import userdata  # type: ignore
    RUNNING_IN_COLAB = True
except ImportError:
    RUNNING_IN_COLAB = False

pd.set_option("display.max_colwidth", 60)
print("Rodando no Colab:", RUNNING_IN_COLAB)
""")

# ---------------------------------------------------------------- 2. Secrets
md(r"""
## 2. Secrets

No Colab os valores vêm de `google.colab.userdata`. Fora do Colab (por exemplo, rodando localmente) o
notebook usa variáveis de ambiente com os mesmos nomes.
""")
code(r"""
def get_secret(name, required=False):
    value = None
    if RUNNING_IN_COLAB:
        try:
            value = userdata.get(name)
        except Exception:  # SecretNotFoundError / NotebookAccessError
            value = None
    value = value or os.environ.get(name)
    if required and not value:
        raise RuntimeError(
            f"Secret '{name}' não encontrado. Cadastre-o em 🔑 Secrets e habilite 'Notebook access'."
        )
    return value

MONGODB_URI = get_secret("MONGODB_URI", required=True)
ATLAS_CLIENT_ID = get_secret("ATLAS_CLIENT_ID")
ATLAS_CLIENT_SECRET = get_secret("ATLAS_CLIENT_SECRET")
ATLAS_PUBLIC_KEY = get_secret("ATLAS_PUBLIC_KEY")
ATLAS_PRIVATE_KEY = get_secret("ATLAS_PRIVATE_KEY")
ATLAS_PROJECT_ID = get_secret("ATLAS_PROJECT_ID")

HAS_ADMIN_CREDS = bool((ATLAS_CLIENT_ID and ATLAS_CLIENT_SECRET) or (ATLAS_PUBLIC_KEY and ATLAS_PRIVATE_KEY))
print("MONGODB_URI:", MONGODB_URI.split("@")[-1])  # mostra só o host, nunca a senha
print("Credencial da Admin API:",
      "Service Account" if ATLAS_CLIENT_ID else "API Key" if ATLAS_PUBLIC_KEY else "nenhuma")
""")

# ---------------------------------------------------------------- 3. Access list
md(r"""
## 3. Liberando o IP do Colab na IP Access List (Atlas Admin API v2)

Cada runtime do Colab sai para a internet por um IP diferente, e o Atlas recusa conexões de IPs que não
estão na *IP Access List* do projeto. Então:

1. descobrimos o IP público do runtime (`api.ipify.org`);
2. autenticamos na Admin API, via **OAuth2 client credentials** (Service Account) ou **HTTP Digest**
   (API Key);
3. fazemos `POST /api/atlas/v2/groups/{groupId}/accessList` com `deleteAfterDate`: a entrada é
   **temporária** e o Atlas a remove sozinho depois de 6 horas;
4. consultamos `GET .../accessList/{ip}/status` até ficar `ACTIVE`.
""")
code(r"""
ATLAS_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_ACCEPT = "application/vnd.atlas.2023-01-01+json"


class AtlasAdmin:
    # Cliente mínimo da Atlas Admin API v2

    def __init__(self, client_id=None, client_secret=None, public_key=None, private_key=None):
        self.s = requests.Session()
        self.s.headers.update({"Accept": ATLAS_ACCEPT})
        if client_id and client_secret:
            r = requests.post(
                "https://cloud.mongodb.com/api/oauth/token",
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=30,
            )
            self._check(r)
            self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        elif public_key and private_key:
            self.s.auth = HTTPDigestAuth(public_key, private_key)
        else:
            raise RuntimeError("Nenhuma credencial da Admin API configurada.")

    @staticmethod
    def _check(r):
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text
            raise RuntimeError(f"Atlas API {r.request.method} {r.url} -> {r.status_code}: {detail}")

    def call(self, method, path, body=None):
        headers = {"Content-Type": ATLAS_ACCEPT} if body is not None else {}
        r = self.s.request(method, ATLAS_BASE + path, json=body, headers=headers, timeout=30)
        self._check(r)
        return r.json() if r.content else None

    def resolve_project_id(self, project_id=None):
        if project_id:
            return project_id
        projetos = self.call("GET", "/groups")["results"]
        if len(projetos) != 1:
            nomes = [(p["name"], p["id"]) for p in projetos]
            raise RuntimeError(f"Defina o secret ATLAS_PROJECT_ID. Projetos visíveis: {nomes}")
        return projetos[0]["id"]

    def add_ip(self, project_id, ip, hours=6, comment=None):
        delete_after = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0)
        body = [{
            "ipAddress": ip,
            "comment": comment or f"colab {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
            "deleteAfterDate": delete_after.isoformat().replace("+00:00", "Z"),
        }]
        return self.call("POST", f"/groups/{project_id}/accessList", body)

    def wait_active(self, project_id, ip, timeout=180):
        t0 = time.time()
        while time.time() - t0 < timeout:
            status = self.call("GET", f"/groups/{project_id}/accessList/{ip}/status")["STATUS"]
            print(f"  status: {status}")
            if status == "ACTIVE":
                return True
            if status == "FAILED":
                raise RuntimeError("Atlas reportou FAILED ao aplicar a regra.")
            time.sleep(5)
        raise TimeoutError("A entrada da access list não ficou ACTIVE a tempo.")

    def remove_ip(self, project_id, ip):
        return self.call("DELETE", f"/groups/{project_id}/accessList/{ip}")
""")
code(r"""
PUBLIC_IP = None
atlas = None

if HAS_ADMIN_CREDS:
    PUBLIC_IP = requests.get("https://api.ipify.org", timeout=15).text.strip()
    print("IP público do runtime:", PUBLIC_IP)

    atlas = AtlasAdmin(ATLAS_CLIENT_ID, ATLAS_CLIENT_SECRET, ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY)
    ATLAS_PROJECT_ID = atlas.resolve_project_id(ATLAS_PROJECT_ID)
    print("Projeto:", ATLAS_PROJECT_ID)

    resp = atlas.add_ip(ATLAS_PROJECT_ID, PUBLIC_IP)
    print("Entradas na access list:", resp["totalCount"])
    atlas.wait_active(ATLAS_PROJECT_ID, PUBLIC_IP)
elif RUNNING_IN_COLAB:
    raise RuntimeError("No Colab é preciso configurar a credencial da Admin API (veja a tabela de Secrets).")
else:
    print("Sem credenciais da Admin API fora do Colab: pulando a etapa da access list.")
""")

# ---------------------------------------------------------------- 4. Connect
md("## 4. Conectando ao cluster")
code(r"""
client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=20_000, appname="colab-airbnb-demo")
print(client.admin.command("ping"))
print("Versão do servidor:", client.server_info()["version"])

db = client["airbnb_demo"]
""")

# ---------------------------------------------------------------- 5. Import
md(r"""
## 5. Importando o CSV para `listings_raw`

O `insert_many` grava os documentos como vieram do CSV, com campos ausentes (NaN) simplesmente omitidos.
Isso é natural em um banco *schemaless*.

A única transformação feita em Python é o `amenities`. Ele vem como texto JSON com escapes Unicode
(`"–"`), e o MQL não tem um parser de JSON.
""")
code(r"""
DATA_URL = "https://raw.githubusercontent.com/jasju/mongodb-colab-showcase/main/data/listings_rio_subset.csv"

# Usa uma cópia local se houver (clone do repositório / execução offline); senão baixa do GitHub
CANDIDATOS = [Path("data/listings_rio_subset.csv"), Path("listings_rio_subset.csv")]
csv_path = next((p for p in CANDIDATOS if p.exists()), None)

if csv_path is None:
    csv_path = (Path("/content") if RUNNING_IN_COLAB else Path.cwd()) / "listings_rio_subset.csv"
    r = requests.get(DATA_URL, timeout=60)
    r.raise_for_status()
    csv_path.write_bytes(r.content)
    print(f"Baixado de {DATA_URL} ({len(r.content) / 1e6:.1f} MB)")

df = pd.read_csv(csv_path)
print(df.shape)
df.head(3)
""")
code(r"""
def to_doc(row):
    doc = {k: v for k, v in row.items() if pd.notna(v)}
    doc["amenities"] = json.loads(doc.get("amenities", "[]"))
    return doc

docs = [to_doc(r) for r in df.to_dict("records")]

db.listings_raw.drop()  # torna a célula idempotente
res = db.listings_raw.insert_many(docs)
print("Inseridos:", len(res.inserted_ids))
pprint(db.listings_raw.find_one({}, {"_id": 0, "description": 0, "amenities": {"$slice": 5}}))
""")

# ---------------------------------------------------------------- 6. Clean
md(r"""
## 6. Limpeza e modelagem com aggregation pipeline (`$merge`)

Transformamos `listings_raw` na coleção final `listings`:

- `_id` passa a ser o `id` do Airbnb;
- `price`: `"$1,234.00"` → `1234.0` (`$replaceAll` + `$toDouble`);
- `"t"/"f"` → booleanos; textos de data → `Date` (`$dateFromString`);
- `bathrooms_text` (`"1.5 shared baths"`) → `bathrooms: 1.5` + `bathroom_shared: true` (`$regexFind`);
- latitude/longitude → **GeoJSON** `location: {type: "Point", coordinates: [lng, lat]}`;
- os campos do host e das avaliações viram **subdocumentos embutidos** `host{}` e `reviews{scores{}}`.
""")
code(r"""
to_date = lambda f: {"$dateFromString": {"dateString": f, "format": "%Y-%m-%d", "onNull": "$$REMOVE", "onError": "$$REMOVE"}}

pipeline_limpeza = [
    {"$project": {
        "_id": "$id",
        "name": 1,
        "description": 1,
        "neighbourhood": "$neighbourhood_cleansed",
        "property_type": 1,
        "room_type": 1,
        "accommodates": 1,
        "bedrooms": 1,
        "beds": 1,
        "bathrooms": {"$let": {
            "vars": {"m": {"$regexFind": {"input": "$bathrooms_text", "regex": r"^[\d.]+"}}},
            "in": {"$cond": [
                {"$ifNull": ["$$m", False]},
                {"$toDouble": "$$m.match"},
                {"$cond": [{"$regexMatch": {"input": {"$ifNull": ["$bathrooms_text", ""]}, "regex": "half", "options": "i"}}, 0.5, "$$REMOVE"]},
            ]},
        }},
        "bathroom_shared": {"$regexMatch": {"input": {"$ifNull": ["$bathrooms_text", ""]}, "regex": "shared", "options": "i"}},
        "amenities": 1,
        "price": {"$toDouble": {"$replaceAll": {"input": {"$replaceAll": {"input": "$price", "find": {"$literal": "$"}, "replacement": ""}}, "find": ",", "replacement": ""}}},
        "minimum_nights": 1,
        "availability_365": 1,
        "location": {"type": "Point", "coordinates": ["$longitude", "$latitude"]},
        "host": {
            "id": "$host_id",
            "name": "$host_name",
            "years_as_host": "$hosts_time_as_host_years",
            "is_superhost": {"$eq": ["$host_is_superhost", "t"]},
            "identity_verified": {"$eq": ["$host_identity_verified", "t"]},
            "listings_count": "$host_listings_count",
        },
        "reviews": {
            "count": "$number_of_reviews",
            "first": to_date("$first_review"),
            "last": to_date("$last_review"),
            "scores": {
                "rating": "$review_scores_rating",
                "cleanliness": "$review_scores_cleanliness",
                "location": "$review_scores_location",
                "value": "$review_scores_value",
            },
        },
        "last_scraped": to_date("$last_scraped"),
    }},
    {"$merge": {"into": "listings", "on": "_id", "whenMatched": "replace", "whenNotMatched": "insert"}},
]

db.listings.drop()
db.listings_raw.aggregate(pipeline_limpeza)
print("listings:", db.listings.count_documents({}))
pprint(db.listings.find_one({"reviews.count": {"$gt": 50}}, {"description": 0, "amenities": {"$slice": 5}}))
""")
code(r"""
# Conferindo os tipos resultantes com $type
list(db.listings.aggregate([
    {"$limit": 1},
    {"$project": {"_id": 0, "price": {"$type": "$price"}, "location": {"$type": "$location"},
                  "superhost": {"$type": "$host.is_superhost"}, "amenities": {"$type": "$amenities"},
                  "first_review": {"$type": "$reviews.first"}}},
]))
""")

# ---------------------------------------------------------------- 7. CRUD
md("## 7. CRUD\n\n### 7.1 Leitura (`find`)")
code(r"""
# Filtro + projeção + ordenação + limite
cur = (db.listings
       .find({"neighbourhood": "Ipanema", "room_type": "Entire home/apt", "price": {"$lte": 600}},
             {"_id": 0, "name": 1, "price": 1, "bedrooms": 1, "reviews.scores.rating": 1})
       .sort("price", DESCENDING)
       .limit(5))
pd.json_normalize(list(cur))
""")
code(r"""
# Operadores: $in, $gte, $exists, $regex, $all, $size, $or
filtros = {
    "bairros $in": {"neighbourhood": {"$in": ["Leblon", "Ipanema"]}},
    "sem nota ($exists: false)": {"reviews.scores.rating": {"$exists": False}},
    "nome com 'vista' ($regex)": {"name": {"$regex": "vista", "$options": "i"}},
    "Wifi E Pool ($all)": {"amenities": {"$all": ["Wifi", "Pool"]}},
    "mais de 60 amenities ($expr + $size)": {"$expr": {"$gt": [{"$size": "$amenities"}, 60]}},
    "superhost OU nota >= 4.95 ($or)": {"$or": [{"host.is_superhost": True}, {"reviews.scores.rating": {"$gte": 4.95}}]},
}
for nome, f in filtros.items():
    print(f"{nome:40s} {db.listings.count_documents(f):5d}")
""")
code(r"""
# $elemMatch em array de strings: algum amenity que case com a regex
db.listings.count_documents({"amenities": {"$elemMatch": {"$regex": "^Beach access"}}})
""")
code(r"""
# distinct + paginação com skip/limit
print(sorted(db.listings.distinct("room_type")))
pagina = 2
list(db.listings.find({}, {"name": 1}).sort("_id").skip((pagina - 1) * 3).limit(3))
""")
md("### 7.2 Criação, atualização e remoção")
code(r"""
demo = {
    "_id": "demo-1",
    "name": "Apê demo em Botafogo",
    "neighbourhood": "Botafogo",
    "room_type": "Entire home/apt",
    "price": 350.0,
    "amenities": ["Wifi", "Kitchen"],
    "location": {"type": "Point", "coordinates": [-43.1822, -22.9519]},
}
db.listings.delete_many({"_id": {"$in": ["demo-1", "demo-2", "demo-3"]}})
print(db.listings.insert_one(demo).inserted_id)
print(db.listings.insert_many([{**demo, "_id": "demo-2"}, {**demo, "_id": "demo-3"}]).inserted_ids)
""")
code(r"""
# update_one com vários operadores
r = db.listings.update_one(
    {"_id": "demo-1"},
    {"$set": {"price": 380.0, "host.name": "Ana"},
     "$inc": {"accommodates": 2},             # cria o campo se não existir
     "$push": {"amenities": "Air conditioning"},
     "$addToSet": {"tags": "demo"},           # não duplica
     "$currentDate": {"updated_at": True}},
)
print(r.matched_count, r.modified_count)
db.listings.update_one({"_id": "demo-1"}, {"$pull": {"amenities": "Kitchen"}, "$unset": {"host.name": ""}})
db.listings.find_one({"_id": "demo-1"})
""")
code(r"""
# update_many: marca como "luxo" os anúncios acima de R$ 2.000
r = db.listings.update_many({"price": {"$gt": 2000}}, {"$addToSet": {"tags": "luxo"}})
print("marcados como luxo:", r.modified_count)

# upsert: atualiza se existir, senão insere
r = db.listings.update_one({"_id": "demo-4"}, {"$set": {**{k: v for k, v in demo.items() if k != "_id"}, "price": 999.0}}, upsert=True)
print("upserted_id:", r.upserted_id)

# find_one_and_update devolve o documento (antes ou depois)
from pymongo import ReturnDocument
db.listings.find_one_and_update({"_id": "demo-4"}, {"$mul": {"price": 0.9}},
                                projection={"price": 1}, return_document=ReturnDocument.AFTER)
""")
code(r"""
# replace_one troca o documento inteiro (exceto _id)
db.listings.replace_one({"_id": "demo-3"}, {**demo, "_id": "demo-3", "name": "Substituído", "price": 1.0})

# bulk_write: várias operações em uma única ida ao servidor
r = db.listings.bulk_write([
    UpdateOne({"_id": "demo-2"}, {"$set": {"price": 420.0}}),
    InsertOne({**demo, "_id": "demo-5"}),
    DeleteOne({"_id": "demo-3"}),
], ordered=True)
print(r.bulk_api_result)

# limpeza
print("removidos:", db.listings.delete_many({"_id": {"$regex": "^demo-"}}).deleted_count)
db.listings.update_many({"tags": "luxo"}, {"$unset": {"tags": ""}})
print("total:", db.listings.count_documents({}))
""")

# ---------------------------------------------------------------- 8. Indexes
md(r"""
## 8. Índices e `explain()`

Sem índice o MongoDB faz `COLLSCAN` e examina todos os documentos. Para o índice composto seguimos a regra
**ESR** (*Equality, Sort, Range*): primeiro o campo de igualdade, depois o de ordenação, depois o de faixa.
""")
code(r"""
def resumo_explain(cursor):
    plan = cursor.explain()
    stages = []
    def walk(n):
        if isinstance(n, dict):
            if "stage" in n:
                stages.append(n["stage"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(plan["queryPlanner"]["winningPlan"])
    es = plan.get("executionStats", {})
    return {"stages": stages, "nReturned": es.get("nReturned"),
            "docsExamined": es.get("totalDocsExamined"), "keysExamined": es.get("totalKeysExamined")}

for ix in db.listings.list_indexes():
    if ix["name"] != "_id_":
        db.listings.drop_index(ix["name"])

consulta = lambda: db.listings.find({"neighbourhood": "Copacabana", "price": {"$gte": 300, "$lte": 600}}).sort("price", ASCENDING)
print("ANTES :", resumo_explain(consulta()))

db.listings.create_index([("neighbourhood", ASCENDING), ("price", ASCENDING)], name="bairro_preco")
print("DEPOIS:", resumo_explain(consulta()))
""")
code(r"""
# Multikey (índice em array), texto, geoespacial, parcial
db.listings.create_index("amenities", name="amenities_multikey")
db.listings.create_index([("name", TEXT), ("description", TEXT)],
                         weights={"name": 3, "description": 1},
                         default_language="portuguese", name="texto_nome_desc")
db.listings.create_index([("location", GEOSPHERE)], name="geo_location")
db.listings.create_index([("reviews.scores.rating", DESCENDING)],
                         partialFilterExpression={"reviews.count": {"$gte": 10}},
                         name="nota_parcial_10_reviews")

print(resumo_explain(db.listings.find({"amenities": "Pool"})))

# O índice parcial só é usado quando o filtro garante a condição do partialFilterExpression
print(resumo_explain(db.listings.find({"reviews.scores.rating": {"$gte": 4.9}, "reviews.count": {"$gte": 20}})))
print(resumo_explain(db.listings.find({"reviews.scores.rating": {"$gte": 4.9}})))
""")
code(r"""
# unique: coleção de reservas (usada também na seção de transações)
db.reservations.drop()
db.reservations.create_index("codigo", unique=True)

# TTL: documentos expiram sozinhos (um monitor em background remove a cada ~60s)
db.eventos.drop()
db.eventos.create_index("created_at", expireAfterSeconds=3600)
db.eventos.insert_one({"tipo": "page_view", "created_at": datetime.now(timezone.utc)})

pd.DataFrame([{"nome": ix["name"], "chave": dict(ix["key"]),
               "extras": {k: v for k, v in ix.items() if k not in ("v", "key", "name", "weights", "textIndexVersion", "language_override")}}
              for ix in db.listings.list_indexes()])
""")
code(r"""
stats = db.command("collStats", "listings")
print(f"documentos: {stats['count']}, dados: {stats['size']/1024:.0f} KB, índices: {stats['totalIndexSize']/1024:.0f} KB")
""")

# ---------------------------------------------------------------- 9. Schema validation
md(r"""
## 9. Validação de schema (`$jsonSchema`)

Mesmo sendo flexível, o MongoDB pode exigir uma estrutura mínima. Com `validationLevel: "moderate"`, os
documentos que já existem e não seguem o schema continuam válidos até serem atualizados.
""")
code(r"""
validator = {"$jsonSchema": {
    "bsonType": "object",
    "required": ["name", "neighbourhood", "room_type", "price", "location"],
    "properties": {
        "name": {"bsonType": "string", "minLength": 3},
        "price": {"bsonType": ["double", "int", "long", "decimal"], "minimum": 0},
        "room_type": {"enum": db.listings.distinct("room_type")},
        "location": {
            "bsonType": "object",
            "required": ["type", "coordinates"],
            "properties": {
                "type": {"enum": ["Point"]},
                "coordinates": {"bsonType": "array", "minItems": 2, "maxItems": 2},
            },
        },
        "amenities": {"bsonType": "array", "items": {"bsonType": "string"}},
    },
}}
db.command("collMod", "listings", validator=validator, validationLevel="moderate", validationAction="error")

invalido = {"_id": "bad-1", "name": "X", "neighbourhood": "Centro", "room_type": "Castelo", "price": "barato",
            "location": {"type": "Point", "coordinates": [-43.18, -22.90]}}
try:
    db.listings.insert_one(invalido)
except WriteError as e:
    print("Rejeitado ✅")
    for regra in e.details["errInfo"]["details"]["schemaRulesNotSatisfied"]:
        for p in regra.get("propertiesNotSatisfied", []):
            print(" -", p["propertyName"], "→", p["details"][0].get("reason", p["details"][0]))
""")

# ---------------------------------------------------------------- 10. Basic aggregation
md(r"""
## 10. Aggregation pipeline: o básico

`$match` → `$group` → `$sort` → `$project`. `$median` e `$percentile` (MongoDB 7.0+) são mais robustos que a
média para preço, que tem cauda longa.
""")
code(r"""
por_bairro = list(db.listings.aggregate([
    {"$match": {"room_type": "Entire home/apt"}},
    {"$group": {
        "_id": "$neighbourhood",
        "anuncios": {"$sum": 1},
        "preco_medio": {"$avg": "$price"},
        "preco_mediano": {"$median": {"input": "$price", "method": "approximate"}},
        "nota_media": {"$avg": "$reviews.scores.rating"},
    }},
    {"$match": {"anuncios": {"$gte": 30}}},
    {"$sort": {"preco_mediano": -1}},
    {"$project": {"_id": 0, "bairro": "$_id", "anuncios": 1,
                  "preco_medio": {"$round": ["$preco_medio", 0]},
                  "preco_mediano": {"$round": ["$preco_mediano", 0]},
                  "nota_media": {"$round": ["$nota_media", 2]}}},
]))
df_bairro = pd.DataFrame(por_bairro)
df_bairro
""")
code(r"""
# $unwind: as amenities mais comuns
pd.DataFrame(db.listings.aggregate([
    {"$unwind": "$amenities"},
    {"$group": {"_id": "$amenities", "n": {"$sum": 1}}},
    {"$sort": {"n": -1}},
    {"$limit": 10},
    {"$project": {"_id": 0, "amenity": "$_id", "n": 1,
                  "pct": {"$round": [{"$multiply": [{"$divide": ["$n", db.listings.count_documents({})]}, 100]}, 1]}}},
]))
""")

# ---------------------------------------------------------------- 11. Advanced
md("## 11. Queries avançadas sobre a base\n\n### 11.1 Geoespacial: `$geoNear`, anúncios perto de pontos turísticos")
code(r"""
PONTOS = {
    "Candelária (Centro)": [-43.1777, -22.9007],
    "Arpoador": [-43.1907, -22.9886],
    "Posto 5 (Copacabana)": [-43.1857, -22.9711],
    "Escadaria Selarón": [-43.1794, -22.9153],
}

linhas = []
for ponto, coords in PONTOS.items():
    res = list(db.listings.aggregate([
        {"$geoNear": {
            "near": {"type": "Point", "coordinates": coords},
            "distanceField": "dist_m",
            "maxDistance": 1000,          # metros
            "query": {"room_type": "Entire home/apt"},
            "spherical": True,
        }},
        {"$group": {"_id": None, "n": {"$sum": 1}, "preco_mediano": {"$median": {"input": "$price", "method": "approximate"}},
                    "mais_perto_m": {"$min": "$dist_m"}}},
    ]))
    r = res[0] if res else {"n": 0, "preco_mediano": None, "mais_perto_m": None}
    linhas.append({"ponto": ponto, "anuncios_1km": r["n"], "preco_mediano": r["preco_mediano"],
                   "mais_perto_m": round(r["mais_perto_m"]) if r["mais_perto_m"] else None})
pd.DataFrame(linhas)
""")
code(r"""
# Os 5 mais próximos do Arpoador, com a distância
pd.DataFrame(db.listings.aggregate([
    {"$geoNear": {"near": {"type": "Point", "coordinates": PONTOS["Arpoador"]}, "distanceField": "dist_m", "spherical": True}},
    {"$limit": 5},
    {"$project": {"_id": 0, "name": 1, "neighbourhood": 1, "price": 1, "dist_m": {"$round": ["$dist_m", 0]}}},
]))
""")
code(r"""
# $geoWithin com um polígono aproximado da orla Ipanema + Leblon (GeoJSON: [lng, lat], anel fechado)
orla_ipanema_leblon = {"type": "Polygon", "coordinates": [[
    [-43.2330, -22.9880], [-43.1915, -22.9890], [-43.1990, -22.9800],
    [-43.2250, -22.9790], [-43.2330, -22.9880],
]]}
pd.DataFrame(db.listings.aggregate([
    {"$match": {"location": {"$geoWithin": {"$geometry": orla_ipanema_leblon}}}},
    {"$group": {"_id": "$neighbourhood", "n": {"$sum": 1}, "preco_mediano": {"$median": {"input": "$price", "method": "approximate"}}}},
    {"$sort": {"n": -1}},
]))
""")

md(r"""
### 11.2 Busca textual (`$text`)

O índice de texto usa o stemming do português: "varandas" casa com "varanda". Frases entre aspas exigem
o trecho exato e `-palavra` exclui termos.
""")
code(r"""
def busca(q, n=5):
    return pd.DataFrame(db.listings.find(
        {"$text": {"$search": q}},
        {"_id": 0, "score": {"$meta": "textScore"}, "name": 1, "neighbourhood": 1, "price": 1},
    ).sort([("score", {"$meta": "textScore"})]).limit(n))

busca("vista mar varanda")
""")
code(r"""
busca('"vista para o mar" -hostel')
""")

md("### 11.3 Amenities que \"valem\" mais: preço mediano com vs. sem cada amenity")
code(r"""
total = db.listings.count_documents({"room_type": "Entire home/apt"})
pd.DataFrame(db.listings.aggregate([
    {"$match": {"room_type": "Entire home/apt"}},
    {"$setWindowFields": {"output": {"mediana_geral": {
        "$median": {"input": "$price", "method": "approximate"},
        "window": {"documents": ["unbounded", "unbounded"]}}}}},
    {"$unwind": "$amenities"},
    {"$group": {"_id": "$amenities", "n": {"$sum": 1},
                "mediana_com": {"$median": {"input": "$price", "method": "approximate"}},
                "mediana_geral": {"$first": "$mediana_geral"}}},
    {"$match": {"n": {"$gte": 30, "$lte": total * 0.6}}},   # nem raras nem onipresentes
    {"$set": {"premio_pct": {"$round": [{"$multiply": [{"$subtract": [{"$divide": ["$mediana_com", "$mediana_geral"]}, 1]}, 100]}, 0]}}},
    {"$sort": {"premio_pct": -1}},
    {"$limit": 10},
    {"$project": {"_id": 0, "amenity": "$_id", "n": 1, "mediana_com": 1, "premio_pct": 1}},
]))
""")

md("### 11.4 `$facet` + `$bucket` + `$bucketAuto`: um \"dashboard\" em uma única consulta")
code(r"""
dash = next(db.listings.aggregate([
    {"$facet": {
        "faixas_preco": [
            {"$bucket": {"groupBy": "$price", "boundaries": [0, 200, 400, 700, 1000, 2000],
                         "default": "2000+", "output": {"n": {"$sum": 1}}}},
        ],
        "tipos_quarto": [{"$sortByCount": "$room_type"}],
        "top_bairros": [{"$sortByCount": "$neighbourhood"}, {"$limit": 5}],
        "notas_quintis": [
            {"$match": {"reviews.scores.rating": {"$exists": True}}},
            {"$bucketAuto": {"groupBy": "$reviews.scores.rating", "buckets": 5}},
        ],
        "resumo": [{"$group": {"_id": None, "anuncios": {"$sum": 1},
                               "superhosts": {"$sum": {"$cond": ["$host.is_superhost", 1, 0]}},
                               "p90_preco": {"$percentile": {"input": "$price", "p": [0.9], "method": "approximate"}}}}],
    }},
]))
for k, v in dash.items():
    print(f"\n== {k}")
    for item in v:
        print("  ", item)
""")

md(r"""
### 11.5 Window functions (`$setWindowFields`)

Para cada anúncio calculamos o ranking de preço dentro do bairro, a mediana do bairro e quanto o anúncio
está acima ou abaixo dela, sem perder o nível de detalhe (diferente do `$group`).
""")
code(r"""
pd.DataFrame(db.listings.aggregate([
    {"$match": {"room_type": "Entire home/apt", "neighbourhood": {"$in": ["Copacabana", "Ipanema", "Leblon"]}}},
    {"$setWindowFields": {
        "partitionBy": "$neighbourhood",
        "sortBy": {"price": -1},
        "output": {
            "rank_preco": {"$rank": {}},
            "n_bairro": {"$count": {}, "window": {"documents": ["unbounded", "unbounded"]}},
            "mediana_bairro": {"$median": {"input": "$price", "method": "approximate"},
                               "window": {"documents": ["unbounded", "unbounded"]}},
        },
    }},
    {"$match": {"rank_preco": {"$lte": 3}}},
    {"$project": {"_id": 0, "neighbourhood": 1, "rank_preco": 1, "n_bairro": 1, "name": 1, "price": 1,
                  "x_mediana": {"$round": [{"$divide": ["$price", "$mediana_bairro"]}, 1]}}},
]))
""")
code(r"""
# Média móvel: preço mediano por "safra" (ano da primeira avaliação), suavizado com janela de 3 anos
pd.DataFrame(db.listings.aggregate([
    {"$match": {"reviews.first": {"$exists": True}}},
    {"$group": {"_id": {"$year": "$reviews.first"}, "n": {"$sum": 1},
                "preco_mediano": {"$median": {"input": "$price", "method": "approximate"}}}},
    {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {
        "media_movel_3a": {"$avg": "$preco_mediano", "window": {"documents": [-2, 0]}}}}},
    {"$project": {"_id": 0, "ano_1a_review": "$_id", "n": 1, "preco_mediano": 1,
                  "media_movel_3a": {"$round": ["$media_movel_3a", 0]}}},
]))
""")

md(r"""
### 11.6 `$lookup`: coleção de hosts e junção com os anúncios

Primeiro materializamos uma coleção `hosts` com `$group` + `$merge`, que funciona como uma *materialized
view*. Depois usamos `$lookup` com sub-pipeline para trazer os 3 anúncios mais caros de cada host.
""")
code(r"""
db.hosts.drop()
db.listings.aggregate([
    {"$group": {"_id": "$host.id", "name": {"$first": "$host.name"},
                "is_superhost": {"$max": "$host.is_superhost"},
                "anuncios_no_subset": {"$sum": 1},
                "bairros": {"$addToSet": "$neighbourhood"}}},
    {"$merge": {"into": "hosts", "whenMatched": "replace"}},
])
print("hosts:", db.hosts.count_documents({}))

pd.DataFrame(db.hosts.aggregate([
    {"$match": {"anuncios_no_subset": {"$gte": 4}}},
    {"$lookup": {
        "from": "listings",
        "let": {"hid": "$_id"},
        "pipeline": [
            {"$match": {"$expr": {"$eq": ["$host.id", "$$hid"]}}},
            {"$sort": {"price": -1}},
            {"$limit": 3},
            {"$project": {"_id": 0, "name": 1, "price": 1}},
        ],
        "as": "top_anuncios",
    }},
    {"$project": {"_id": 0, "name": 1, "is_superhost": 1, "anuncios_no_subset": 1,
                  "n_bairros": {"$size": "$bairros"}, "top_anuncios": 1}},
    {"$sort": {"anuncios_no_subset": -1}},
]))
""")
code(r"""
# Superhosts têm nota maior? (agrupando por um campo do subdocumento embutido)
pd.DataFrame(db.listings.aggregate([
    {"$match": {"reviews.count": {"$gte": 10}}},
    {"$group": {"_id": "$host.is_superhost", "n": {"$sum": 1},
                "nota": {"$avg": "$reviews.scores.rating"},
                "limpeza": {"$avg": "$reviews.scores.cleanliness"},
                "preco_mediano": {"$median": {"input": "$price", "method": "approximate"}}}},
    {"$project": {"_id": 0, "superhost": "$_id", "n": 1, "nota": {"$round": ["$nota", 2]},
                  "limpeza": {"$round": ["$limpeza", 2]}, "preco_mediano": 1}},
]))
""")

md("### 11.7 Outliers de preço por tipo de quarto (regra do IQR com `$percentile`)")
code(r"""
pd.DataFrame(db.listings.aggregate([
    {"$setWindowFields": {"partitionBy": "$room_type", "output": {
        "q": {"$percentile": {"input": "$price", "p": [0.25, 0.75], "method": "approximate"},
              "window": {"documents": ["unbounded", "unbounded"]}}}}},
    {"$set": {"q1": {"$arrayElemAt": ["$q", 0]}, "q3": {"$arrayElemAt": ["$q", 1]}}},
    {"$set": {"limite_sup": {"$add": ["$q3", {"$multiply": [1.5, {"$subtract": ["$q3", "$q1"]}]}]}}},
    {"$match": {"$expr": {"$gt": ["$price", "$limite_sup"]}}},
    {"$group": {"_id": "$room_type", "outliers": {"$sum": 1}, "limite_sup": {"$first": "$limite_sup"},
                "maior_preco": {"$max": "$price"}}},
    {"$sort": {"outliers": -1}},
]))
""")

md("### 11.8 View: anúncios \"custo-benefício\"")
code(r"""
db.drop_collection("vw_custo_beneficio")
db.create_collection("vw_custo_beneficio", viewOn="listings", pipeline=[
    {"$match": {"reviews.count": {"$gte": 20}, "reviews.scores.rating": {"$gte": 4.8},
                "room_type": "Entire home/apt", "price": {"$lte": 400}}},
    {"$project": {"name": 1, "neighbourhood": 1, "price": 1, "rating": "$reviews.scores.rating",
                  "n_reviews": "$reviews.count"}},
])
# A view é consultada como uma coleção comum (somente leitura) e é recalculada a cada consulta
pd.DataFrame(db.vw_custo_beneficio.find({}, {"_id": 0}).sort([("rating", -1), ("n_reviews", -1)]).limit(8))
""")

# ---------------------------------------------------------------- 12. Transactions
md(r"""
## 12. Transações multi-documento

Uma reserva insere um documento em `reservations` e decrementa `availability_365` do anúncio, de forma
atômica. A segunda tentativa reaproveita o mesmo `codigo` (índice unique), então a transação inteira
sofre **rollback** e o `$inc` também é desfeito.
""")
code(r"""
alvo = db.listings.find_one({"availability_365": {"$gte": 30}}, {"availability_365": 1, "name": 1})
print("Antes:", alvo["availability_365"])

def reservar(session, codigo, noites):
    db.listings.update_one({"_id": alvo["_id"], "availability_365": {"$gte": noites}},
                           {"$inc": {"availability_365": -noites}}, session=session)
    db.reservations.insert_one({"codigo": codigo, "listing_id": alvo["_id"], "noites": noites,
                                "criada_em": datetime.now(timezone.utc)}, session=session)

with client.start_session() as s:
    s.with_transaction(lambda sess: reservar(sess, "RSV-001", 3))
print("Depois da reserva OK:", db.listings.find_one({"_id": alvo["_id"]})["availability_365"])

try:
    with client.start_session() as s:
        s.with_transaction(lambda sess: reservar(sess, "RSV-001", 5))   # código duplicado
except DuplicateKeyError:
    print("Transação abortada (código duplicado) ↩️")
print("Depois do rollback:", db.listings.find_one({"_id": alvo["_id"]})["availability_365"],
      "| reservas:", db.reservations.count_documents({}))
""")

# ---------------------------------------------------------------- 13. Viz
md("## 13. Visualizando os resultados")
code(r"""
fig, axes = plt.subplots(1, 2, figsize=(15, 6))

d = df_bairro.sort_values("preco_mediano")
axes[0].barh(d["bairro"], d["preco_mediano"], color="#2a9d8f")
axes[0].set_title("Preço mediano por noite: imóvel inteiro (R$)")
axes[0].set_xlabel("R$")

pts = pd.DataFrame(db.listings.aggregate([
    {"$project": {"_id": 0, "lng": {"$arrayElemAt": ["$location.coordinates", 0]},
                  "lat": {"$arrayElemAt": ["$location.coordinates", 1]}, "price": 1}},
]))
sc = axes[1].scatter(pts["lng"], pts["lat"], c=pts["price"].clip(upper=pts["price"].quantile(0.95)),
                     s=8, cmap="viridis", alpha=0.7)
for nome, (lng, lat) in PONTOS.items():
    axes[1].annotate(nome, (lng, lat), fontsize=8, color="crimson")
    axes[1].plot(lng, lat, "x", color="crimson")
axes[1].set_title("Anúncios no mapa (cor = preço, limitado ao p95)")
axes[1].set_aspect("equal")
fig.colorbar(sc, ax=axes[1], label="R$")
plt.tight_layout()
plt.show()
""")

# ---------------------------------------------------------------- 14. Cleanup
md(r"""
## 14. Limpeza

A entrada da access list expira sozinha (`deleteAfterDate`), mas também dá para removê-la já. Para apagar
os dados da demonstração, descomente o `drop_database`.
""")
code(r"""
# client.drop_database("airbnb_demo")

if atlas and PUBLIC_IP:
    atlas.remove_ip(ATLAS_PROJECT_ID, PUBLIC_IP)
    print(f"IP {PUBLIC_IP} removido da access list")

client.close()
""")

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
    "colab": {"provenance": []},
}
nbf.write(nb, sys.argv[1])
print(f"{len(cells)} células -> {sys.argv[1]}")
