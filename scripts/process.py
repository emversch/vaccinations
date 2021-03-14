"""Fetch CSV and compute daily numbers."""
import json
import locale
import pandas as pd
import requests
import typer
import os
import re
import unicodedata
from os import path
from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from functools import lru_cache
locale.setlocale(locale.LC_ALL, "nl_BE")

CSV_ENDPOINT = "https://www.laatjevaccineren.be/vaccination-info/get"


def slugify(value: str, allow_unicode: bool = False) -> str:
    """Create a slug from a given string."""
    if allow_unicode:
        value = unicodedata.normalize("NFKC", value)
    else:
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s\-.]", "", value).strip().lower()
    return re.sub(r"[\s\-.]+", "-", value)


def data_path(date_of_file: date) -> str:
    """Construct absolute path of a CSV file."""
    data_dir = path.realpath(path.join(path.dirname(path.realpath(__file__)), "..", "data"))
    return path.join(data_dir, f"vaccinations_{date_of_file:%Y-%m-%d}.csv")


def json_path(municipality: str) -> str:
    """Construct absolute path of the JSON output."""
    output_dir = path.realpath(path.join(path.dirname(path.realpath(__file__)), "..", "website", "data"))
    return path.join(output_dir, f"numbers_{slugify(municipality)}.json")


def fetch(date_to_store: date, endpoint: str = CSV_ENDPOINT) -> None:
    """
    The 'vaccinatieteller' updates the Open Data csv file on a daily basis, except for weekend days.

    CSV File endpoint: https://www.laatjevaccineren.be/vaccination-info/get
    Explanation of the CSV file: https://www.laatjevaccineren.be/toelichting-csv-bestand-open-data
    """
    output_path = data_path(date_to_store)
    if path.exists(output_path):
        raise IOError(f"File already exists {output_path}")
    result = requests.get(endpoint)
    with open(output_path, "w") as f:
        f.write(result.text)


def load_range(start_date: date, end_date: date) -> pd.DataFrame:
    """Load a range of CSV files into a DataFrame."""
    date_range = pd.date_range(start=start_date, end=end_date).tolist()
    dfs = []

    last_date = start_date
    for d in date_range:
        df = None
        try:
            df = pd.read_csv(data_path(d))
            last_date = d
        except FileNotFoundError:
            # If it fails re-use the previous DataFrame. There is no data for the weekends for
            # example
            if len(dfs) > 0:
                df = dfs[-1].copy(deep=True)
        if df is not None:
            df["DATE"] = d
            dfs.append(df)

    all_df = pd.concat(dfs)
    all_df.last_date = last_date
    return all_df


@lru_cache(maxsize=1)
def load_config() -> pd.DataFrame:
    """Load municipality config into a DataFrame."""
    data_dir = path.realpath(path.join(path.dirname(path.realpath(__file__)), "..", "data"))
    config_path = path.join(data_dir, "config.csv")
    return pd.read_csv(config_path)


def crunch_history(df: pd.DataFrame) -> Dict[str, Any]:
    """."""
    grouped = df.groupby("DATE", as_index=False).agg({
        "POPULATION_NBR": sum,
        "VACCINATED_FIRST_DOSIS_NBR": sum,
        "VACCINATED_SECOND_DOSIS_NBR": sum
    }).sort_values(by="DATE", ascending=True)
    grouped["VACCINATED_ONE_DOSIS_NBR"] = grouped.apply(
        lambda r: r["VACCINATED_FIRST_DOSIS_NBR"] + r["VACCINATED_SECOND_DOSIS_NBR"], axis=1)

    last_date = pd.Timestamp(sorted(df["DATE"].unique(), reverse=True)[0])
    last_df = df[df["DATE"] == last_date]
    population = int(last_df["POPULATION_NBR"].fillna(0).sum())
    total_first_dose = int(last_df["VACCINATED_FIRST_DOSIS_NBR"].fillna(0).sum())
    total_second_dose = int(last_df["VACCINATED_SECOND_DOSIS_NBR"].fillna(0).sum())

    return {
        "population": population,
        "minimum_one_dose": total_first_dose + total_second_dose,
        "first_dose": total_first_dose,
        "second_dose": total_second_dose,
        "timeseries_minimum_one_dose": grouped["VACCINATED_ONE_DOSIS_NBR"].tolist(),
        "timeseries_second_dose": grouped["VACCINATED_SECOND_DOSIS_NBR"].tolist(),
        "timeseries_percentage_minimum_one_dose": grouped["VACCINATED_ONE_DOSIS_NBR"].apply(
            lambda v: round(v / population * 100, 2)).tolist(),
        "timeseries_percentage_second_dose": grouped["VACCINATED_SECOND_DOSIS_NBR"].apply(
            lambda v: round(v / population * 100, 2)).tolist()
    }


def crunch_per_age(df: pd.DataFrame) -> Dict[str, Any]:
    """Compute numbers per municipality."""

    def re_arrange(v):
        if v in ["0-9", "10-19"]:
            return "0-19"
        if v in ["20-29", "30-39"]:
            return "20-39"
        if v in ["40-49", "50-59"]:
            return "40-59"
        if v in ["60-69", "70-79"]:
            return "60-79"
        return "80+"

    df["AGE_CD"] = df.apply(lambda row: re_arrange(row["AGE_CD"]), axis=1)
    last_date = pd.Timestamp(sorted(df["DATE"].unique(), reverse=True)[0])
    df = df[df["DATE"] == last_date]
    # print(df)
    df_ages = df.groupby("AGE_CD", as_index=False).agg({
        'POPULATION_NBR': sum,
        'VACCINATED_FIRST_DOSIS_NBR': sum,
        'VACCINATED_SECOND_DOSIS_NBR': sum
    })
    df_ages = df_ages.sort_values(by='AGE_CD', ascending=False)
    population = df_ages['POPULATION_NBR'].values.tolist()
    first_dose = df_ages['VACCINATED_FIRST_DOSIS_NBR'].values.tolist()
    second_dose = df_ages['VACCINATED_SECOND_DOSIS_NBR'].values.tolist()

    return {
        "population": population,
        "first_dose": first_dose,
        "second_dose": second_dose,
        "percentage_first_dose": [round(100*v/population[i], 2) for i, v in enumerate(first_dose)],
        "percentage_second_dose": [round(100*v/population[i], 2) for i, v in enumerate(second_dose)]
    }


def crunch(df: pd.DataFrame, start_date: date, end_date: date, municipality: str) -> Dict[str, Any]:
    """."""
    date_range = pd.date_range(start=start_date, end=end_date).tolist()
    # labels = [f"{d:%d-%m}" for d in date_range]
    labels = [f"{d:%d-%b}" for d in date_range]
    mdf = df[df["MUNICIPALITY"] == municipality]
    config = load_config()
    entry = config[config["MUNICIPALITY"] == municipality]["INHABITANTS"]
    province = mdf["PROVINCE"].tolist()[0]
    zone = mdf["EERSTELIJNSZONE"].tolist()[0]
    inhabitants = entry.values[0] if len(entry.values) else "inwoners"

    return {
        # Timeseries: historical numbers, all ages
        "history_all": {
            "labels": labels,
            **crunch_history(mdf)
        },
        # Timeseries: historical numbers for adults (18+)
        "history_adults": {
            "labels": labels,
            **crunch_history(mdf[mdf["ADULT_FL(18+)"] == 1])
        },
        # Numbers per age
        "per_age": {
            "labels": ["80+", "60-79", "40-59", "20-39", "0-19"],
            **crunch_per_age(mdf.copy())
        },
        "municipality": municipality,
        "province": province,
        "zone": zone,
        "inhabitants": inhabitants,
        "last_date": f"{df.last_date:%d/%m/%Y}"
    }


def municipalities(df: pd.DataFrame) -> List[str]:
    """Return a list of all available municipalities."""
    return df["MUNICIPALITY"].unique().tolist()


def create_content(df: pd.DataFrame) -> None:
    """Create Hugo content folders for each municipality."""
    ms = municipalities(df)
    content_path = path.realpath(path.join(path.dirname(path.realpath(__file__)), "..", "website", "content"))
    for municipality in ms:
        slug = slugify(municipality)
        dir_path = path.join(content_path, slug)
        index_path = path.join(content_path, slug, "_index.md")
        screenshots_path = path.join(content_path, slug, "screenshots.md")
        os.makedirs(dir_path, exist_ok=True)
        if not path.exists(index_path):
            with open(index_path, "w") as fh_index:
                fh_index.write("---\nlayout: municipality\n---")
        if not path.exists(screenshots_path):
            with open(screenshots_path, "w") as fh_screenshots:
                fh_screenshots.write("---\nlayout: screenshots\n---")


cli = typer.Typer()


@cli.command(name="content")
def do_content() -> None:
    """Create municipality directories."""
    _start_date = date(2021, 2, 24)
    _end_date = date.today() - timedelta(days=1)
    print(f"Loading data from {_start_date} to {_end_date}")
    df = load_range(_start_date, _end_date)
    print("Creating directories ...")
    create_content(df)


@cli.command(name="fetch")
# def do_fetch(date_to_fetch: str = "01-03-2021") -> None:
# def do_fetch(date_to_fetch: str = typer.Argument(f"{date.today():%d-%m-%Y}")) -> None:
def do_fetch(date_to_fetch: str = typer.Argument(...)) -> None:
    """Fetch a CSV file."""
    dt = datetime.strptime(date_to_fetch, "%d-%m-%Y")
    typer.echo(f"Fetching {dt.date()}")
    fetch(dt.date())


@cli.command(name="crunch")
def do_crunch() -> None:
    """Compute timeseries & store results to a JSON file."""
    # _start_date = date(2021, 1, 11)
    _start_date = date(2021, 2, 25)
    _end_date = date.today() - timedelta(days=1)
    print(f"Loading data from {_start_date} to {_end_date}")
    df = load_range(_start_date, _end_date)
    print(f"Crunch daily numbers")
    ms = municipalities(df)
    for municipality in ms:
    # for municipality in ["Lommel"]:
        data = crunch(df, _start_date, _end_date, municipality)
        jp = json_path(municipality)
        print(f"Store JSON: {jp}")
        # print(data)
        json.dump(data, open(jp, "w"), indent=4)
    print(f"Processed: {len(ms)}")


if __name__ == "__main__":
    cli()
