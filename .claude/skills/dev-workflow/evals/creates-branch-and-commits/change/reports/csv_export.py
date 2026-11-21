"""Exportação do relatório mensal em CSV."""

import csv


def write_report(rows, path):
    # Planilhas em pt-BR leem `;` como separador e a vírgula como decimal.
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["data", "cliente", "total"])
        for row in rows:
            writer.writerow([row["date"], row["customer"], f"{row['total']:.2f}".replace(".", ",")])
