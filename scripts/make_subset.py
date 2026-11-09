"""Gera um subset pequeno e reprodutível do Inside Airbnb (Rio de Janeiro).

Uso:
    python -I scripts/make_subset.py                       # baixa a base e grava data/listings_rio_subset.csv
    python -I scripts/make_subset.py --source listings.csv.gz --out outro.csv

A base completa (~27 MB) é baixada para um diretório temporário, usada só para
gerar o subset e apagada em seguida.

Os tipos NÃO são limpos aqui (preço "$1,234.00", amenities como string JSON,
"t"/"f"): a limpeza é feita dentro do MongoDB, no notebook.
"""
import argparse
import tempfile
import urllib.request
from pathlib import Path

import pandas as pd

SOURCE_URL = "https://data.insideairbnb.com/brazil/rj/rio-de-janeiro/2026-09-22/data/listings.csv.gz"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "listings_rio_subset.csv"

SEED = 42
TOP_N_BAIRROS = 12
POR_BAIRRO = 100
OUTROS = 300

COLUNAS = [
    "id", "name", "description", "neighbourhood_cleansed", "latitude", "longitude",
    "property_type", "room_type", "accommodates", "bedrooms", "beds", "bathrooms_text",
    "amenities", "price", "minimum_nights", "availability_365", "number_of_reviews",
    "review_scores_rating", "review_scores_cleanliness", "review_scores_location",
    "review_scores_value", "host_id", "host_name", "hosts_time_as_host_years", "host_is_superhost",
    "host_listings_count", "host_identity_verified", "first_review", "last_review", "last_scraped",
]


def load_source(source: str) -> pd.DataFrame:
    if not source.startswith(("http://", "https://")):
        return pd.read_csv(source, usecols=COLUNAS, low_memory=False)
    with tempfile.TemporaryDirectory(prefix="airbnb-raw-") as tmp:
        path = Path(tmp) / "listings.csv.gz"
        print(f"Baixando {source} ...")
        urllib.request.urlretrieve(source, path)
        print(f"  {path.stat().st_size / 1e6:.1f} MB (temporário)")
        return pd.read_csv(path, usecols=COLUNAS, low_memory=False)


def make_subset(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["price"].notna() & df["latitude"].notna() & df["longitude"].notna()]
    top = df["neighbourhood_cleansed"].value_counts().head(TOP_N_BAIRROS).index
    partes = [
        df[df["neighbourhood_cleansed"] == b].sample(POR_BAIRRO, random_state=SEED)
        for b in top
    ]
    resto = df[~df["neighbourhood_cleansed"].isin(top)]
    partes.append(resto.sample(min(OUTROS, len(resto)), random_state=SEED))
    return pd.concat(partes).sort_values("id")[COLUNAS]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=SOURCE_URL, help="URL ou caminho local do listings.csv.gz")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="CSV de saída")
    args = ap.parse_args()

    sub = make_subset(load_source(args.source))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sub.to_csv(args.out, index=False)
    print(f"{len(sub)} linhas, {sub['neighbourhood_cleansed'].nunique()} bairros -> {args.out}")


if __name__ == "__main__":
    main()
