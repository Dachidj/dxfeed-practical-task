"""Builds tests/fixtures/cme/2030_synthetic_day.xlsx, a tiny made-up file in CME's
"Download data" layout (holiday = Wed 2030-03-13). No real CME data.

    Equities          halted 12:00 CT, reopens 17:00     -> trading_halt 13:00 ET
    Grains            empty holiday cell                  -> closed
    Dairy             PREOPEN until 17:00 reopen          -> closed
    Cryptocurrencies  only the 16:01/16:02 daily break    -> normal trading, no row

Run: python tests/make_cme_fixture.py
"""
from pathlib import Path

import openpyxl

OUT = Path(__file__).resolve().parent / "fixtures" / "cme" / "2030_synthetic_day.xlsx"


def cell(*events: str) -> str:
    return "".join(f"{e} \n" for e in events)


ROWS = [
    ["Asset Class", "2030-03-12", "2030-03-13", "2030-03-14"],
    ["Equities",
     cell("16:00 Trade Date: 2030-03-12 (CLOSED)", "17:00 Trade Date: 2030-03-14 (OPEN)"),
     cell("12:00 Trade Date: 2030-03-14 (PREOPEN)", "17:00 Trade Date: 2030-03-14 (OPEN)"),
     cell("16:00 Trade Date: 2030-03-14 (CLOSED)")],
    ["Grains",
     cell("08:30 Trade Date: 2030-03-12 (OPEN)", "13:30 Trade Date: 2030-03-12 (CLOSED)"),
     "",
     cell("08:30 Trade Date: 2030-03-14 (OPEN)", "13:30 Trade Date: 2030-03-14 (CLOSED)")],
    ["Dairy",
     cell("16:00 Trade Date: 2030-03-12 (CLOSED)", "16:45 Trade Date: 2030-03-14 (PREOPEN)"),
     cell("17:00 Trade Date: 2030-03-14 (OPEN)"),
     cell("16:00 Trade Date: 2030-03-14 (CLOSED)")],
    ["Cryptocurrencies",
     cell("16:00 Trade Date: 2030-03-12 (CLOSED)", "16:02 Trade Date: 2030-03-14 (OPEN)"),
     cell("16:01 Trade Date: 2030-03-14 (PREOPEN)", "16:02 Trade Date: 2030-03-14 (OPEN)"),
     cell("16:00 Trade Date: 2030-03-14 (CLOSED)")],
]


def main() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trading Hours"
    for _ in range(4):          # real exports start with a few blank rows
        ws.append([])
    for r in ROWS:
        ws.append(r)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
