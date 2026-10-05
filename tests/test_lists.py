from fastapi.testclient import TestClient

import app as gift_app

client = TestClient(gift_app.app)

ITEMS = [
    {"id": "paB000000001", "name": "Acme Golf Balls", "price": 24.0, "image_url": "https://img.example/a.jpg"},
    {"id": "paB000000005", "name": "Grillmaster BBQ Tongs", "reason": "For their love of grilling"},
]


def new_list(**extra):
    res = client.post("/api/lists", json={"person": "Dad", "occasion": "birthday", "items": ITEMS, **extra})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_create_and_read_a_list(temp_db):
    list_id = new_list()
    data = client.get(f"/api/lists/{list_id}").json()
    assert data["person"] == "Dad" and data["occasion"] == "birthday"
    assert [i["votes"] for i in data["items"]] == [0, 0]
    # Links are built from the id, not taken from whoever made the list.
    assert data["items"][0]["url"] == "https://www.amazon.com/dp/B000000001"


def test_votes_go_up_and_down_but_not_below_zero(temp_db):
    list_id = new_list()

    def vote(delta):
        res = client.post(f"/api/lists/{list_id}/votes", json={"item_id": "paB000000005", "delta": delta})
        return res.json()["items"][1]["votes"]

    assert vote(1) == 1
    assert vote(1) == 2
    assert vote(-1) == 1
    assert vote(-1) == 0
    assert vote(-1) == 0


def test_list_errors(temp_db):
    list_id = new_list()
    assert client.get("/api/lists/nope").status_code == 404
    assert client.post(f"/api/lists/{list_id}/votes", json={"item_id": "other"}).status_code == 404
    assert client.post(f"/api/lists/{list_id}/votes", json={"item_id": "paB000000001", "delta": 5}).status_code == 422
    assert client.post("/api/lists", json={"items": []}).status_code == 422
    bad_image = [{"id": "x1", "name": "a", "image_url": "javascript:alert(1)"}]
    assert client.post("/api/lists", json={"items": bad_image}).status_code == 422
    assert client.get(f"/list.html?id={list_id}").status_code == 200
    old_link = client.get(f"/list/{list_id}", follow_redirects=False)
    assert old_link.status_code == 307 and old_link.headers["location"] == f"/list.html?id={list_id}"


def test_cors_allows_only_the_github_pages_site():
    ok = client.options("/api/gifts", headers={"Origin": "https://jenspalmborg.github.io", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "https://jenspalmborg.github.io"
    other = client.options("/api/gifts", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in other.headers
