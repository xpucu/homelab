# -*- coding: utf-8 -*-

#  Biblioman metadata provider for Autocaliweb / Calibre-Web
#  Fetches Bulgarian book metadata from https://biblioman.chitanka.info
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

from typing import List, Optional
from urllib.parse import quote, urljoin
import re

import requests
from lxml import html as lxml_html

from cps import logger, constants
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

log = logger.create()


class Biblioman(Metadata):
    __name__ = "Biblioman"
    __id__ = "biblioman"
    DESCRIPTION = "Biblioman (chitanka.info)"
    META_URL = "https://biblioman.chitanka.info/"
    BASE_URL = "https://biblioman.chitanka.info"
    SEARCH_URL = "https://biblioman.chitanka.info/books?q="
    MAX_RESULTS = 5  # how many result rows to follow into full book pages
    HEADERS = {
        "User-Agent": constants.USER_AGENT,
        "Accept-Language": "bg,en;q=0.8",
    }
    TIMEOUT = 8  # seconds -- never hang the fetch dialog

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        val = []
        if not self.active:
            return val
        if not query or not query.strip():
            return val

        q = query.strip()
        # ISBN? use the isbn: field; otherwise search as a title.
        if re.fullmatch(r"[\d\-xX]{10,17}", q):
            search_q = "isbn:" + q.replace("-", "")
        else:
            search_q = "title: " + q

        try:
            resp = requests.get(
                Biblioman.SEARCH_URL + quote(search_q.encode("utf-8")),
                headers=Biblioman.HEADERS,
                timeout=Biblioman.TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("Biblioman search failed: %s", e)
            return val

        try:
            tree = lxml_html.fromstring(resp.content)
        except Exception as e:
            log.warning("Biblioman: could not parse search page: %s", e)
            return val

        # Search results link to /books/<id>. Collect unique ids in order.
        book_links = []
        seen = set()
        for href in tree.xpath('//a[contains(@href, "/books/")]/@href'):
            m = re.search(r"/books/(\d+)(?:$|[/?#])", href)
            if not m:
                continue
            book_id = m.group(1)
            if book_id in seen:
                continue
            seen.add(book_id)
            book_links.append((book_id, urljoin(Biblioman.BASE_URL, href)))
            if len(book_links) >= Biblioman.MAX_RESULTS:
                break

        for book_id, book_url in book_links:
            try:
                mr = self._fetch_book(book_id, book_url, generic_cover)
                if mr:
                    val.append(mr)
            except Exception as e:
                log.warning("Biblioman: failed to parse book %s: %s", book_id, e)
                continue
        return val

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        # biblioman pages are full of tabs/newlines used for layout
        return re.sub(r"\s+", " ", (text or "")).strip()

    def _dd_text(self, tree, field_class: str) -> str:
        """Return cleaned text of the <dd> for a given entity-field-* class."""
        nodes = tree.xpath(
            f'//dd[contains(concat(" ", normalize-space(@class), " "),'
            f' " entity-field-{field_class} ")]'
        )
        if nodes:
            return self._clean(nodes[0].text_content())
        return ""

    def _dd_links(self, tree, field_class: str) -> List[str]:
        """Return list of <a> texts inside the <dd> for a field (multi-value)."""
        nodes = tree.xpath(
            f'//dd[contains(concat(" ", normalize-space(@class), " "),'
            f' " entity-field-{field_class} ")]//a'
        )
        out = []
        for n in nodes:
            txt = self._clean(n.text_content())
            if txt:
                out.append(txt)
        return out

    # -- book page parsing ---------------------------------------------------

    def _fetch_book(
        self, book_id: str, book_url: str, generic_cover: str
    ) -> Optional[MetaRecord]:
        try:
            resp = requests.get(
                book_url, headers=Biblioman.HEADERS, timeout=Biblioman.TIMEOUT
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("Biblioman: book fetch failed %s: %s", book_url, e)
            return None

        tree = lxml_html.fromstring(resp.content)

        # --- Title --- prefer og:title (clean), fall back to the title field
        title = ""
        og = tree.xpath('//meta[@property="og:title"]/@content')
        if og:
            title = self._clean(og[0])
        if not title:
            parts = self._dd_links(tree, "title")
            title = "; ".join(parts) if parts else self._dd_text(tree, "title")
        if not title:
            return None

        # --- Authors --- (may be multiple <a> in the author dd)
        authors = self._dd_links(tree, "author")
        if not authors:
            a = self._dd_text(tree, "author")
            if a:
                authors = [p.strip() for p in re.split(r"[;,]", a) if p.strip()]

        match = MetaRecord(
            id=book_id,
            title=title,
            authors=authors,
            url=book_url,
            source=MetaSourceInfo(
                id=self.__id__,
                description=Biblioman.DESCRIPTION,
                link=Biblioman.META_URL,
            ),
        )

        # --- Cover --- og:image is the clean high-res path
        cover = ""
        og_img = tree.xpath('//meta[@property="og:image"]/@content')
        if og_img:
            cover = og_img[0].strip()
        match.cover = cover if cover else generic_cover

        # --- Description --- annotation field
        match.description = self._dd_text(tree, "annotation")

        # --- Publisher --- (entity-field-publisher)
        match.publisher = self._dd_text(tree, "publisher") or None

        # --- Published date --- prefer publish year, else translation year
        year_text = (
            self._dd_text(tree, "publishingYear")
            or self._dd_text(tree, "dateOfTranslation")
        )
        ym = re.search(r"(\d{4})", year_text)
        if ym:
            match.publishedDate = ym.group(1) + "-01-01"

        # --- Series + index --- (entity-field-sequence) e.g. "... №1"
        seq = self._dd_text(tree, "sequence")
        if seq:
            sm = re.search(r"^(.*?)\s*(?:\u2116|#|No\.?)\s*([\d]+(?:\.\d+)?)", seq)
            if sm:
                match.series = sm.group(1).strip(" ,;")
                try:
                    match.series_index = float(sm.group(2))
                except ValueError:
                    match.series_index = 1
            else:
                match.series = seq
                match.series_index = 1

        # --- Tags --- category + genre
        tags = []
        for fc in ("category", "genre"):
            for v in (self._dd_links(tree, fc) or [self._dd_text(tree, fc)]):
                v = v.strip()
                if v and v not in tags:
                    tags.append(v)
        match.tags = tags

        # --- Language --- biblioman is Bulgarian editions
        lang = self._dd_text(tree, "language")
        match.languages = [lang] if lang else ["\u0431\u044a\u043b\u0433\u0430\u0440\u0441\u043a\u0438"]

        # --- Identifiers --- biblioman has no ISBN field (uses УДК); id only
        match.identifiers = {"biblioman": book_id}

        return match
