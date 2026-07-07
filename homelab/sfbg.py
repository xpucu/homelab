# -*- coding: utf-8 -*-

#  SFBG metadata provider for Autocaliweb / Calibre-Web
#  Fetches Bulgarian (SF/fantasy) book metadata from http://sfbg.us
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


class SFBG(Metadata):
    __name__ = "SFBG"
    __id__ = "sfbg"
    DESCRIPTION = "SFBG (sfbg.us)"
    META_URL = "http://sfbg.us/"
    BASE_URL = "http://sfbg.us"
    SEARCH_URL = "http://sfbg.us/search?query="
    MAX_RESULTS = 5
    HEADERS = {
        "User-Agent": constants.USER_AGENT,
        "Accept-Language": "bg,en;q=0.8",
    }
    TIMEOUT = 8  # never hang the fetch dialog

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        val = []
        if not self.active:
            return val
        if not query or not query.strip():
            return val

        try:
            resp = requests.get(
                SFBG.SEARCH_URL + quote(query.strip().encode("utf-8")),
                headers=SFBG.HEADERS,
                timeout=SFBG.TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("SFBG search failed: %s", e)
            return val

        try:
            tree = lxml_html.fromstring(resp.content)
        except Exception as e:
            log.warning("SFBG: could not parse search page: %s", e)
            return val

        # Results link to /book/<CODE>. Collect unique codes in order.
        book_links = []
        seen = set()
        for href in tree.xpath('//a[contains(@href, "/book/")]/@href'):
            m = re.search(r"/book/([A-Za-z0-9\-]+)", href)
            if not m:
                continue
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)
            book_links.append((code, urljoin(SFBG.BASE_URL, href)))
            if len(book_links) >= SFBG.MAX_RESULTS:
                break

        for code, book_url in book_links:
            try:
                mr = self._fetch_book(code, book_url, generic_cover)
                if mr:
                    val.append(mr)
            except Exception as e:
                log.warning("SFBG: failed to parse book %s: %s", code, e)
                continue
        return val

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    def _rows(self, tree) -> dict:
        """Build a dict of {label(without trailing colon): value} from the
        book page's metadata table."""
        data = {}
        for tr in tree.xpath('//table//tr'):
            cells = tr.xpath('./td|./th')
            if len(cells) >= 2:
                label = self._clean(cells[0].text_content()).rstrip(":").lstrip("+").strip()
                value = self._clean(cells[1].text_content())
                if label and value and label not in data:
                    data[label] = value
        return data

    # -- book page parsing ---------------------------------------------------

    def _fetch_book(
        self, code: str, book_url: str, generic_cover: str
    ) -> Optional[MetaRecord]:
        try:
            resp = requests.get(
                book_url, headers=SFBG.HEADERS, timeout=SFBG.TIMEOUT
            )
            resp.raise_for_status()
        except Exception as e:
            log.warning("SFBG: book fetch failed %s: %s", book_url, e)
            return None

        tree = lxml_html.fromstring(resp.content)

        # --- Title --- h2
        title = ""
        h2 = tree.xpath('//h2')
        if h2:
            title = self._clean(h2[0].text_content())
        if not title:
            return None

        # --- Authors --- h3 (may contain multiple, comma/&-separated)
        authors = []
        h3 = tree.xpath('//h3')
        if h3:
            a = self._clean(h3[0].text_content())
            if a:
                authors = [p.strip() for p in re.split(r"[,&]", a) if p.strip()]

        match = MetaRecord(
            id=code,
            title=title,
            authors=authors,
            url=book_url,
            source=MetaSourceInfo(
                id=self.__id__,
                description=SFBG.DESCRIPTION,
                link=SFBG.META_URL,
            ),
        )

        rows = self._rows(tree)

        # --- Cover --- predictable /covers/<PREFIX>/<CODE>.jpg, else scan imgs
        cover = ""
        imgs = [s for s in tree.xpath('//img/@src') if "cover" in s.lower()]
        if imgs:
            cover = urljoin(SFBG.BASE_URL, imgs[0])
        match.cover = cover if cover else generic_cover

        # --- Series --- (Поредица)  — SFBG usually has no explicit index number
        series = rows.get("Поредица", "")
        if series:
            sm = re.search(r"^(.*?)\s*(?:\u2116|#|No\.?)\s*(\d+(?:\.\d+)?)$", series)
            if sm:
                match.series = sm.group(1).strip(" ,;")
                try:
                    match.series_index = float(sm.group(2))
                except ValueError:
                    match.series_index = 1
            else:
                match.series = series
                match.series_index = 1

        # --- Publisher --- (Издател)
        match.publisher = rows.get("Издател") or None

        # --- Published date --- (Година)
        year = rows.get("Година", "")
        ym = re.search(r"(\d{4})", year)
        if ym:
            match.publishedDate = ym.group(1) + "-01-01"

        # --- Description --- text following the <h4>Издателска анотация</h4>
        match.description = self._annotation(tree)

        # --- Tags --- nationality/genre hints (SFBG is SF/fantasy focused)
        tags = []
        nat = rows.get("Националност")
        # SFBG doesn't always expose a clean genre; leave tags minimal/empty
        match.tags = tags

        # --- Language --- SFBG lists Bulgarian editions
        match.languages = ["\u0431\u044a\u043b\u0433\u0430\u0440\u0441\u043a\u0438"]

        # --- Identifiers --- ISBN (SFBG has it) + sfbg code
        match.identifiers = {"sfbg": code}
        isbn = rows.get("ISBN", "")
        if isbn:
            isbn_clean = re.sub(r"[^0-9Xx]", "", isbn)
            if isbn_clean:
                match.identifiers["isbn"] = isbn_clean

        return match

    def _annotation(self, tree) -> str:
        """Grab paragraph text following the 'Издателска анотация' h4 heading."""
        # find the heading node
        heads = tree.xpath(
            '//h4[contains(normalize-space(.), "\u0430\u043d\u043e\u0442\u0430\u0446\u0438\u044f") '
            'or contains(normalize-space(.), "\u0410\u043d\u043e\u0442\u0430\u0446\u0438\u044f")]'
        )
        if not heads:
            return ""
        h = heads[0]
        # collect following siblings' text until the next heading
        parts = []
        for sib in h.itersiblings():
            if sib.tag in ("h1", "h2", "h3", "h4", "h5", "table"):
                break
            txt = self._clean(sib.text_content())
            if txt:
                parts.append(txt)
        return " ".join(parts).strip()