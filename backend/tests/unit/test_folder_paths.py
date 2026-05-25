import pytest

from app.repositories.folders import split_folder_path


def test_split_folder_path_handles_rooted_path():
    assert split_folder_path("/2026-05/生产排障") == ["2026-05", "生产排障"]


def test_split_folder_path_trims_extra_slashes():
    assert split_folder_path("//2026-05//ACAS项目/") == ["2026-05", "ACAS项目"]


def test_split_folder_path_rejects_empty_path():
    with pytest.raises(ValueError, match="folder path must contain at least one segment"):
        split_folder_path("/")


@pytest.mark.parametrize("path", ["relative/path", "2026-05"])
def test_split_folder_path_rejects_relative_path(path):
    with pytest.raises(ValueError, match="folder path must be absolute"):
        split_folder_path(path)


@pytest.mark.parametrize("path", ["/.", "/2026-05/../生产排障"])
def test_split_folder_path_rejects_dot_segments(path):
    with pytest.raises(ValueError, match="folder path must not contain . or .. segments"):
        split_folder_path(path)


def test_split_folder_path_rejects_control_characters():
    with pytest.raises(ValueError, match="folder path segments must not contain control characters"):
        split_folder_path("/2026-05/bad\x1fsegment")


def test_split_folder_path_rejects_overlong_segment():
    with pytest.raises(ValueError, match="folder path segment exceeds 255 characters"):
        split_folder_path(f"/{'a' * 256}")


def test_split_folder_path_rejects_overlong_canonical_path():
    path = "/" + "/".join(["a" * 250] * 5)

    with pytest.raises(ValueError, match="folder path exceeds 1024 characters"):
        split_folder_path(path)
