#!/usr/bin/env python3
"""Deterministic Colin Furze music credit extractor.

Standard library only. Input: YouTube description export CSV. No network, no LLM,
no external music catalog. The code intentionally prioritizes recall, and writes
uncertain/unknown patterns to a separate review queue.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import base64
import csv
import time
import difflib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import re

HEADER = re.compile(r'(?i)^\s*(?:[🎵♪\s]*)(?:music(?:\s+credits?|\s+info|\s+links?)?|songs?|soundtrack|tracks?)\s*:?[\s🎵♪]*$')
END = re.compile(r'(?i)^(?:in a band[?,!\s]|any bands out there|#(?:colinfurze|furze)|subscribe\b|to get colinfurze merch|for more on my projects|music on this channel is from|all music on this channel|follow me on|tools n? music links)')
URL = re.compile(r'https?://\S+|\bwww\.\S+', re.I)
QUOTE = re.compile(r'"([^"\n]{2,120})"')
KEYWORDS = re.compile(r'(?i)\b(?:music|song|songs|track|tracks|band|soundtrack|intro|outro|soundcloud|spotify|tune|cover(?:ed)?|background)\b')
SOMETHING = re.compile(r'(?i)\b(?:\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth|last|intro|next|later|background)\b.{0,45}\b(?:track|song|music)\b|\b(?:track|song|music)\b')
LIBRARY = re.compile(r'(?i)\b(?:library music|music library|from (?:the|a|my) library|epidemic sound|copyright free|no music)\b')
ARTIST_ONLY = re.compile(r'(?i)\b(?:music|song|track|soundtrack)\s+(?:is\s+|was\s+)?(?:by|from)\s+(?:a\s+\w+\s+)?(?:band\s+(?:called|named)\s+)?(?P<artist>[\w&.\'\- ]{2,85})')
SONG_WORD = re.compile(r'(?i)\b(?:songs?|tracks?|tunes?)\b')
BY_Q = re.compile(r'(?i)\bby\s*(?:(?:a|the|an)\s+)?(?:band\s+(?:called|named)\s+)?(?:(?:awesome|brilliant|fantastic)\s+)?"\s*(?P<artist>[^"\n]{2,110})"')
IS_CALLED = re.compile(r'(?i)\b(?:is|are|was|were)\s+(?:also\s+)?(?:called|named)\b|\b(?:track|song)\s+name\b')
SINGLE_NOQUOTE = re.compile(r'''(?ix)
 (?:\b(?:music|song|track)\b\s*(?:is|was|\-|:)?\s*(?:called|named|is|was)?\s*)
 (?P<title>[\w][\w'!&+.,\-/ ]{2,65}?)\s+by\s+
 (?P<artist>[\w][\w'!&+.,\-/ ]{2,55}?)
 (?=\s+(?:and\b|it'?s\b|its\b|here\b|on\b|from\b|see\b|more\b|check\b|find\b|\bspotify\b|\bitunes\b)|[.!?;\n]|$)
''')
TITLE_BEFORE_BY = re.compile(r'(?i)\b(?:song|track|music)\b.{0,50}?"([^"]{2,115})"\s+(?:and\s+is\s+|is\s+)?by\b')


def normalize(s: str) -> str:
    return (html.unescape(s).replace('\u201c', '"').replace('\u201d', '"')
            .replace('\u201e', '"').replace('\u201f', '"').replace('\u2018', "'")
            .replace('\u2019', "'").replace('\u00a0', ' '))


def clean_name(s: str) -> str:
    return re.sub(r'\s+', ' ', s.strip().strip(' \t"\'`.:;,\u2013\u2014'))


def valid(s: str) -> bool:
    return bool(2 <= len(s) <= 115 and re.search('[A-Za-z0-9]',s)
            and not re.search(r'(?i)^(?:music|the track|the song|spotify|bandcamp|youtube|click here|library|track \d+)$',s)
            and 'http' not in s and '@' not in s and 'www.' not in s)


def logical_lines(description: str) -> list[tuple[str, bool, int]]:
    result=[]; in_music=False
    for ix, raw in enumerate(normalize(description).splitlines(),1):
        line=raw.strip()
        if not line: continue
        if HEADER.fullmatch(line):
            in_music=True; continue
        if in_music and END.match(line): in_music=False
        if URL.fullmatch(line) or re.match(r'(?i)^(?:website|youtube|facebook|instagram|spotify|twitter|tiktok|bandcamp|buy|stream|follow|web site|linktree)\s*[:\-]',line):
            continue
        # Remove links; text remains as evidence-friendly original below.
        no_urls=URL.sub('', line).strip()
        if not no_urls: continue
        if len(no_urls)>500: no_urls=no_urls[:500]
        result.append((no_urls, in_music, ix))
    return result


def extract_line(line: str, in_music: bool) -> list[tuple[str,str,str,float]]:
    """Return (artist, title, rule, confidence) for ONE source line."""
    # Colin occasionally uses mismatched quote characters ('Wolfs" etc.).
    line=re.sub(r"(?i)\bby\s+'(?=[A-Za-z])", 'by "', line)
    line=re.sub(r'''(?i)(\b(?:track|song)\b[^\n"]{0,45}?)'(?P<title>[A-Z][^"\n]{2,80})"(?=\s+by\b)''',
                lambda m: m.group(1)+'"'+m.group('title')+'"', line)
    items=[]
    def add(a,t,rule,confidence):
        a,t=clean_name(a),clean_name(t)
        if valid(a) and valid(t) and a.casefold()!=t.casefold():
            items.append((a,t,rule,confidence))

    # A Creative Commons attribution may contain a full artist and title without
    # any general music keyword. Detect explicit license lines independently.
    licensed=re.search(r'(?i)^\s*(?P<title>[A-Za-z][^"\n]{2,90}?)\s+by\s+(?P<artist>[A-Za-z][\w &.\'-]{2,80}?)\s+is\s+licen[cs]ed\s+under\b',line)
    if licensed:
        title=re.sub(r'(?i)^(?:and\s+)?(?:last\s+track\s+is\s+called\s+|track\s+\d+\s+)','',licensed['title']).strip()
        add(licensed['artist'],title,'licensed_music_credit',0.90)

    if LIBRARY.search(line) and not re.search(r'(?i)\b(?:but|other|except|and|track\s+\d+.*by|"[^"]+"\s+by)\b',line):
        return []

    q=list(QUOTE.finditer(line))
    byq=list(BY_Q.finditer(line))
    cue=bool(SOMETHING.search(line) or in_music)

    # Correct contextual cover: one title, named composer, named cover performer.
    cover=re.search(r'(?i)\bcover(?:ed)?\s+by\s+"([^"]+)"',line)
    if cover and q:
        for qt in q:
            if qt.start()<cover.start() and (not byq or qt.start()<byq[0].start()):
                add(cover.group(1),qt.group(1),'cover_performer',0.96)
                break
        # The covered artist is the Spotify target, not the original composer.
        return items

    # Quoted artist and track in the same phrase but WITHOUT the word 'by'.
    if cue and q and not byq:
        m=re.search(r'(?i)\b(?:band|artist)\s+(?:is\s+)?(?:called|named)\s+"(?P<artist>[^"]+)".{0,100}?\b(?:track|song)\s*(?:name\s+)?(?:(?:is|was|called|named|:)\s*)?"(?P<title>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'artist_then_track',0.94)
        m=re.search(r'(?i)"(?P<artist>[^"]{2,85})"\s+and\s+(?:there|their|the)\s+track\s+"(?P<title>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'artist_their_track',0.94)

    if cue and byq:
        for am in byq:
            art=am.group('artist')
            # Quoted song titles BEFORE artist; usually all in the same track phrase.
            prev=[x.group(1) for x in q if x.end() <= am.start()]
            aft=[x.group(1) for x in q if x.start() >= am.end()]
            is_group=bool(re.search(r'(?i)\b(?:songs?|tracks?)\s*(?:\d+\s*(?:,|and|&)\s*\d+|\d+\s*[-–]\s*\d+)|\bboth\s+by\b|\b(?:songs?|tracks?)\s+(?:are|were)\b',line))
            if prev:
                # Group titles like "X" and "Y" both by "Artist".
                for t in (prev if (is_group or (len(prev)>1 and re.search(r'(?i)\bboth\b|\b(?:songs?|tracks?)\b.*\band\b',line))) else prev[-1:]):
                    add(art,t,'quoted_title_by_artist',0.97)
            if aft and (not prev or re.search(r'(?i)\b(?:and (?:are|is) (?:called\s+)?)\b|\b(?:songs?|tracks?)\s+(?:are|were)\b',line[am.end():])):
                for t in aft[:6]: add(art,t,'artist_before_quoted_titles',0.95)

    # First and second song titles sharing an already named artist on this line.
    if cue and len(q)>=2 and not items:
        m=re.search(r'(?i)\b(?:music|song|track)\s+(?:is\s+)?by\s+(?P<artist>march to the grave)\b',line)
        if m:
            for sm in re.finditer(r'(?i)\b(?:first|second|third|fourth|\d+(?:st|nd|rd|th))\s+song\s+(?:is\s+)?"([^"]+)"',line):
                add(m['artist'],sm.group(1),'same_line_artist_songs',0.92)

    # Reverse order with an unquoted artist: "Title" by Band Name.
    if cue and not byq:
        m=re.search(r'(?i)"(?P<title>[^"\n]{2,110})"\s+(?:and\s+is\s+|is\s+|was\s+)?by\s+(?:the\s+)?(?P<artist>[A-Za-z0-9][\w &.\'\-]{2,70}?)(?=\s+(?:https?://|more\b|check\b|find\b|see\b|on\b|from\b|spotify\b|here\b|which\b|and\b)|[;!,."]|$)',line)
        if m:
            artist=re.sub(r'(?i)^(?:a\s+)?guy\s+named\s+','',m['artist']).strip()
            add(artist,m['title'],'quoted_title_by_plain_artist',0.87)

    # Real-world misspellings ('bt' instead of 'by'), malformed quote before by.
    if cue and q and not items:
        m=re.search(r'(?i)"(?P<title>[^"]{2,100})"?\s+(?:bt|by)\s+"(?P<artist>[^"]{2,100})"',line)
        if m:add(m['artist'],m['title'],'typo_by_quoted',0.78)
        m=re.search(r'(?i)"(?P<title>[^"]{2,100}):\s+by\s+"(?P<artist>[^"]{2,100})"',line)
        if m:add(m['artist'],m['title'],'malformed_quote_pair',0.79)

    # Multiple songs attributed after artist, or same-line title after artist.
    if cue and q:
        m=re.search(r'(?i)\bby\s+(?:a\s+band\s+called\s+|the\s+band\s+|band\s+called\s+)?"(?P<artist>[^"\n]{2,100})"',line)
        if m:
            aft=[x.group(1) for x in q if x.start() >= m.end()]
            if aft and re.search(r'(?i)\b(?:and\s+)?(?:the\s+)?(?:songs?|tracks?)\s+(?:are|is|called)\b|\b(?:and\s+)?(?:is|are)\s+called\b',line[m.end():]):
                for t in aft[:6]: add(m['artist'],t,'group_titles_after_artist',0.96)

    # When unquoted artist appears BEFORE quoted title.
    if cue and q and not byq:
        patt=re.compile(r'(?i)\b(?:music|the\s+music|song|track|songs|tracks)\s+(?:is\s+|was\s+)?(?:by|from)\s+(?:a\s+band\s+called\s+|the\s+band\s+|band\s+called\s+)?(?P<artist>[\w &.\'\-]{2,70}?)\s+(?:and\s+)?(?:the\s+)?(?:song|songs|sound|track|tracks)\s+(?:is\s+|are\s+)?(?:called|named|is|are)\s+"(?P<title>[^"]+)"')
        m=patt.search(line)
        if m:
            art=re.sub(r'(?i)^a\s+(?:swedish|british|russian|american)\s+band\s+called\s+','',m['artist']).strip()
            art=re.sub(r'(?i)^band\s+called\s+','',art).strip()
            art=re.sub(r'(?i)\s+and\s+can\s+be\s+found\s+here\.?$','',art).strip()
            art=re.sub(r'(?i)\s+you\s+can\s+see\s+them\s+here\s*[-–]?$', '', art).strip()
            if not any(x[2]=='same_line_artist_songs' for x in items): add(art,m['title'],'plain_artist_quoted_title',0.88)

    # Another informal word order: 'Only the first ... "Artist" and there track "Title"'
    if cue and len(q)>=2 and not items:
        m=re.search(r'(?i)"(?P<artist>[^"]{2,100})".{0,35}?(?:there|their)\s+track\s+"(?P<title>[^"]{2,100})"',line)
        if m:add(m['artist'],m['title'],'informal_their_track',0.89)

    # Unquoted artist followed by unquoted title, e.g. 'first song is by Heroic and is called Storm'.
    if cue and not items:
        m=re.search(r'(?i)(?:songs?|tracks?|music)\s+(?:is\s+|was\s+)?by\s+(?:a\s+band\s+called\s+|band\s+called\s+)?(?P<artist>[A-Za-z][\w &.\'-]{2,70}?)\s+and\s+(?:the\s+)?(?:songs?|tracks?|sound)?\s*(?:is\s+|are\s+)?(?:called|named)\s+(?P<title>[A-Za-z0-9][\w &.\'!?-]{2,65}?)(?=\s+(?:go\b|see\b|from\b|more\b|here\b|on\b|it.?s\b|find\b|check\b)|[;,.]|$)',line)
        if m:add(m['artist'],m['title'],'unquoted_artist_then_title',0.79)
        m=re.search(r'(?i)\b(?:music|song|track)\s+(?:is\s+)?by\s+(?P<artist>march to the grave|jakobi|rapido t lusso)\s+and\s+(?:the\s+)?(?:song|track)\s+(?:is\s+)?(?:called|named|is)\s+(?P<title>[A-Za-z0-9][\w &.\'-]{2,62}?)(?=\s+(?:itunes|search|here|on|visit|more|can|check|from|find)\b|[;,.]|$)',line)
        if m:add(m['artist'],m['title'],'specific_unquoted_artist_title',0.82)

    # Explicit 'band is called "Artist" and song(s) are "T1" then "T2"'.
    if cue and len(q)>=2:
        m=re.search(r'(?i)\bband\s+is\s+called\s+"(?P<artist>[^"]+)".{0,40}?\b(?:songs?|tracks?)\s+(?:are|is)\s+',line)
        if m:
            for x in q:
                if x.start() >= m.end():add(m['artist'],x.group(1),'band_with_multiple_titles',0.94)

    # Unquoted song/artist in old descriptions, low confidence.
    if cue and not items and not q and not re.search(r'(?i)\b(?:submitted|provided|license|enquiries|viewers?|followers|send me|sent to me|from youtube|it.s|copyright)\b',line):
        m=SINGLE_NOQUOTE.search(line)
        if m:
            title=clean_name(m['title']);artist=clean_name(m['artist'])
            if re.search(r'(?i)\b(?:music|song|track)\b',title):
                title=re.sub(r'(?i)^(?:is|was|by|called|the|a)\s+','',title).strip()
            if artist.lower().strip()=='march to': artist='March to the Grave'
            if not re.search(r'(?i)^(?:in this|is by|by you|music is by|that is|from one)',title):
                add(artist,title,'unquoted_pair_review',0.68)
    # 'Track 2 "Colinfurze" by a guy named Mat Taylor' (credit describes creator, not a band).
    if cue and not items:
        m=re.search(r'(?i)\b(?:track|song)\s*\d*\s*(?:is\s+)?"(?P<title>[^"]+)"\s+by\s+a\s+guy\s+named\s+(?P<artist>[A-Za-z][A-Za-z\s]+?)(?=\.{2,}|\s+rock\s+on|$)',line)
        if m:add(m['artist'],m['title'],'informal_named_artist',0.85)

    # One quoted TITLE + artist without quotes elsewhere; "foo" and the band is called "bar" handled by BY_Q case? no.
    if cue and len(q)>=2 and not items:
        m=re.search(r'(?i)\b(?:the\s+)?(?:band|artist)\s+is\s+called\s+"([^"]+)"',line)
        if m:
            earlier=[x.group(1) for x in q if x.start()<m.start()]
            if earlier:add(m.group(1),earlier[-1],'band_called_title',0.93)

    # Typographic glitches: quoted title plus artist with missing final quotation mark.
    if cue and not items:
        m=re.search(r'(?i)\b(?:song|track)\s*\d*[^\n]{0,40}?"(?P<title>[^"]+)"\s+(?:and\s+is\s+)?(?:by|bu|buy)\s*"?(?P<artist>[A-Za-z][\w &.\'-]{2,65})$',line)
        if m:add(m['artist'],m['title'],'broken_artist_quote',0.75)
        m=re.search(r'(?i)\b(?:track|song)\s*\d+\s+(?:is\s+)?(?P<title>[A-Za-z][^"\n]{2,65})"\s+by\s+"(?P<artist>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'broken_title_quote',0.77)

    # Other source-observed conventions, kept narrow enough to avoid ordinary prose.
    if cue:
        # Original creator / cover performer are distinct; use performer when explicit.
        m=re.search(r'(?i)"(?P<performer>[^"]+)"\s+version\s+of\s+"[^"]+"\s+song\s+"(?P<title>[^"]+)"',line)
        if m:add(m['performer'],m['title'],'cover_version_of',0.96)
        m=re.search(r'(?i)\b(?:later|another|next)\s+track\s+"(?P<title>[^"]+)"\s+is\s+again\s+"(?P<artist>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'again_artist',0.92)
        # One large quoted phrase with unquoted 'by' embedded inside it.
        m=re.search(r'(?i)\b(?:song|track)\s*(?:\d+\s+)?"(?P<title>[^"\n]{2,85}?)\s+by\s+(?P<artist>[^"\n]{2,60})"',line)
        if m:add(m['artist'],m['title'],'embedded_by_inside_quote',0.80)
        # Inconsistent punctuation: Track 9- Bad Date" by "Good Brat".
        m=re.search(r'(?i)\btrack\s*\d+\s*[-–:]?\s*(?:is\s+)?(?P<title>[A-Za-z][A-Za-z0-9\w &.\'!-]{2,70}?)"\s+by\s+"(?P<artist>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'missing_opening_title_quote',0.80)
        # Track 4 Is Escapism" by Colour Walls.
        m=re.search(r'(?i)\btrack\s*\d+\s+(?:is\s+)?(?P<title>[A-Za-z][A-Za-z0-9\w &.\'!-]{2,70}?)"\s+by\s+(?P<artist>[A-Za-z][\w &.\'-]{2,65})$',line)
        if m:add(m['artist'],m['title'],'unquoted_artist_broken_title',0.75)
        m=re.search(r'(?i)\b(?:last|first|next)\s+track\s+(?:the\s+awesome\s+)?(?P<title>[A-Za-z][\w &.\'-]{2,80}?)\s+by\s+"(?P<artist>[^"]{2,110})"',line)
        if m:add(m['artist'],m['title'],'last_track_unquoted_title',0.83)
        # "She will be known" and " Give it all are available here (unclosed title).
        if re.search(r'(?i)\bsongs?\s+\d+\s+and\s+\d+\s+are\s+by\b',line):
            am=BY_Q.search(line)
            tm=re.search(r'(?i)\band\s+"(?P<title>[^"\n]{2,110}?)\s+are\s+available\b',line)
            if am and tm:add(am['artist'],tm['title'],'unclosed_second_group_title',0.75)

    # Missing 'by': 'Track 2 is called "I ain't Buying " The Witch Kids"'.
    if cue and not items:
        m=re.search(r'(?i)\btrack\s*\d+\s+is\s+called\s+"(?P<title>[^"]{2,90})"\s+(?P<artist>[A-Z][^"\n]{2,75})"',line)
        if m:add(m['artist'],m['title'],'missing_by_after_title',0.72)

    # Older 2014-2016 loose-language credits.
    if cue:
        m=re.search(r'(?i)\b(?:the\s+)?music\s+is\s+by\s+(?:a\s+band\s+called\s+)?(?P<artist>[A-Za-z][\w &.\'-]{2,70}?),\s+the\s+sound\s+is\s+"(?P<title>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'older_sound_is',0.90)
        m=re.search(r'(?i)\bband\s+from\s+\w+\s+called\s+(?P<artist>[A-Za-z][A-Za-z ]{2,65})\s+with\s+the\s+track\s+name\s+of\s+(?P<title>[A-Za-z][\w &.\'-]{2,70})',line)
        if m:add(m['artist'],m['title'],'band_from_country',0.83)
        m=re.search(r'(?i)\bmusic\s+by\s+(?P<artist>[A-Za-z][\w &.\'-]{2,60})\.\s*track\s+is\s+called\s+(?P<title>[A-Za-z][\w &.\'-]{2,70}?)(?=\s+and\s+can\b|[.,;]|$)',line)
        if m:add(m['artist'],m['title'],'music_artist_then_track',0.87)
        m=re.search(r'(?i)\b(?:song|track)\s+(?:is\s+)?by\s+(?P<artist>[A-Za-z0-9][\w &.\'-]{2,60}?)\s+and\s+is\s+called\s+"(?P<title>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'plain_artist_and_called',0.90)
        m=re.search(r'(?i)\bband\s+"(?P<artist>[^"]+)"\s+and\s+track\s+is\s+"(?P<title>[^"]+)"',line)
        if m:add(m['artist'],m['title'],'band_and_track',0.92)
        m=re.search(r'(?i)\b(?:music|the\s+music)\s+is\s+by\s+"(?P<artist>[^"]+)"\s+and\s+the\s+track\s+is\s+(?P<title>[A-Za-z][\w &.\'-]{2,55}?)(?=,\s+search|\s+search\b|[.;]|$)',line)
        if m:add(m['artist'],m['title'],'quoted_artist_unquoted_track',0.87)
        m=re.search(r'(?i)\b(?:\d+\s+tracks?|tracks?\s*\d+|library music apart from \d+ track)\s+by\s+the\s+band\s+"(?P<artist>[^"]+)"\s+called\s+(?P<titles>.+)',line)
        if m:
            for t in QUOTE.findall(m['titles']):add(m['artist'],t,'band_group_from_library_sentence',0.94)

    # De-duplicate within line, prefer higher confidence.
    out={}
    for a,t,rule,conf in items:
        k=(a.casefold(),t.casefold())
        if k not in out or out[k][3] < conf:out[k]=(a,t,rule,conf)
    return list(out.values())


def extract_description(row: dict) -> tuple[list[dict],list[dict]]:
    lines=logical_lines(row.get('description',''))
    found=[]; review=[]; recent_artist=None; recent_line=-100
    waiting_for_title_artist=None; waiting_for_title_line=-100
    for txt, in_music, lin in lines:
        # No attempt to guess a song from generic invitation or social links.
        if re.search(r'(?i)\b(?:send your tracks|want your music|music on this channel is from viewers|subscribe|my music is)\b',txt): continue
        hits=extract_line(txt,in_music)
        # The previous line ended with '... and the song is called' and a
        # quoted title follows immediately on the next line.
        if not hits and waiting_for_title_artist and lin - waiting_for_title_line <= 2:
            quoted=re.match(r'^"(?P<title>[^"\n]{2,110})"(?:\s|$)',txt)
            if quoted:
                hits=[(waiting_for_title_artist,quoted['title'],
                       'artist_and_quoted_title_next_line',0.94)]
        waiting_for_title_artist=None
        # Cross-line: artist is on an explicit MUSIC BY line, title on the next line.
        if not hits and recent_artist and lin-recent_line<=3:
            m=re.match(r'(?i)^(?:the\s+)?song\s*[-–:]\s*(?P<title>[A-Za-z][\w &.\'-]{2,60}?)(?=\s+(?:itunes|iTunes|itues|link)\b|$)',txt)
            if m:hits=[(recent_artist,m['title'],'artist_previous_line_unquoted_title',0.82)]
        # Cross-line: explicit band credit followed immediately by a title line.
        if not hits and recent_artist and lin-recent_line<=3 and in_music:
            m=re.search(r'(?i)\b(?:songs?|tracks?)\s*(?:is\s+)?(?:called|named|\-|:)\s*"([^"]{2,110})"',txt)
            if m:hits=[(recent_artist,m.group(1),'previous_line_artist',0.79)]
        if hits:
            for artist,title,rule,confidence in hits:
                found.append({'channel':row['channel'],'video_id':row['video_id'],
                  'published_at':row['published_at'],'video_title':row['title'],'video_url':row['url'],
                  'artist':artist,'track':title,'rule':rule,'confidence':f'{confidence:.2f}',
                  'evidence':txt,'line_number':lin})
            recent_artist=hits[-1][0];recent_line=lin
            continue
        # Never infer a title from an unrelated nearby quote: demand a complete
        # artist attribution ending with an explicit 'song is called'.
        starts_title=re.search(r'(?i)\bmusic\s+is\s+by\s+"(?P<artist>[^"\n]{2,100})".{0,120}\b(?:song|track)\s+is\s+called\s*$',txt)
        if starts_title:
            waiting_for_title_artist=starts_title['artist']
            waiting_for_title_line=lin
        if SOMETHING.search(txt) and not LIBRARY.search(txt) and not re.match(r'(?i)^\s*(?:music|songs?|track)\s*$',txt):
            # Try to retain band without song for next line.
            m=BY_Q.search(txt)
            if m: recent_artist=m.group('artist');recent_line=lin
            m=re.match(r'(?i)^music\s+(?:is\s+)?by\s+(?P<artist>March to the Grave)\s*$',txt)
            if m: recent_artist=m['artist'];recent_line=lin
            if in_music or re.search(r'(?i)\b(?:song|track|music)\b.{0,45}\b(?:is|are|called|by)\b',txt):
                if not re.search(r'(?i)\b(?:track|song)\s*(?:to\s+|for\s+|in\s+)?(?:me|us|here|you)\b',txt):
                    review.append({'channel':row['channel'],'video_id':row['video_id'],
                      'video_title':row['title'],'video_url':row['url'],'line_number':lin,
                      'reason':'music_reference_not_parsed','evidence':txt})
    # Explicit Bandcamp /track/ URL in a sentence saying this is the full song.
    original=normalize(row.get('description',''))
    for m in re.finditer(r'(?i)(?P<full>(?:music\s+is\s+by|background\s+song\s+by)\s+(?P<artist>[A-Za-z][A-Za-z ]{2,55}?)\s+and\s+the\s+full\s+(?:song|version)\b[^\n]{0,210}?https?://[^/\s]+\.bandcamp\.com/track/(?P<slug>[a-z0-9-]+))',original):
        artist=clean_name(m['artist']);title=m['slug'].replace('-',' ').title()
        if valid(artist) and valid(title):
            found.append({'channel':row['channel'],'video_id':row['video_id'],'published_at':row['published_at'],
                'video_title':row['title'],'video_url':row['url'],'artist':artist,'track':title,
                'rule':'explicit_linked_bandcamp_track','confidence':'0.82',
                'evidence':m['full'],'line_number':original[:m.start()].count('\n')+1})
    # Better selected no duplicate repeated credits in same video (e.g., Track 8/9 referenced twice).
    unique={}
    for item in found:
        k=(item['video_id'],re.sub(r'[^a-z0-9]+','',item['artist'].casefold()),re.sub(r'[^a-z0-9]+','',item['track'].casefold()))
        if k not in unique or float(item['confidence'])>float(unique[k]['confidence']): unique[k]=item
    return list(unique.values()),review




VERSION_WORDS = re.compile(r'\b(?:demo|live|acoustic|instrumental|remix|remaster(?:ed)?|radio edit|sped up|slowed|cover|karaoke|alternate(?: version| take)?|version)\b', re.I)


def policy_normalize(value: str, *, artist: bool = False) -> str:
    value = unicodedata.normalize('NFKD', value or '').casefold()
    value = ''.join(c for c in value if not unicodedata.combining(c))
    value = value.replace('&', ' and ').replace('+', ' ')
    value = re.sub(r"[’']", '', value)
    value = re.sub(r'[^a-z0-9]+', ' ', value)
    value = ' '.join(value.split())
    if artist and value.startswith('the '):
        value = value[4:]
    return value


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def component_similarity(a: str, b: str, *, artist: bool = False) -> float:
    aa, bb = policy_normalize(a, artist=artist), policy_normalize(b, artist=artist)
    if not aa or not bb:
        return 0.0
    # Accept a missing space (Youth 8500 / Youth8500, Cyberneticwitchcult).
    return max(ratio(aa, bb), ratio(aa.replace(' ', ''), bb.replace(' ', '')))


def title_is_safe_variant(expected: str, actual: str) -> bool:
    a, b = policy_normalize(expected), policy_normalize(actual)
    if not a or not b:
        return False
    if a.replace(' ', '') == b.replace(' ', ''):
        return True
    if VERSION_WORDS.search(actual) or VERSION_WORDS.search(expected):
        return False  # A demo/remix is not automatically the same recording.
    x, y = a.split(), b.split()
    if len(x) != len(y):
        return False  # Heart Dotted != Heart Dotted I; extra words need review.
    if len(x) == 1 and min(len(x[0]), len(y[0])) < 5:
        return False
    return all(u == v or (min(len(u), len(v)) >= 4 and ratio(u, v) >= .77)
               for u, v in zip(x, y))


@dataclass(frozen=True)
class Assessment:
    score: float
    artist_score: float
    title_score: float
    status: str
    reason: str


def assess(artist: str, title: str, candidate: dict | None, *, min_score: float = 90.0) -> Assessment:
    if not candidate:
        return Assessment(0, 0, 0, 'not_found', 'no Spotify candidates')
    names = [a.get('name', '') for a in candidate.get('artists', [])]
    if not names or not candidate.get('name'):
        return Assessment(0, 0, 0, 'review', 'candidate has missing artist/title')
    ar = max(component_similarity(artist, name, artist=True) for name in names)
    tr = component_similarity(title, candidate['name'])
    score = round(100 * (.54 * ar + .46 * tr), 1)
    ar_pct, tr_pct = round(100*ar,1), round(100*tr,1)
    if score < min_score:
        return Assessment(score,ar_pct,tr_pct,'review', 'combined score below threshold')
    if ar < .88:
        return Assessment(score,ar_pct,tr_pct,'review','artist below 88%')
    if tr < .88:
        return Assessment(score,ar_pct,tr_pct,'review','title below 88%')
    if not title_is_safe_variant(title, candidate['name']):
        return Assessment(score,ar_pct,tr_pct,'review','different wording or version: review')
    return Assessment(score,ar_pct,tr_pct,'likely_match','artist/title match; safe spelling variation')


def rank(artist: str, title: str, candidates: list[dict], *, min_score: float = 90.0):
    """Prefer safe matches above higher-scoring but incompatible candidates."""
    evaluated = [(candidate, assess(artist,title,candidate,min_score=min_score)) for candidate in candidates]
    evaluated.sort(key=lambda x: (x[1].status=='likely_match', x[1].score, x[1].artist_score, x[1].title_score), reverse=True)
    return evaluated



# --- Stateless daily orchestration: no persistence except the public audit JSON. ---
YOUTUBE_CHANNELS = {
    "colinfurze": "UCp68_FLety0O-n9QU6phsgw",
    "2muchcolinfurze": "UCdmcAwbdiXQWs9d4Qd4KzDw",
}
SPOTIFY_PLAYLIST = "59DazYEmOTJlmDb1fLAkCd"
WINDOW_HOURS = 48
VIDEO_PAGE_SIZE = 50
ROOT = Path(__file__).resolve().parent
VIDEOS_FILE = ROOT / "data" / "videos.json"
RUNS_FILE = ROOT / "data" / "runs.jsonl"
REQUIRED = ("YOUTUBE_API_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET",
            "SPOTIFY_REFRESH_TOKEN")
TRACK_URI = re.compile(r"^spotify:track:[A-Za-z0-9]{22}$")


class ApiError(Exception):
    pass


def api_json(url, *, method="GET", payload=None, headers=None, form=False):
    """Small standard-library HTTP client. Never log API keys or OAuth headers."""
    hdrs = dict(headers or {})
    body = None
    if payload is not None:
        if form:
            body = urlencode(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
    for attempt in range(4):
        try:
            req = Request(url, data=body, method=method, headers=hdrs)
            with urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            if error.code == 429 and attempt < 3:
                try:
                    pause = int(error.headers.get("Retry-After", "2"))
                except (ValueError, TypeError):
                    pause = 2
                time.sleep(max(1, min(pause, 30)))
                continue
            if error.code >= 500 and method == "GET" and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise ApiError(f"{method} HTTP {error.code}") from None
        except (URLError, TimeoutError):
            if method == "GET" and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            # Do not retry an ambiguous POST: it may already have been applied.
            raise ApiError(f"{method} network error; next run can reconcile") from None
    raise ApiError("HTTP retries exhausted")


def yt(endpoint, **params):
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    return api_json("https://www.googleapis.com/youtube/v3/" + endpoint
                    + "?" + urlencode(params))


def spotify_api(endpoint, token, *, method="GET", payload=None, params=None):
    url = "https://api.spotify.com/v1" + endpoint
    if params:
        url += "?" + urlencode(params)
    return api_json(url, method=method, payload=payload,
                    headers={"Authorization": "Bearer " + token})


def spotify_token(grant, *, user=False):
    cid = os.environ["SPOTIFY_CLIENT_ID"]
    secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    encoded = base64.b64encode(f"{cid}:{secret}".encode("utf-8")).decode("ascii")
    payload = ({"grant_type":"refresh_token",
                "refresh_token":os.environ["SPOTIFY_REFRESH_TOKEN"]}
               if user else {"grant_type":"client_credentials"})
    result = api_json("https://accounts.spotify.com/api/token", method="POST",
                      payload=payload, form=True,
                      headers={"Authorization": "Basic " + encoded})
    if "access_token" not in result:
        raise ApiError("Spotify token response missing access_token")
    return result["access_token"]


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def recent_uploads(now):
    """Inspect newest 50 uploads per channel; filter using videos.list publication date.

    Use video snippet.publishedAt, NOT upload-playlist videoPublishedAt, to include
    videos that were private and only recently became publicly available.
    """
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    videos = []
    for label, channel_id in YOUTUBE_CHANNELS.items():
        response = yt("channels", part="contentDetails", id=channel_id)
        items = response.get("items") or []
        if len(items) != 1:
            raise ApiError(f"YouTube channel {label} unavailable")
        playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        newest = yt("playlistItems", part="contentDetails", playlistId=playlist,
                    maxResults=VIDEO_PAGE_SIZE).get("items") or []
        ids = [entry.get("contentDetails", {}).get("videoId") for entry in newest]
        ids = [item for item in ids if item]
        if not ids:
            raise ApiError(f"No uploads returned for {label}")
        result = yt("videos", part="snippet", id=",".join(ids), maxResults=50)
        by_id = {item["id"]: item for item in result.get("items", [])}
        for video_id in ids:
            record = by_id.get(video_id)
            if record is None:
                continue
            snippet = record.get("snippet") or {}
            published = snippet.get("publishedAt")
            if not published or not cutoff <= utc(published) <= now:
                continue
            videos.append({
                "video_id":video_id, "channel":label,
                "published_at":published, "title":snippet.get("title", ""),
                "url":"https://www.youtube.com/watch?v=" + video_id,
                "description":snippet.get("description") or "",
            })
        print(f"{label}: {sum(v['channel']==label for v in videos)} recent videos")
    return sorted(videos, key=lambda v:(v["published_at"],v["video_id"]), reverse=True)


def norm_credit(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", "".join(c for c in value
                      if not unicodedata.combining(c))).split())


def search_track(artist, title, catalog_token):
    def query(q):
        result = spotify_api("/search", catalog_token,
                             params={"q":q, "type":"track", "limit":10, "market":"NL"})
        return (result.get("tracks") or {}).get("items") or []
    candidates = query(f'track:"{title}" artist:"{artist}"')
    if not candidates:
        candidates = query(artist + " " + title)
    if not candidates:
        return {"status":"not_found", "score":0, "spotify_uri":None}
    winner, assessment = rank(artist, title, candidates)[0]
    candidate_uri = winner.get("uri", "")
    if not TRACK_URI.fullmatch(candidate_uri):
        return {"status":"review", "score":assessment.score, "spotify_uri":None}
    return {"status":assessment.status, "score":assessment.score,
            "spotify_uri":candidate_uri if assessment.status=="likely_match" else None,
            "spotify_title":winner.get("name"),
            "spotify_artist":", ".join(a.get("name","") for a in winner.get("artists",[]))}


def playlist_items(token):
    result=[]
    for offset in range(0, 10000, 50):
        page=spotify_api(f"/playlists/{SPOTIFY_PLAYLIST}/items",token,
                         params={"limit":50,"offset":offset})
        for entry in (page.get("items") or []):
            item=(entry.get("item") or entry.get("track") or {})
            uri=item.get("uri")
            if not uri:
                raise ApiError("Playlist has unavailable item; refusing to write")
            result.append(uri)
        if not page.get("next"):
            return result
    raise ApiError("Spotify playlist pagination unexpectedly long")


def prepend_missing(token, wanted):
    """Add only missing tracks; no deletions or description writes."""
    owner=spotify_api("/me",token)["id"]
    playlist=spotify_api("/playlists/"+SPOTIFY_PLAYLIST,token)
    if (playlist.get("owner") or {}).get("id") != owner:
        raise ApiError("Spotify playlist not owned by authenticated account")
    original=playlist_items(token)
    if len(original)!=len(set(original)):
        raise ApiError("Existing playlist contains duplicates; refusing writes")
    missing=[]
    known=set(original)
    for uri in wanted:
        if uri not in known:
            known.add(uri)
            missing.append(uri)
    if not missing:
        return 0
    # Oldest chunk first; new videos sit above existing playlist.
    for end in range(len(missing), 0, -100):
        chunk=missing[max(0,end-100):end]
        before=playlist_items(token)
        # Safe recovery: if an earlier chunk was already inserted, do not duplicate.
        chunk=[uri for uri in chunk if uri not in before]
        if not chunk:
            continue
        spotify_api(f"/playlists/{SPOTIFY_PLAYLIST}/items",token,method="POST",
                    payload={"uris":chunk,"position":0})
        after=playlist_items(token)
        if after != chunk + before:
            raise ApiError("Spotify playlist order differs after insert; manual check needed")
    final=playlist_items(token)
    if final != missing + original:
        raise ApiError("Spotify playlist changed during sync; manual check needed")
    return len(missing)


def load_history():
    if not VIDEOS_FILE.exists():
        return {}
    data = json.loads(VIDEOS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("videos.json must contain a video_id-to-video object")
    return data


def save_history(data):
    VIDEOS_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=VIDEOS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
                   + "\n",encoding="utf-8")
    tmp.replace(VIDEOS_FILE)


def log_run(result):
    RUNS_FILE.parent.mkdir(parents=True,exist_ok=True)
    with RUNS_FILE.open("a",encoding="utf-8") as out:
        out.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


def daily(now=None):
    now = now or datetime.now(timezone.utc)
    missing_secrets=[name for name in REQUIRED if not os.getenv(name)]
    if missing_secrets:
        raise ValueError("Missing secrets: " + ", ".join(missing_secrets))
    seen = recent_uploads(now)
    history=load_history()
    extracted={}
    grouped={}
    pending=[]
    for row in seen:
        credits, review=extract_description(row)
        details={"channel":row["channel"],"title":row["title"],
                 "published_at":row["published_at"],"url":row["url"],
                 "credits":[],"review_lines":len(review),"checked_at":now.isoformat(),
                 "status":"checked"}
        for credit in credits:
            key=(norm_credit(credit["artist"]), norm_credit(credit["track"]))
            if key not in grouped:
                grouped[key]={"artist":credit["artist"],"track":credit["track"]}
            pending.append((row["video_id"],key,credit))
        extracted[row["video_id"]]=details

    if pending:
        token=spotify_token("client_credentials")
        matches={key:search_track(item["artist"],item["track"],token)
                 for key,item in grouped.items()}
        for video_id,key,credit in pending:
            result=matches[key]
            extracted[video_id]["credits"].append({
                "artist":credit["artist"],"track":credit["track"],
                "confidence":float(credit["confidence"]),
                "match_status":result["status"],
                "match_score":result["score"],
                "spotify_uri":result["spotify_uri"],
            })
    else:
        matches={}

    # API writes are done before marking this run as fully synchronized.
    uris=[]
    for row in seen:
        for credit in extracted[row["video_id"]]["credits"]:
            uri=credit.get("spotify_uri")
            if uri and uri not in uris:
                uris.append(uri)
    count=0
    if uris:
        user_token=spotify_token("refresh_token",user=True)
        count=prepend_missing(user_token,uris)
    for vid,details in extracted.items():
        history[vid]=details
    save_history(history)
    return {"videos_checked":len(seen),"credits_found":len(pending),
            "approved_tracks":len(uris),"spotify_added":count,
            "reviews":sum(m["status"]!="likely_match" for m in matches.values())}


def main():
    started=datetime.now(timezone.utc).isoformat(timespec="seconds")
    report={"timestamp_utc":started, "status":"ok"}
    try:
        report.update(daily())
        print("OK", report,flush=True)
        code=0
    except Exception as exc:
        # Only exception CLASS to public run history; never secrets or URLs.
        report.update(status="error",error_type=type(exc).__name__)
        print(f"Daily sync failed: {type(exc).__name__}", flush=True)
        code=1
    try:
        log_run(report)
    except OSError:
        print("Failed to write public run summary",flush=True)
        code=1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
