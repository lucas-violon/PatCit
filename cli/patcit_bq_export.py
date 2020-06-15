import os

import typer
from google.cloud import bigquery as bq
from wasabi import Printer

from patcit.config import Config

app = typer.Typer()
msg = Printer()
AUTHORIZED_FORMATS = ["CSV", "NEWLINE_DELIMITED_JSON"]
AUTHORIZED_SUB_TABLES = ["cited_by", "authors", "crossref", "bibl"]
IN_TEXT_SUB_TABLES = ["authors", "bibl"]  # add as in-text get richer content
AUTHORIZED_NPL_FLAVORS = ["front-page", "in-text"]
NESTED_VAR_FP = ["cited_by", "authors", "funder", "subject", "issues"]
NESTED_VAR_IT = ["authors"]  # add as in-text get richer content
AUTHORIZED_CATS = [
    "BIBLIOGRAPHICAL_REFERENCE",
    "PATENT",
    "SEARCH_REPORT",
    "DATABASE",
    "OFFICE_ACTION",
    "WEBPAGE",
    "PRODUCT_DOCUMENTATION",
    "NORM_STANDARD",
    "LITIGATION",
]


def extract_cited_by(src_table):
    query = f"""WITH
      tmp AS (
      SELECT
        npl_publn_id,
        cited_by.origin AS origin_,
        cited_by.publication_number AS publication_number_
      FROM
        `{src_table}`,
        UNNEST(cited_by) AS cited_by)
    SELECT
      npl_publn_id,
      origin,
      publication_number
    FROM
      tmp,
      UNNEST(origin_) AS origin,
      UNNEST(publication_number_) AS publication_number"""
    return query


def extract_authors(flavor, src_table):
    query = f"""
    SELECT
      {"npl_publn_id" if flavor == "front-page" else "publication_number_o"},
      authors.first AS first,
      authors.middle AS middle,
      authors.surname AS surname,
      authors.genname AS genname,
    FROM
      `{src_table}`,
      UNNEST(authors) AS authors
    """
    return query


def extract_crossref(flavor, src_table):
    query = f"""WITH
      tmp AS (
      SELECT
        {"npl_publn_id" if flavor == "front-page" else "publication_number_o"},
        funder.DOI AS DOI,
        funder.award AS award_,
        funder.name AS name,
        subject
      FROM
        `{src_table}`,
        UNNEST(funder) AS funder,
        UNNEST(subject) as subject)
    SELECT
      npl_publn_id,
      DOI,
      award,
      name,
      subject
    FROM
      tmp,
      UNNEST(award_) AS award
    """
    return query


def extract_bibl(flavor, src_table):
    except_var = (
        ",".join(NESTED_VAR_IT) if flavor == "in-text" else ",".join(NESTED_VAR_FP)
    )
    query = f"""
    SELECT
      * EXCEPT ({except_var})
    FROM
      `{src_table}`"""
    return query


def extract_sub_table(flavor, src_table, sub_table, staging_table_ref, client):
    if sub_table not in AUTHORIZED_SUB_TABLES:
        typer.echo(
            f"The sub_table must be in {AUTHORIZED_SUB_TABLES}. destination_format is"
            f" {sub_table}"
        )
        raise typer.Abort()

    if sub_table == "cited_by":
        query = extract_cited_by(src_table)
    elif sub_table == "authors":
        query = extract_authors(flavor, src_table)
    elif sub_table == "crossref":
        query = extract_crossref(flavor, src_table)
    elif sub_table == "bibl":
        query = extract_bibl(flavor, src_table)

    job_config = bq.QueryJobConfig()
    job_config.destination = staging_table_ref
    job_config.write_disposition = bq.WriteDisposition.WRITE_TRUNCATE
    client.query(query, job_config=job_config).result()


def store_table(table_ref, client, destination_format, compression, destination_uri):
    job_config = bq.ExtractJobConfig()
    job_config.compression = bq.Compression.GZIP if compression else bq.Compression.NONE
    job_config.destination_format = destination_format

    destination_format_ = destination_format.split("_")[-1]
    extension = "." + destination_format_.lower() + f"{'.gz' if compression else ''}"
    destination_uri = destination_uri + extension
    with msg.loading():
        client.extract_table(
            source=table_ref, destination_uris=destination_uri, job_config=job_config
        ).result()
    msg.good("Table stored 🚀")


def flatten_npl(
    flavor,
    client,
    destination_format,
    compression,
    src_table,
    staging_table,
    destination_uri,
):
    if flavor not in AUTHORIZED_NPL_FLAVORS:
        typer.echo(
            f"The npl_flavor must be in {AUTHORIZED_NPL_FLAVORS}. npl_flavor is"
            f" {flavor}"
        )
        raise typer.Abort()

    staging_project_id, staging_dataset_id, staging_table_id = staging_table.split(".")
    staging_table_ref = Config(
        project_id=staging_project_id, dataset_id=staging_dataset_id
    ).table_ref(table_id=staging_table_id)

    sub_tables = AUTHORIZED_SUB_TABLES if flavor == "front-page" else IN_TEXT_SUB_TABLES
    for sub_table in sub_tables:
        with msg.loading():
            extract_sub_table(flavor, src_table, sub_table, staging_table_ref, client)
        msg.good(f"Sub-table {sub_table} staged 🚀")
        table_ref = staging_table_ref  # we export the sub-table

        #  a bit of naming
        destination_uri_ = destination_uri.split("/")
        destination_uri_ = os.path.join(
            "/".join(destination_uri_[:-1]), "_".join([sub_table, destination_uri_[-1]])
        )

        store_table(
            table_ref, client, destination_format, compression, destination_uri_
        )


@app.command()
def to_gs(
    src: str,
    dest: str,
    dest_format: str,
    gzip: bool = True,
    npl_flavor: str = None,
    staging_table: str = None,
):
    """
    Export patcit on BQ tables to google Storage

    Notes:
        --dest-format: CSV or NEWLINE_DELIMITED_JSON
        --npl-flavor: 'front-page' or 'in-text'

    E.g. python bin/patcit-cli.py bq export to-gs "npl-parsing.patcit.v02_npl"
    "gs://patcit/npl-latest/v02-npl*" --dest-format "CSV" --compression --staging-table
    "npl-parsing.tmp.tmp" --npl-flavor "front-page"
    """
    project_id, dataset_id, table_id = src.split(".")
    config = Config(project_id=project_id, dataset_id=dataset_id)
    client = config.client()

    if dest_format not in AUTHORIZED_FORMATS:
        typer.echo(
            f"The dest_format must be in {AUTHORIZED_FORMATS}. dest_format is"
            f" {dest_format}"
        )
        raise typer.Abort()

    if dest_format == "CSV" and npl_flavor:
        flatten_npl(npl_flavor, client, dest_format, gzip, src, staging_table, dest)
    else:
        table_ref = config.table_ref(table_id)
        store_table(table_ref, client, dest_format, gzip, dest)


@app.command()
def extract_category(
    cat_table: str,
    category: str,
    staging_table: str = None,
    destination_uri: str = None,
    tls214_table: str = None,
):
    assert category in AUTHORIZED_CATS
    query = f"""
     SELECT
        npl_publn_id,
        npl_class
      FROM
        `{cat_table}`
      WHERE
        npl_class="{category}"
        """
    if tls214_table:
        query = f"""WITH
          tmp AS ({query})
        SELECT
          tmp.npl_publn_id,
          npl_biblio
        FROM
          tmp
        INNER JOIN (
          SELECT
            npl_publn_id,
            npl_biblio
          FROM
            `{tls214_table}`) tls214
        ON
          tls214.npl_publn_id = tmp.npl_publn_id"""

    config = Config()
    client = config.client()
    staging_project, staging_dataset, staging_table = staging_table.split(".")
    staging_table_ref = Config(
        project_id=staging_project, dataset_id=staging_dataset
    ).table_ref(staging_table)

    job_config = bq.QueryJobConfig()
    job_config.destination = staging_table_ref
    job_config.write_disposition = bq.WriteDisposition.WRITE_TRUNCATE
    client.query(query, job_config=job_config).result()

    store_table(
        staging_table_ref, client, "NEWLINE_DELIMITED_JSON", True, destination_uri
    )


if __name__ == "__main__":
    app()