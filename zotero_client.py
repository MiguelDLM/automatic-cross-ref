"""
zotero_client.py
Wrapper de la API local de Zotero (http://localhost:23119).
"""
from __future__ import annotations

import os
import httpx
from typing import Any
from rich.console import Console

console = Console()


class ZoteroClient:
    """Cliente para la API local de Zotero."""

    def __init__(self, api_url: str, api_key: str = "", page_size: int = 100):
        self.base_url = api_url.rstrip("/")
        self.page_size = page_size
        headers = {"Zotero-API-Version": "3"}
        if api_key:
            headers["Zotero-API-Key"] = api_key
        self.client = httpx.Client(
            headers=headers,
            timeout=30.0,
        )

    # ────────────────────────────────────────────────────────────
    # Conectividad
    # ────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """Comprueba si el servidor local de Zotero está activo."""
        try:
            r = self.client.get("http://localhost:23119/")
            return r.status_code < 500
        except httpx.ConnectError:
            return False

    # ────────────────────────────────────────────────────────────
    # Items de la biblioteca
    # ────────────────────────────────────────────────────────────

    def get_all_items(self) -> list[dict[str, Any]]:
        """Devuelve todos los items regulares de la biblioteca (paginado)."""
        items = []
        start = 0
        while True:
            r = self.client.get(
                f"{self.base_url}/items",
                params={
                    "format": "json",
                    "itemType": "-attachment || note",
                    "limit": self.page_size,
                    "start": start,
                },
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            items.extend(batch)
            if len(batch) < self.page_size:
                break
            start += self.page_size
        return items

    def get_item_attachments(self, item_key: str) -> list[dict[str, Any]]:
        """Devuelve los adjuntos (PDFs) de un item."""
        url = f"{self.base_url}/items/{item_key}/children"
        r = self.client.get(url, params={"format": "json"})
        r.raise_for_status()
        children = r.json()
        return [
            c for c in children
            if c.get("data", {}).get("itemType") == "attachment"
            and c.get("data", {}).get("contentType") == "application/pdf"
        ]

    def get_item_relations(self, item_key: str) -> list[str]:
        """Devuelve las claves de items relacionados."""
        url = f"{self.base_url}/items/{item_key}"
        r = self.client.get(url, params={"format": "json"})
        r.raise_for_status()
        data = r.json().get("data", {})
        relations = data.get("relations", {})
        related_keys = []
        for rel_type, values in relations.items():
            if isinstance(values, list):
                for v in values:
                    key = v.split("/")[-1]
                    related_keys.append(key)
            elif isinstance(values, str):
                related_keys.append(values.split("/")[-1])
        return related_keys

    def get_storage_path(self, attachment: dict[str, Any]) -> str | None:
        """Obtiene la ruta al PDF en el storage local de Zotero."""
        data = attachment.get("data", {})
        link_mode = data.get("linkMode", "")

        if link_mode == "linked_file":
            # Archivo vinculado: la ruta está directamente en "path"
            return data.get("path")

        if link_mode in ("imported_file", "imported_url"):
            # Archivo importado: ruta en el storage de Zotero
            attachment_key = data.get("key") or attachment.get("key")
            filename = data.get("filename") or data.get("title", "")
            if attachment_key and filename:
                zotero_storage = os.path.expanduser("~/Zotero/storage")
                path = os.path.join(zotero_storage, attachment_key, filename)
                if os.path.exists(path):
                    return path
        return None

    # ────────────────────────────────────────────────────────────
    # Modificación (Write)
    # ────────────────────────────────────────────────────────────

    def add_relations(self, source_key: str, target_keys: list[str]) -> bool:
        """Añade relaciones bidireccionales a un item existente."""
        if not target_keys:
            return True

        url = f"{self.base_url}/items/{source_key}"
        r = self.client.get(url, params={"format": "json"})
        if r.status_code != 200:
            console.print(f"[red]Error obteniendo item {source_key}[/red]")
            return False

        item = r.json()
        version = item.get("version")
        data = item.get("data", {})
        relations = data.setdefault("relations", {})
        dc_relations = relations.setdefault("dc:relation", [])

        if isinstance(dc_relations, str):
            dc_relations = [dc_relations]

        added = False
        for tk in target_keys:
            rel_uri = f"http://zotero.org/users/0/items/{tk}"
            if rel_uri not in dc_relations:
                dc_relations.append(rel_uri)
                added = True

        if not added:
            return True

        relations["dc:relation"] = dc_relations
        data["relations"] = relations

        headers = {"If-Unmodified-Since-Version": str(version)}
        r = self.client.put(url, json=data, headers=headers)
        if r.status_code not in (204, 200):
            console.print(f"[red]Error guardando item {source_key}: {r.status_code}[/red]")
            return False
        return True

    def close(self):
        self.client.close()
