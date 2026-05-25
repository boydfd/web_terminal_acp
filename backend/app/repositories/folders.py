from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models import Folder, VirtualWindow


@dataclass
class TreeWindow:
    id: UUID
    title: str
    status: str
    created_at: datetime
    title_tags: list[str] | None = None


@dataclass
class TreeFolder:
    id: UUID
    name: str
    path: str
    folders: list[TreeFolder] = field(default_factory=list)
    windows: list[TreeWindow] = field(default_factory=list)


@dataclass
class TopicTreeNode:
    path: str
    name: str
    is_leaf: bool
    terminal_count: int
    children: list[TopicTreeNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "name": self.name,
            "is_leaf": self.is_leaf,
            "terminal_count": self.terminal_count,
            "children": [child.to_dict() for child in self.children],
        }


MAX_FOLDER_SEGMENT_LENGTH = 255
MAX_FOLDER_PATH_LENGTH = 1024
GET_OR_CREATE_RETRIES = 3


def canonicalize_folder_path(path: str) -> str:
    stripped_path = path.strip()
    if not stripped_path.startswith("/"):
        raise ValueError("folder path must be absolute")

    segments: list[str] = []
    for raw_segment in stripped_path.split("/"):
        if not raw_segment:
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in raw_segment):
            raise ValueError("folder path segments must not contain control characters")

        segment = raw_segment.strip()
        if not segment:
            continue
        if segment in {".", ".."}:
            raise ValueError("folder path must not contain . or .. segments")
        if len(segment) > MAX_FOLDER_SEGMENT_LENGTH:
            raise ValueError("folder path segment exceeds 255 characters")
        segments.append(segment)

    if not segments:
        raise ValueError("folder path must contain at least one segment")

    canonical_path = f"/{'/'.join(segments)}"
    if len(canonical_path) > MAX_FOLDER_PATH_LENGTH:
        raise ValueError("folder path exceeds 1024 characters")
    return canonical_path


def split_folder_path(path: str) -> list[str]:
    return canonicalize_folder_path(path).removeprefix("/").split("/")


async def _get_or_create_folder_by_path_once(
    session: AsyncSession, client_id: UUID, path: str
) -> Folder:
    parent_id: UUID | None = None
    folder: Folder | None = None
    current_path = ""

    for segment in split_folder_path(path):
        current_path = f"{current_path}/{segment}"
        folder = await _get_or_create_folder_segment(
            session, client_id, segment, current_path, parent_id
        )
        parent_id = folder.id

    if folder is None:  # split_folder_path prevents this; keep type checkers honest.
        raise ValueError("folder path must contain at least one segment")
    return folder


async def _get_or_create_folder_segment(
    session: AsyncSession,
    client_id: UUID,
    segment: str,
    path: str,
    parent_id: UUID | None,
) -> Folder:
    folder = await session.scalar(
        select(Folder).where(Folder.client_id == client_id, Folder.path == path)
    )
    if folder is not None:
        return folder

    folder = Folder(client_id=client_id, name=segment, path=path, parent_id=parent_id)
    try:
        async with session.begin_nested():
            session.add(folder)
            await session.flush()
    except IntegrityError:
        folder = await session.scalar(
            select(Folder).where(Folder.client_id == client_id, Folder.path == path)
        )
        if folder is None:
            raise
    return folder


async def get_or_create_folder_by_path(
    session: AsyncSession, client_id: UUID, path: str
) -> Folder:
    canonical_path = canonicalize_folder_path(path)

    for _ in range(GET_OR_CREATE_RETRIES):
        try:
            return await _get_or_create_folder_by_path_once(session, client_id, canonical_path)
        except IntegrityError:
            pass

    folder = await session.scalar(
        select(Folder).where(Folder.client_id == client_id, Folder.path == canonical_path)
    )
    if folder is None:
        return await _get_or_create_folder_by_path_once(session, client_id, canonical_path)
    return folder


async def ensure_default_folder(session: AsyncSession, client_id: UUID) -> Folder:
    return await get_or_create_folder_by_path(session, client_id, "/未分类")


async def folder_has_children(session: AsyncSession, folder_id: UUID) -> bool:
    child_id = await session.scalar(
        select(Folder.id).where(Folder.parent_id == folder_id).limit(1)
    )
    return child_id is not None


async def count_direct_windows_in_folder(
    session: AsyncSession, client_id: UUID, folder_id: UUID
) -> int:
    return await session.scalar(
        select(func.count(VirtualWindow.id)).where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.folder_id == folder_id,
        )
    ) or 0


async def folder_path_would_create_child_under_occupied_leaf(
    session: AsyncSession,
    client_id: UUID,
    path: str,
) -> bool:
    canonical_path = canonicalize_folder_path(path)
    existing_target_id = await session.scalar(
        select(Folder.id).where(Folder.client_id == client_id, Folder.path == canonical_path)
    )
    if existing_target_id is not None:
        return False

    current_path = ""
    for segment in split_folder_path(canonical_path)[:-1]:
        current_path = f"{current_path}/{segment}"
        folder = await session.scalar(
            select(Folder).where(Folder.client_id == client_id, Folder.path == current_path)
        )
        if folder is None:
            continue
        if await folder_has_children(session, folder.id):
            continue
        if await count_direct_windows_in_folder(session, client_id, folder.id) > 0:
            return True
    return False


async def build_topic_tree_context(session: AsyncSession, client_id: UUID) -> list[dict[str, object]]:
    folders = list(
        await session.scalars(
            select(Folder)
            .options(
                load_only(
                    Folder.id,
                    Folder.parent_id,
                    Folder.name,
                    Folder.path,
                    Folder.sort_order,
                )
            )
            .where(Folder.client_id == client_id)
            .order_by(Folder.sort_order, Folder.path != "/未分类", Folder.name, Folder.id)
        )
    )
    direct_window_counts = await _direct_window_counts_by_folder(session, client_id)
    nodes = {
        folder.id: TopicTreeNode(
            path=folder.path,
            name=folder.name,
            is_leaf=True,
            terminal_count=direct_window_counts.get(folder.id, 0),
        )
        for folder in folders
    }

    roots: list[TopicTreeNode] = []
    for folder in folders:
        node = nodes[folder.id]
        if folder.parent_id is not None and folder.parent_id in nodes:
            nodes[folder.parent_id].children.append(node)
        else:
            roots.append(node)

    for node in nodes.values():
        node.is_leaf = not node.children

    return [root.to_dict() for root in roots]


async def _direct_window_counts_by_folder(session: AsyncSession, client_id: UUID) -> dict[UUID, int]:
    rows = await session.execute(
        select(VirtualWindow.folder_id, func.count(VirtualWindow.id))
        .where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.folder_id.is_not(None),
        )
        .group_by(VirtualWindow.folder_id)
    )
    return {folder_id: count for folder_id, count in rows if folder_id is not None}


async def build_tree(session: AsyncSession, client_id: UUID) -> list[TreeFolder]:
    folders = list(
        await session.scalars(
            select(Folder)
            .options(
                load_only(
                    Folder.id,
                    Folder.parent_id,
                    Folder.name,
                    Folder.path,
                    Folder.sort_order,
                )
            )
            .where(Folder.client_id == client_id)
            .order_by(Folder.sort_order, Folder.name, Folder.id)
        )
    )
    nodes = {
        folder.id: TreeFolder(
            id=folder.id,
            name=folder.name,
            path=folder.path,
        )
        for folder in folders
    }

    roots: list[TreeFolder] = []
    for folder in folders:
        node = nodes[folder.id]
        if folder.parent_id is not None and folder.parent_id in nodes:
            nodes[folder.parent_id].folders.append(node)
        else:
            roots.append(node)

    windows = list(
        await session.scalars(
            select(VirtualWindow)
            .options(
                load_only(
                    VirtualWindow.id,
                    VirtualWindow.folder_id,
                    VirtualWindow.title,
                    VirtualWindow.title_tags,
                    VirtualWindow.status,
                    VirtualWindow.created_at,
                )
            )
            .where(
                VirtualWindow.client_id == client_id,
                VirtualWindow.folder_id.is_not(None),
            )
            .order_by(VirtualWindow.created_at, VirtualWindow.title, VirtualWindow.id)
        )
    )
    for window in windows:
        if window.folder_id not in nodes:
            continue
        nodes[window.folder_id].windows.append(
            TreeWindow(
                id=window.id,
                title=window.title,
                status=window.status.value,
                created_at=window.created_at,
                title_tags=window.title_tags,
            )
        )

    return roots

