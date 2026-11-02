# ScaleLink's product file

ScaleLink exports its whole product list to a text file, and can import the same format to restore products. This describes the format in our own words, from one real export. Real export files hold real product data, so they never go in this repo.

## File format

- Encoding: UTF-16 little endian, with the byte order mark FF FE at the start.
- Lines end in CR LF. Every line ends that way, including the last.
- Columns are separated by a single TAB. No quoting and no escaping.
- There is no header row.
- One row per product, 15 columns on every row.
- There is no id column and no timestamp column. Row order matched id order on all 484 rows of the export we studied.

## Columns

| Column | Meaning | Seen in the export | Field in the scale's record |
|---|---|---|---|
| 1 | Code | 4 digits | 2 |
| 2 | Description | Up to 35 characters. Some have trailing spaces | 4 |
| 3 | Price | Always 2 decimals | 5 |
| 4 | Type. 1 by weight, 0 by piece | 465 by weight, 19 by piece | 7 |
| 5 | PLU | 4 digits, same as Code on every row | 3 |
| 6 | Unknown | Always 1 | 8 |
| 7 | Unknown | Always 0 | 9 |
| 8 | Tax | 0.0000 on every row but one | 10, see below |
| 9 to 14 | Unknown | Always 0 | 9 to 14 are not all mapped |
| 15 | Image name | Empty on 227 rows, one space on 257 | 6 |

## How it differs from what the scale holds

- Trailing spaces: some descriptions have them in the file. The scale's copy of the same products does not.
- Tax: the one non zero row shows 13.99 in the file and 0.1399 on the scale. The scale's value is the file's value divided by 100. That is one example, so the rule is not proven.
- The file carries no timestamp. The scale's record has one, so an import has to supply it.

## Why row order matters

The scale replaces records by id. The file has no ids, so an import can only number products by their order. If an import gives products new ids, a scale that misses the next full send keeps the old order. One item sends then land in the wrong slots. See `PROTOCOL.md`.
