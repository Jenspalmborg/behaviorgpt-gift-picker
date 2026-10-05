# Gift Picker

Tell it what someone loves ("running, hiking, flowers"), or paste their Pinterest profile, and get three gift ideas picked by [BehaviorGPT](https://github.com/Unbox-AI/behaviorgpt). The interests are played to the model as a short shopping session, and the model predicts what that person would reach for next. Save the ideas you like to get more like them, then share a shortlist so others can vote.

Every recommendation comes from BehaviorGPT. There is no LLM in the loop.

![Birthday gift ideas for Mom: running, hiking, flowers, under $60](docs/screenshots/picks.png)

It is a small, complete example of building on the BehaviorGPT SDK: no catalog to upload, no user data. Just a synthetic history, recommendations and personalized search against one of the pre-embedded catalogs.

**[▶ Try it here](https://jenspalmborg.github.io/behaviorgpt-gift-picker/)**

> Status: working prototype. It runs against the pre-embedded `retail_catalog`, so gift quality depends on what that catalog carries.

## What it shows

| In the app | BehaviorGPT call |
|---|---|
| Choosing the best wording for an interest | `client.complete(history=[Search(q)])` on a few phrasings, keeping the highest score |
| Turning interests into a profile | `Search` + `View` events, one pair per interest |
| "Top match for their whole profile" | `complete` with the profile, ending on a `View` (recommendations) |
| "For their love of …" | `complete` with the profile plus a final `Search` (personalized search) |
| Pinterest import | each board's Pinterest topics become a theme search; pins from every board, weighted by board size, fill the history |
| Occasion and age | the per-interest search says it: `Search("golf birthday gift")` |
| ♥ Save, more like this | `View` + `AddToCart` of the saved product appended to the profile |
| ✕ Not for them | same call, with that product and its near-copies excluded |

## Run it

Needs Python 3.11+, [uv](https://github.com/astral-sh/uv) and a BehaviorGPT API key from [unboxai.com/behaviorgpt](https://unboxai.com/behaviorgpt).

```sh
git clone https://github.com/Jenspalmborg/behaviorgpt-gift-picker.git
cd behaviorgpt-gift-picker
uv sync
cp .env.example .env                # paste your key as UNBOXAI_API_KEY
uv run uvicorn app:app --reload
```

Open http://127.0.0.1:8000. Links like `/?for=Mom&interests=running,hiking,flowers&budget=50&occasion=birthday` (or `/?for=Jane&pinterest=jane/cozy-home`) fill in the form and run the search, which is handy for sharing examples.

Shared shortlists are stored in SQLite at `data/gift-picker.db`; set `GIFT_DB` to put it elsewhere.

Tests run offline against a small fake catalog, so they need no API key:

```sh
uv run pytest
```

## How it works

1. **Pick the wording.** Broad words land badly on their own: in this catalog "video games" returns Nike sneakers and "gold jewelry" returns coffee. So each interest is searched as `X`, `X gift` and `X accessories`, and the phrasing whose top result the model is most confident about wins. If even the best score is below `MIN_MATCH_SCORE`, the interest is skipped and the page says so.
2. **Build a pretend session.** Each interest becomes a `Search` followed by a `View` of its top product, all in one session:
   ```
   Search("running …")  → View(top running product)
   Search("hiking …")   → View(top hiking product)
   Search("flowers …")  → View(top flowers product)
   ```
   The views matter: they tell the model which kind of product in each area this person looked at, not just the words.
3. **Ask for picks.** The top pick comes from the session as is ("what would they want next?"). The other two add one more `Search` for a single interest at the end, so the results are ranked for someone who also likes everything else.
4. **Keep them different.** Picks skip the products already "viewed" in the session, anything over budget, and near-copies of an earlier pick: titles sharing most of their main words in any order ("MASTER FENG Sausage Stuffer" and "Sausage Stuffer – Stainless Steel"). Within one screen, picks also come from different categories.
5. **Refine.** Saving a card adds `View` + `AddToCart` of it at the end of the session. The model leans hard on the latest events (one saved kettle turns every result into kettles), so only that card's slot follows the saves, as "More like what you saved"; the others stay tied to one interest each. ✕ replaces a card from the same interest, skipping near-copies of it (same first words of the title, usually the same brand and line) for the rest of the session. "Show me others" asks again while excluding everything already shown.

**Pinterest.** Public profiles and boards have RSS feeds (`/<user>/feed.rss`, `/<user>/<board>.rss`), so no login or API key is needed. For a profile, the board list and sizes come from the profile page, and 16 pins are split over the boards: one each, the rest by the square root of board size. A 1,000-pin watch board and a 10-pin clothes board come out about 7:1, so the big interest leads without drowning out the rest. Pins are interleaved across boards, so the latest events in the history are a mix. If the board list can't be read, the profile's recent-pins feed is used instead.

**Board themes.** Each board page lists Pinterest's own topics for it, in English whatever the board is called: "Prag" gives "prague czech republic, prague, beautiful places", "Prylar" gives "beautiful bicycle, fixed bike, fixie bike". The first five are each tried as is, as a gift and as accessories (for Prag, the fifth, "places to travel accessories", wins with a luggage scale), and the phrasing with the best score plus fit becomes the board's theme. Fit is the share of the board's other topics (and its pins' matches) whose results include the same category, so a lone outlier loses: Inspo's topics agree on Home & Kitchen, so "home decor" beats the slightly higher-scoring "stairs" (stair treads). Board cards come from the theme ("For their love of fixie bike"), so a board reads as a concept, not as one pin's caption. Themes also go first in the history, one per board.

**Staying on topic.** A card only takes products from the categories its own plain search returned, so with a big food board in the history, the bike board's card still comes back as a bike rather than drifting into the kitchen. If none of the personalized results fits, it uses the theme's own results. The top match follows the same rule (a category one of their interests or themes returns) and counts as the card for whichever board or interest it's closest to, so one watch on screen leaves room for something else.

**Gifts only.** Food is never picked unless it's sold as a gift (gift basket, hamper, sampler): a pad thai "kit" or a 16 oz box of spaghetti is groceries. That covers the grocery categories plus uncategorized products sold by weight or named as produce. Household supplies and repair parts (motion-sickness patches, stair treads) count only as a set or kit, so a tool set still does.

**Food boards mean cooking.** When most of what a theme or interest finds is food, it also searches "… kitchen tools" and "… cookbook", and food (even a gift box) ranks behind cooking gear and books for its card. A recipe board gives a meat grinder, a cookbook and a vegetable cutter rather than a stir-fry kit and peppers. A pin that only matches food stays in the history as a search, without viewing the groceries.

Many personal pins have no caption; for those the title of the pin's own page is used ("Diy dinosaur play house | Dinosaur dollhouse, …"). Pin titles are searched as written. With an occasion set, the last card searches the occasion alone ("housewarming gift"), still personalized by the pins, because phrases like "Barnerom diy housewarming gift" find nonsense.

**Rotation.** A screen shows at most one card per interest or board, and "Show me others" moves on to the ones not shown recently. They also move to the end of the history, so the top match drifts with them.

**Occasion and age** shape the search wording: "birthday gift" on its own returns generic gift-shop items, but "golf birthday gift" returns golf gifts. "Who's it for?" is only used for the heading: a dad might want LEGO as much as a grill, so who they are never steers the picks.

For a baby or a kid, age is also a filter, since the catalog has no age field: a pick must be in a children's category (Baby Products, Toys & Games, Children's Books, …) or say so in its name ("toddler", "6 months", "for kids"). If nothing they like passes, the cards fall back to general ideas for that age and the page says so. Teens shop from the same categories as adults, so for them only the wording changes.

**Prices.** The catalog stores prices of $1,000 and up as only their thousands digit (a MacBook is `"1"`), so prices under $10 are treated as unknown, and with a budget set, items without a trusted price are left out.

The name is only used for the heading. The SDK's `Domains` has `age_group` and `gender`, but they're set once per client and their values aren't documented, so they aren't used yet.

## Layout

```
app.py              FastAPI backend: builds the history, calls BehaviorGPT, picks gifts
pinterest.py        reads pins and board topics from a public Pinterest profile or board
lists.py            shareable shortlists and votes (SQLite)
docs/index.html     the main page (plain HTML, CSS and JS, no build step)
docs/list.html      the shared shortlist page, list.html?id=<id>
docs/config.js      where the page finds the API (same server locally, Render on Pages)
render.yaml         how Render builds and runs the API
tests/              pytest suite; BehaviorGPT and Pinterest are faked
docs/screenshots/   README images
```

`GIFT_CATALOG_ID` and `GIFT_MARKET` in `.env` switch to another catalog or market.

## Deploy

The page is static and lives in `docs/`, so GitHub Pages serves it as is. The API can't be static: it holds the BehaviorGPT key, fetches Pinterest (which browsers can't do across sites) and stores shortlists. It runs on [Render](https://render.com)'s free plan:

1. **API:** in Render, *New → Blueprint*, pick this repo, and paste your key for `UNBOXAI_API_KEY`. `render.yaml` does the rest. Calls are only accepted from `ALLOWED_ORIGINS` (https://jenspalmborg.github.io).
2. **Page:** in the repo's *Settings → Pages*, deploy from the `main` branch, `/docs` folder.

`docs/config.js` points the page at `https://behaviorgpt-gift-picker.onrender.com` when it's on github.io; change it if Render gives the service another name.

The free plan sleeps after 15 minutes without visits, so the first search after a pause takes up to a minute while it wakes (the page says so). Its disk doesn't survive a restart, so shared shortlists can disappear; a paid disk (`GIFT_DB` pointing at it) keeps them.

## Known limits

- The retail catalog is patchy: some interests need more specific wording ("gaming headset" rather than "video games").
- Three slots and one top pick means a third interest can get squeezed out.
- Near-duplicates from different sellers (two V-Bucks cards) can still slip through.
- A theme is only as good as the catalog's reading of its topic: "metamorphosis book" (from a board with Kafka pins) comes back as insect books and toys.
- The first search for a Pinterest profile reads every board page, about 4-5 seconds; later searches for it are cached for 10 minutes.
- Items over $10,000 can still slip under a budget (a Rolex stored as `"19"`), until the catalog's prices are fixed.
- "Find it" links straight to the Amazon product, since catalog ids are `pa` + the ASIN; other ids fall back to an Amazon search.
- Votes on shared lists aren't tied to accounts: one per browser, easy to game.

## License

MIT. Product data and images come from the BehaviorGPT pre-embedded catalog.
