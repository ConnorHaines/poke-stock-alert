# Pokémon 30th Celebration stock alerts

Checks UK shops every 2 to 5 minutes for sealed **Pokémon TCG 30th Celebration**
products. When one comes into stock (or opens for pre-order) at a fair price, it
sends a push to your phone. Tap the push to open the shop page, then buy it yourself.

It never adds to basket or buys anything. It's a lookout, not a bot.

## Get the alerts on your phone

1. Install **ntfy** ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iPhone](https://apps.apple.com/app/ntfy/id1625396347)).
2. Tap **+**, leave the server as `ntfy.sh`, and enter the topic name Connor sends you
   (it starts with `pkmn30-`). Treat it like a password: anyone with it gets your alerts.
3. In the topic's settings, turn on **Instant delivery** (Android) so alerts aren't delayed.

You'll get:
- **IN STOCK / PRE-ORDER**: product, shop, price. Tap to open the page.
- **Can't check <shop>**: a shop has stopped answering, so check that one by hand for now.
- **<shop> is back**: checks for that shop are working again.

## What it watches

| Shop | How |
|---|---|
| Magic Madhouse | Each product page (official stockist, sells at RRP) |
| Obsidia TCG | Their 30th Celebration collection |
| Total Cards | Their 30th Celebration collection |
| Zatu | Pokémon new releases, pre-orders, ETBs, sealed and booster pack lists |
| Titan Cards | Shop search for "30th celebration" |

Products: Elite Trainer Box (including the Pokémon Center one), Booster Bundle, Mini Tins
(and 10-tin displays), ex Boxes, ex Tins, 2-Pack Blisters, Booster Packs, Battle Decks,
Binder / Poster / Tech Sticker / Knock Out / Figure / Ditto Premium collections and the
Day & Night Ultra-Premium Collections. English only. Singles, Japanese/Chinese/Korean
product, empty boxes, sleeves and loose tin inserts are ignored.

**Not covered:** Pokémon Center UK, Smyths, Argos, GAME, The Entertainer, Very, HMV,
John Lewis and Amazon. They all put automated visitors behind a bot check, so this
doesn't try. Use their own "notify me" buttons and apps for those.

## Price limits

Each product has a `max_price` in `config.json`, set at about 30% over UK RRP. Listings
above it are ignored so you don't get pinged for £169.99 ETBs. Change the numbers to
suit, or set one to `null` for "tell me at any price".

| Product | UK RRP | Alert up to |
|---|---|---|
| Elite Trainer Box | £49.99 | £65 |
| Pokémon Center ETB | n/a | £85 |
| Ultra-Premium Collection | £169.99 | £200 |
| Ditto Premium Collection | £39.99 | £52 |
| Binder Collection | £37.99 | £50 |
| Figure Collection | £29.99 | £39 |
| Booster Bundle | £24.99 | £33 |
| ex Box / ex Tin | £21.99 | £29 |
| Poster Collection | £19.99 | £26 |
| Battle Deck | £19.99 | £26 |
| Tech Sticker Collection | £16.99 | £23 |
| Mini Tin | £12.99 | £17 |
| 2-Pack Blister / Knock Out | £9.99 | £14 |
| Booster Pack | about £5 | £7 |

## How it runs

GitHub Actions runs `watcher.py` in the cloud, so no computer needs to stay on. Each run
checks for 55 minutes and, as it starts, queues the next run, which takes over the moment
it ends. A 15-minute schedule restarts the chain if it ever breaks (GitHub's schedule is
too unreliable to rely on alone). What it last saw is saved in `state.json`, so you only
get pinged when something *changes* to in stock, not every two minutes while it stays
in stock.

**To stop it:** on GitHub, open Actions > Stock watch > "..." > **Disable workflow**.
Enable it again and press **Run workflow** to restart.

The ntfy topic is stored as the `NTFY_TOPIC` repository secret, never in the code.

### Run it on a PC instead

```
python watcher.py --once --dry-run       # one check, print what it would send
set NTFY_TOPIC=pkmn30-...                # Windows (export on Mac/Linux)
python watcher.py --loop-minutes 600     # check for 10 hours
```

### Add a shop

- **Shopify shop** (most UK card shops): find its 30th collection URL
  (`/collections/<handle>`) and add a `shopify_collection` entry, or use
  `shopify_search` with a search phrase.
- **Any other shop**: add a `jsonld_pages` entry with product page URLs. It reads the
  standard stock info most shops put in their pages.
