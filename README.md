# Gift Picker

Tell it what someone loves ("running, hiking, flowers") and get three gift ideas, picked by [BehaviorGPT](https://github.com/Unbox-AI/behaviorgpt). The interests are played to the model as a short shopping session, and the model predicts what that person would reach for next.

![Gift ideas for Mom: running, hiking, flowers](docs/screenshots/picks.png)

It is a small, complete example of building on the BehaviorGPT SDK: no catalog to upload, no user data. Just a synthetic history, recommendations and personalized search against one of the pre-embedded catalogs.

> Status: working prototype. It runs against the pre-embedded `retail_catalog`, so gift quality depends on what that catalog carries.

## What it shows

| In the app | BehaviorGPT call |
|---|---|
| Choosing the best wording for an interest | `client.complete(history=[Search(q)])` on a few phrasings, keeping the highest score |
| Turning interests into a profile | `Search` + `View` events, one pair per interest |
| "Top match for their whole profile" | `complete` with the profile, ending on a `View` (recommendations) |
| "For their love of …" | `complete` with the profile plus a final `Search` (personalized search) |

## Run it

Needs Python 3.11+, [uv](https://github.com/astral-sh/uv) and a BehaviorGPT API key from [unboxai.com/behaviorgpt](https://unboxai.com/behaviorgpt).

```sh
git clone https://github.com/Jenspalmborg/behaviorgpt-gift-picker.git
cd behaviorgpt-gift-picker
uv sync
cp .env.example .env                # paste your key as UNBOXAI_API_KEY
uv run uvicorn app:app --reload
```

Open http://127.0.0.1:8000. Links like `/?for=Mom&interests=running,hiking,flowers&budget=50` fill in the form and run the search, which is handy for sharing examples.

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
4. **Keep them different.** Picks skip the products already "viewed" in the session, anything over budget, and items too close to an earlier pick (same leaf category or same first words of the title).

The name is only used for the heading, and the budget is a filter applied after the model call. Gender and age are not sent, although the SDK's `Domains` supports them.

## Layout

```
app.py              FastAPI backend: builds the history, calls BehaviorGPT, picks 3 gifts
static/index.html   the page (plain HTML, CSS and JS, no build step)
docs/screenshots/   README images
```

`GIFT_CATALOG_ID` and `GIFT_MARKET` in `.env` switch to another catalog or market.

## Known limits

- The retail catalog is patchy: some interests need more specific wording ("gaming headset" rather than "video games").
- Three slots and one top pick means a third interest can get squeezed out.
- Near-duplicates from different sellers (two V-Bucks cards) can still slip through.
- "Find it" opens an Amazon search for the product name, since the catalog has no product links.

## License

MIT. Product data and images come from the BehaviorGPT pre-embedded catalog.
