"""多图白名单、容量、旧客户端兼容以及成人库封面排除。"""

import pytest
from movieclaw_friends.app import Media
from movieclaw_friends.media import sanitize_media
from pydantic import ValidationError


def test_nine_images_preserve_order_and_legacy_cover():
    source = {
        "tmdb_id": 550,
        "kind": "movie",
        "title": "电影",
        "cover_images": [{"path": f"/image-{i}.jpg", "type": "poster"} for i in range(9)],
    }
    value = Media.model_validate(source).model_dump(mode="json")
    assert value["cover_images"] == source["cover_images"]
    assert value["cover_path"] == "/image-0.jpg" and value["cover_type"] == "poster"
    assert sanitize_media(value)["cover_images"] == source["cover_images"]
    assert "cover_images" not in Media(title="旧电影").model_dump()
    with pytest.raises(ValidationError):
        Media.model_validate({**source, "cover_images": source["cover_images"] * 2})


@pytest.mark.parametrize(
    "path",
    [
        "http://private.test/a.jpg",
        "/../secret.jpg",
        "/image.jpg?token=x",
        "/folder/image.jpg",
        "javascript:alert(1)",
    ],
)
def test_multi_image_external_or_private_path_rejected(path):
    with pytest.raises(ValidationError):
        Media.model_validate({"cover_images": [{"path": path, "type": "poster"}]})
    assert "cover_images" not in sanitize_media(
        {"cover_images": [{"path": path, "type": "poster"}]}
    )
