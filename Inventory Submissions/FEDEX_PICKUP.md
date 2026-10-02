# FedEx pickups (next-business-day Ground)

Schedules FedEx Ground pickups on the regular **Schedule a Pickup** page
(`https://www.fedex.com/shippingplus/en-us/pickup/schedule-pickup`) from today's
`Lowe's M-D-YYYY Output.csv` — the same file the FedEx batch uploads.

**Standalone for now** — the FedEx batch (menu **F**) does not call this.

## What it books

| Pickup | Which SKUs | Address |
|--------|-----------|---------|
| Our Warehouse | Vendors in `warehouse_vendors.json` (incl. Post Protector-Here) | Account default (Lodi) — left as is |
| Post Protector | Vendor **Post Protector** | Changed to Cornerstone Post Protector, 513 Napoleon Rd, Bowling Green OH 43402 (Chris Elliott, 419-352-8688 x1210) |

- Packages = sum of column **AB**; weight = sum of column **AC** (row totals), rounded up. Vendor comes from column **AG** (SKU) via `vendor_map_lowes.xlsx`.
- Ground, next business day (Fri → Mon; if FedEx doesn't offer that day, the next weekday it offers), 10:00 AM – 4:00 PM, "No instructions".
- A pickup with 0 packages is skipped. Other vendors are ignored (listed in the log).
- Booked pickups are logged in `fedex_pickup_state.json`; the same pickup date + location is not booked twice unless you use `--force`.
- Screenshots: `screenshots\fedex_pickup\` (dry run, confirmation, errors).

## Running

Menu **P** in `Run Full Workflow.bat`:

```
1  Schedule both pickups
2  Schedule - Our Warehouse only
3  Schedule - Post Protector only
4  Dry run - both            (fills the form, does NOT submit)
5  Dry run - Our Warehouse
6  Dry run - Post Protector  (checks the address change)
```

Or `Run FedEx Pickups.bat` / `Run FedEx Pickups (Dry Run).bat`, or:

```powershell
cd "Inventory Submissions"
python run_fedex_pickup.py --plan-only                         # totals only, no browser
python run_fedex_pickup.py --location postprotector --dry-run
python run_fedex_pickup.py --location warehouse
python run_fedex_pickup.py --csv "path\to\Lowe's 10-1-2026 Output.csv" --plan-only
```

Other flags: `--pickup-date`, `--force`, `--no-wait`, `--manual-login`, `--skip-auto-login`.

Uses the same FedEx login / browser profile as the batch (`fedex_batch.json`, `.env`).

## Settings

`fedex_pickup.example.json` holds the times, Post Protector address, and which vendor
names count as Post Protector. Copy it to `fedex_pickup.json` to change values locally.
