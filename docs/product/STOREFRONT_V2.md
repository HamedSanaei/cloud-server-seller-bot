# Storefront V2 — readable catalog, plan details, automatic catalog

## Customer flow

Market → Provider → Product list (6 per page, country flags) → Plan detail
→ Location → OS → Confirmation → Buy.

- Product rows show the country flag (once per country, globe when unknown),
  the spec (`4C/6GB`) and the exact price — never a bare location count.
- The plan-detail screen shows the full spec, only proven technical facts
  (unknown IPv4/IPv6 renders neutrally, never guessed), every availability
  with flags, and the exact monthly price plus the display equivalent.
- Every Telegram `callback_data` fits the 64-byte limit: long screens travel
  under wire aliases (`product_locations` → `pl`, `product_detail` → `pd`)
  and offer identity travels as a reversible 22-char compact reference
  (legacy full-UUID callbacks still decode).

## One-time production configuration

In the server-owned `configuration.toml`:

```toml
[storefront.catalog_sync]
enabled = true
interval_seconds = 900

[storefront.pricing.leaseweb]
mode = "markup"
markup_percent = 25
auto_publish = true

[storefront.pricing.hetzner]
mode = "markup"
markup_percent = 25
auto_publish = true
```

After this, plan changes need no manual commands: the worker refreshes
costs from the official APIs, reprices auto-priced rows in the same
currency (integer minor units, same function as `price-book`), and publishes
eligible rows. Toman/IRT stays display-only via the FX service.

Verify with:

```bash
python -m cloud_platform.cli catalog auto-sync doctor
python -m cloud_platform.cli offers doctor
python -m cloud_platform.cli offers preview --market foreign
```

## Operator control

- `offers disable` records an explicit block future syncs never undo;
  `offers enable` clears it. A disabled production offer stays disabled.
- `offers price` sets a manual price and opts the row out of auto-pricing.
- Automatic publishing requires: provider-reported, explicitly priced,
  not operator-blocked, not deprecated, and `auto_publish = true`.
- A failed provider never blocks another; partial runs never mass-retire;
  overlapping runs serialize on the catalog advisory lock.
