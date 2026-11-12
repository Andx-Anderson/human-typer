"""Human Typer - types pasted text into whatever window has focus, with human-like timing.

Hit Start, click where you want the text during the countdown, and it types.
F8 pauses/resumes (with a 3s countdown so you can click back in), Esc stops.
"""

import ctypes
import json
import os
import random
import threading
import time
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
import pyperclip
from pynput import keyboard

Key = keyboard.Key

# Neighbouring keys on a QWERTY layout, used for realistic typos.
NEIGHBOURS = {
    "q": "wa", "w": "qeas", "e": "wrsd", "r": "etdf", "t": "ryfg", "y": "tugh",
    "u": "yihj", "i": "uojk", "o": "ipkl", "p": "ol", "a": "qwsz", "s": "awedxz",
    "d": "serfcx", "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "j": "huikmn",
    "k": "jiolm", "l": "kop", "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb",
    "b": "vghn", "n": "bhjm", "m": "njk",
}
LEFT_HAND = set("qwertasdfgzxcvb")
SHIFTED = set('~!@#$%^&*()_+{}|:"<>?')
# The most frequent English letter pairs come out fastest because they are drilled the most.
COMMON_BIGRAMS = set(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le "
    "ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur".split())
COMMON_WORDS = set(
    "the of and to a in is it you that he was for on are with as i his they be at one have this "
    "from or had by not but what all were we when your can said there use an each which she do "
    "how their if will up other about out many then them these so some her would make like him "
    "into time has look two more go see no way could people my than first been who its now "
    "find long down day did get come made may part".split())

SENTENCE_END = ".!?"
CLAUSE_END = ",;:"
# Characters a code editor may auto-close by inserting a partner after the cursor.
OPENERS = "([{\"'`<"
CLOSERS = ")]}"
QUOTES = "\"'`"
PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "`": "`"}
# VS Code only auto-closes when the next character is one of these (or the line is empty).
CLOSE_BEFORE = set(";:.,=}])> \t")

INDENT_UNITS = {"4 spaces": "    ", "2 spaces": "  ", "Tab": "\t"}

CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "HumanTyper" / "settings.json"


def indent_width(s):
    return sum(4 if c == "\t" else 1 for c in s)


def leading_ws(s):
    return s[: len(s) - len(s.lstrip(" \t"))]


def in_string(line):
    """True if the line ends inside an unclosed string literal."""
    quote = None
    escaped = False
    for c in line:
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif quote:
            if c == quote:
                quote = None
        elif c in QUOTES:
            quote = c
    return quote is not None


def is_word_char(c):
    return c.isalnum() or c == "_"


class EditorModel:
    """Predicts what a code editor auto-adds, so Natural mode never has to read the screen.

    `line` is the current line up to the cursor; `pending` is everything after the cursor,
    which is only ever stuff the editor added (we always type at the end of the file).
    """

    def __init__(self, profile, unit, python):
        self.profile = profile  # "vs code" | "intellij" | "plain"
        self.unit = unit
        self.python = python
        self.line = ""
        self.pending = ""

    @property
    def smart(self):
        return self.profile != "plain"

    def autocloses(self, ch, before):
        nxt = self.pending.split("\n", 1)[0][:1]  # what follows on THIS line; "" = end of line
        if ch == "{" and self.profile == "intellij":
            return False  # IntelliJ adds } when you press Enter, not when you type {
        if nxt and nxt not in CLOSE_BEFORE:
            return False
        if ch in QUOTES and (is_word_char(before[-1:]) or in_string(before)):
            return False
        return True

    def type(self, ch):
        before = self.line
        self.line += ch
        if self.smart and ch in PAIRS and self.autocloses(ch, before):
            self.pending = PAIRS[ch] + self.pending

    def right(self):
        self.line += self.pending[:1]
        self.pending = self.pending[1:]

    def enter(self):
        head = self.line
        rest_line, sep, rest = self.pending.partition("\n")
        if not self.smart:
            self.line, self.pending = "", self.pending
            return
        last = head.rstrip()[-1:]  # "" on an empty line, and "" is "in" every string, so check it
        indent = leading_ws(head)
        bracket = last != "" and last in "{(["
        opens = bracket or (self.python and last == ":")
        if self.profile == "intellij" and last == "{" and not rest_line.strip():
            rest_line = "}" + rest_line.strip()
        self.line = indent + (self.unit if opens else "")
        if bracket and rest_line[:1] == PAIRS[last]:
            # Enter between an auto-closed pair: the closer drops to its own line.
            self.pending = "\n" + indent + rest_line + sep + rest
        else:
            self.pending = rest_line + sep + rest

    def tab(self):
        if self.smart and self.unit != "\t":
            size = len(self.unit)
            self.line += " " * (size - len(self.line) % size)
        else:
            self.line += "\t"

    def backspace(self):
        if self.smart and self.line and not self.line.strip() and self.unit != "\t":
            size = len(self.unit)
            self.line = self.line[: -((len(self.line) - 1) % size + 1)]  # back to the last tab stop
        else:
            self.line = self.line[:-1]

    def delete_back(self, n):
        self.line = self.line[:-n] if n else self.line

    def move_past(self, k):
        consumed = self.pending[: k + 1]
        self.pending = self.pending[k + 1:]
        if "\n" in consumed:
            self.line = consumed.split("\n")[-1]
        else:
            self.line += consumed


class Typer:
    """Runs the typing loop on a background thread."""

    def __init__(self, settings, on_status, on_progress, on_done):
        self.s = settings
        self.on_status = on_status
        self.on_progress = on_progress
        self.on_done = on_done
        self.kb = keyboard.Controller()
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.model = None
        self.last = ""  # last character sent, for deciding whether a popup might be open
        # Current speed wanders inside [wpm_min, wpm_max] so the pace has bursts and slumps.
        self.wpm = random.uniform(settings["wpm_min"], settings["wpm_max"])
        self.fatigue_secs = 0.0
        self.next_break = self.break_interval()
        self.started = None  # set when typing actually begins, after the countdown
        self.paused_secs = 0.0

    # -- timing ---------------------------------------------------------------

    def wait(self, seconds):
        """Sleep in small slices so pause/stop react instantly."""
        end = time.perf_counter() + seconds
        while not self.stopped.is_set():
            if self.paused.is_set():
                self.on_status("Paused. Press F8 to resume.")
                paused_at = time.perf_counter()
                while self.paused.is_set() and not self.stopped.is_set():
                    time.sleep(0.05)
                self.countdown(3, "Resuming")
                self.paused_secs += time.perf_counter() - paused_at
                end = time.perf_counter() + max(0.0, end - time.perf_counter())
            remaining = end - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))

    def countdown(self, n, label):
        for i in range(n, 0, -1):
            if self.stopped.is_set():
                return
            self.on_status(f"{label} in {i}. Click where you want to type.")
            time.sleep(1)

    def key_delay(self, text, i):
        """Delay before typing text[i]: drifting speed, fatigue, and how hard the key is."""
        lo, hi = self.s["wpm_min"], self.s["wpm_max"]
        self.wpm += random.gauss(0, max(hi - lo, 1) * 0.04)
        if self.wpm < lo:  # bounce off the edges instead of sticking to them
            self.wpm = 2 * lo - self.wpm
        if self.wpm > hi:
            self.wpm = 2 * hi - self.wpm
        self.wpm = min(hi, max(lo, self.wpm))
        base = 60.0 / (self.wpm * 5)  # 5 chars per word

        # Fatigue: a little slower for every 10 minutes of typing, capped at +40%.
        base *= 1 + min(0.4, self.s["fatigue"] / 100 * self.fatigue_secs / 600)

        ch = text[i] if i < len(text) else ""
        prev = text[i - 1] if 0 < i <= len(text) else ""
        mult = 1.0
        if ch.isupper() or ch in SHIFTED:
            mult *= 1.45  # reaching for Shift
        elif ch.isdigit():
            mult *= 1.3
        elif ch and not ch.isalnum() and ch not in " \n":
            mult *= 1.25
        if (prev + ch).lower() in COMMON_BIGRAMS:
            mult *= 0.75
        elif prev.isalpha() and ch.isalpha():
            alternating = (prev.lower() in LEFT_HAND) != (ch.lower() in LEFT_HAND)
            mult *= 0.92 if alternating else 1.06
        if ch.isalpha() and self.word_at(text, i).lower() in COMMON_WORDS:
            mult *= 0.8  # short familiar words come out in one burst

        sigma = self.s["variation"] / 100 * 0.6
        delay = base * mult * random.lognormvariate(0, sigma)
        self.fatigue_secs += delay
        return delay

    @staticmethod
    def word_at(text, i):
        start = i
        while start > 0 and text[start - 1].isalpha():
            start -= 1
        end = i
        while end < len(text) and text[end].isalpha():
            end += 1
        return text[start:end]

    def break_interval(self):
        every = self.s.get("break_every", 0)
        return time.perf_counter() + every * 60 * random.uniform(0.7, 1.3) if every else float("inf")

    def pause_after(self, text, i):
        """Extra pause after a character, depending on what it was."""
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        punct = self.s["punct_pause"]
        extra = 0.0

        if ch == "\n":
            extra += random.uniform(0.8, 2.5) * punct
        elif ch in SENTENCE_END and nxt in " \n":
            extra += random.uniform(0.35, 1.1) * punct
        elif ch in CLAUSE_END and nxt == " ":
            extra += random.uniform(0.15, 0.5) * punct

        if ch == " ":
            extra += random.uniform(0, self.s["word_gap"])
            # A beat before a long word, like deciding how to spell it.
            nxt_word = self.word_at(text, i + 1) if nxt.isalpha() else ""
            if len(nxt_word) >= 8:
                extra += random.uniform(0.1, 0.4) * min(len(nxt_word) / 8, 2)
            if random.random() < self.s["think_chance"] / 100:
                lo, hi = self.s["think_min"], self.s["think_max"]
                secs = random.uniform(lo, max(lo, hi))
                self.on_status(f"Thinking... ({secs:.1f}s)")
                extra += secs

        # Long breaks only at natural stopping points.
        at_stop = (ch in SENTENCE_END and nxt in " \n") or ch == "\n"
        if at_stop and time.perf_counter() >= self.next_break:
            secs = self.s["break_len"] * random.uniform(0.6, 1.4)
            m, s = divmod(int(secs), 60)
            self.on_status(f"Taking a break ({m}m {s:02d}s)")
            extra += secs
            self.fatigue_secs *= 0.5  # a rest takes some of the tiredness away
            self.next_break = self.break_interval() + secs
        return extra

    # -- keystrokes -----------------------------------------------------------

    def popup_may_be_open(self):
        return is_word_char(self.last)

    def press(self, ch):
        mode = self.s["code_mode"]
        if mode == "classic" and not (ch.isalnum() or ch in "_ "):
            self.kb.tap(Key.esc)
        elif mode == "natural" and ch in "\n\t" and self.popup_may_be_open():
            # Right after a word an autocomplete popup can be open, and Enter/Tab would accept it.
            self.kb.tap(Key.esc)

        if ch == "\n":
            self.kb.tap(Key.enter)
            if self.model:
                self.model.enter()
        elif ch == "\t":
            self.kb.tap(Key.tab)
            if self.model:
                self.model.tab()
        else:
            self.kb.type(ch)
            if self.model:
                self.model.type(ch)
        self.last = ch

        if mode != "classic":
            return
        if ch in OPENERS or ch == "\n":
            # Delete whatever the editor put after the cursor (an auto-closed ) ] } " ...).
            with self.kb.pressed(Key.ctrl, Key.shift):
                self.kb.tap(Key.end)
            self.kb.tap(Key.delete)
        if ch == "\n" and not self.s["editor_indents"]:
            self.strip_auto_indent()

    def tap_backspace(self):
        self.kb.tap(Key.backspace)
        if self.model:
            self.model.backspace()

    def backspace(self, n, fast=False):
        for _ in range(n):
            self.wait(random.uniform(0.03, 0.06) if fast else random.uniform(0.06, 0.14))
            self.tap_backspace()

    def ctrl_backspace(self, n_chars):
        with self.kb.pressed(Key.ctrl):
            self.kb.tap(Key.backspace)
        if self.model:
            self.model.delete_back(n_chars)

    def strip_auto_indent(self):
        """Classic mode: delete the indent the editor added after Enter; the text brings its own.

        Shift+Home alone fails on a whitespace-only line: in VS Code the cursor is already at
        the "first non-whitespace" spot (the line end), so nothing gets selected. A placeholder
        letter gives Home a real first character to jump from, in smart and plain editors alike.
        """
        self.kb.type("x")
        self.kb.tap(Key.esc)  # close the autocomplete popup "x" may have opened
        self.kb.tap(Key.left)  # cursor now sits right after the indent
        with self.kb.pressed(Key.shift):
            self.kb.tap(Key.home)  # select the indent (empty if there was none)
        self.kb.tap(Key.delete)  # remove the indent, or "x" if the selection was empty
        self.kb.tap(Key.delete)  # remove "x", or nothing: we're at the end of the file

    # -- mistakes and revisions -----------------------------------------------

    def maybe_typo(self, text, i):
        """Sometimes fumble this letter, keep going a beat, then notice and fix it."""
        ch = text[i]
        if ch.lower() not in NEIGHBOURS or random.random() >= self.s["typo_rate"] / 100:
            return
        nxt = text[i + 1] if i + 1 < len(text) else ""
        kinds = ["neighbour"] * 5 + ["double"] * 2
        if nxt.isalpha():
            kinds += ["swap"] * 2 + ["skip"]
        kind = random.choice(kinds)
        if kind == "neighbour":
            wrong = random.choice(NEIGHBOURS[ch.lower()])
            typed, resume = [wrong.upper() if ch.isupper() else wrong], i + 1
        elif kind == "double":  # "thhe"
            typed, resume = [ch, ch], i + 1
        elif kind == "swap":  # "teh"
            typed, resume = [nxt, ch], i + 2
        else:  # skipped a letter: "th"
            typed, resume = [nxt], i + 2

        for n, c in enumerate(typed):
            if n:
                self.wait(self.key_delay(text, i))
            self.press(c)

        # Real people often type a letter or two more before noticing.
        overrun = 0
        for _ in range(random.choice([0, 0, 1, 1, 2])):
            j = resume + overrun
            if j >= len(text) or not text[j].isalpha():
                break
            self.wait(self.key_delay(text, j))
            self.press(text[j])
            overrun += 1

        self.wait(random.uniform(0.25, 0.7))  # the "wait, that's wrong" moment
        mistakes = len(typed) + overrun
        start = i
        while start > 0 and text[start - 1].isalpha():
            start -= 1
        if mistakes >= 2 and start > 0 and text[start - 1] == " " and random.random() < 0.35:
            # Noticed late: wipe the whole word with Ctrl+Backspace and type it again.
            self.ctrl_backspace(i - start + mistakes)
            self.wait(random.uniform(0.2, 0.5))
            for j in range(start, i):
                self.wait(self.key_delay(text, j))
                self.press(text[j])
        else:
            self.backspace(mistakes)
        self.wait(random.uniform(0.1, 0.3))

    def maybe_rewrite(self, text, i):
        """After a space, sometimes delete the last few words and type them again (rephrasing)."""
        rate = self.s["rewrite_rate"] / 100 / 12  # setting is per sentence; ~12 words a sentence
        if self.s["code_mode"] != "off" or random.random() >= rate:
            return
        start = i
        for _ in range(random.randint(1, 3)):
            start -= 1
            while start > 0 and text[start - 1] not in " \n":
                start -= 1
        region = text[start:i + 1]
        if start <= 0 or "\n" in region or len(region) > 40 or text[start - 1] != " ":
            return
        self.on_status("Rewording...")
        self.wait(random.uniform(0.4, 1.5))
        self.backspace(len(region), fast=True)  # holding Backspace
        self.wait(random.uniform(0.6, 2.5))
        for j in range(start, i + 1):
            self.wait(self.key_delay(text, j))
            self.press(text[j])

    # -- natural code mode ----------------------------------------------------
    # With an editor picked, EditorModel predicts what the editor adds. With "Auto" we read the
    # editor instead, by selecting and copying (works anywhere, but shows a highlight flash).

    def get_clip(self):
        return pyperclip.paste()

    def set_clip(self, value):
        pyperclip.copy(value)

    def copy_selection(self):
        """Ctrl+C the current selection and return it, or None if nothing arrived."""
        sentinel = f"⁣human-typer-{random.random()}"
        self.set_clip(sentinel)
        with self.kb.pressed(Key.ctrl):
            self.kb.tap("c")
        deadline = time.perf_counter() + 0.6
        while time.perf_counter() < deadline:
            value = self.get_clip()
            if value != sentinel:
                return value.replace("\r\n", "\n")
            time.sleep(0.01)
        return None

    def read_after(self):
        """Everything after the cursor (what the editor auto-added), cursor left where it was.

        Starts the selection one character back so it is never empty: VS Code copies the whole
        line when you Ctrl+C with nothing selected.
        """
        self.kb.tap(Key.esc)  # close autocomplete so arrow keys move the cursor, not the popup
        self.kb.tap(Key.left)
        with self.kb.pressed(Key.ctrl, Key.shift):
            self.kb.tap(Key.end)
        value = self.copy_selection()
        self.kb.tap(Key.left)  # collapse to the selection start...
        self.kb.tap(Key.right)  # ...and step back to where we were
        return value[1:] if value else None

    def read_line_before(self):
        """The current line up to the cursor, i.e. the indent the editor gave this line."""
        with self.kb.pressed(Key.ctrl, Key.shift):
            self.kb.tap(Key.home)
        value = self.copy_selection()
        self.kb.tap(Key.right)  # collapse to the selection end, where the cursor was
        return None if value is None else value.split("\n")[-1]

    def editor_after(self, i):
        if self.model:
            return self.model.pending
        return self.read_after() if i > 0 else None

    def editor_line(self):
        return self.model.line if self.model else self.read_line_before()

    def fix_indent(self, target):
        """Nudge the editor's auto-indent to the text's indent with Tab/Backspace, like a person."""
        goal = indent_width(target)
        for _ in range(20):
            line = self.editor_line()
            if line is None:  # couldn't read the editor; type the indent and hope
                for c in target:
                    self.press(c)
                return
            if line.strip():  # the editor put real text here; leave it alone
                return
            have = indent_width(line)
            if have == goal:
                return
            self.wait(random.uniform(0.05, 0.15))
            if have > goal:
                self.tap_backspace()
            elif goal - have >= 4 and not (self.model and not self.model.smart):
                self.press("\t")
            else:
                for c in " " * (goal - have):
                    self.press(c)

    def move_past(self, after, k):
        """Step over a closer the editor already added, k characters after the cursor."""
        lines_down = after[:k].count("\n")
        if lines_down == 0:
            for _ in range(k + 1):
                self.kb.tap(Key.right)
        else:
            if self.popup_may_be_open():
                self.kb.tap(Key.esc)  # Down would move through the popup instead of the text
            for _ in range(lines_down):
                self.kb.tap(Key.down)
            self.kb.tap(Key.end)
            for _ in after[k + 1:].split("\n")[0]:  # back up over anything after it, like "});"
                self.kb.tap(Key.left)
        if self.model:
            self.model.move_past(k)
        self.last = after[k]

    def run_natural(self, text):
        total = len(text)
        i = 0
        while i < total and not self.stopped.is_set():
            ch = text[i]
            self.wait(self.key_delay(text, i))
            if self.stopped.is_set():
                break

            if ch in CLOSERS + QUOTES:
                # Editor already typed this closer? Then step over it instead of doubling it.
                after = self.editor_after(i)
                if after and after[0] == ch:
                    self.move_past(after, 0)
                    self.wait(self.pause_after(text, i))
                    i += 1
                    self.report(i, total)
                    continue
            elif ch == "\n":
                # Next line is just a closer, like "}"? The editor may already have put it there.
                j = i
                while j < total and text[j] in " \t\n":
                    j += 1
                if j < total and text[j] in CLOSERS:
                    after = self.editor_after(i)
                    k = next((n for n, c in enumerate(after or "") if c not in " \t\n"), None)
                    if k is not None and after[k] == text[j]:
                        self.move_past(after, k)
                        self.wait(self.pause_after(text, i))
                        i = j + 1
                        self.report(i, total)
                        continue

            if ch == "\n":
                self.press("\n")
                self.wait(self.pause_after(text, i))
                i += 1
                j = i
                while j < total and text[j] in " \t":
                    j += 1
                if j < total and text[j] != "\n":  # blank lines keep whatever the editor gave them
                    self.fix_indent(text[i:j])
                i = j
            else:
                self.maybe_typo(text, i)
                self.press(ch)
                self.wait(self.pause_after(text, i))
                i += 1
            if i % 10 == 0 or i >= total:
                self.report(min(i, total), total)

    # -- main loop ------------------------------------------------------------

    done_chars = 0

    def actual_wpm(self, chars):
        """Real speed so far: everything counts (pauses, breaks, typos) except F8 pauses."""
        if self.started is None:
            return 0.0
        minutes = (time.perf_counter() - self.started - self.paused_secs) / 60
        return chars / 5 / minutes if minutes > 0 else 0.0

    def report(self, done, total):
        self.done_chars = done
        self.on_progress(done, total, self.actual_wpm(done))

    def run(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        mode = self.s["code_mode"]
        if mode == "classic" and self.s["editor_indents"]:
            # The editor places every line itself, so drop the text's own indentation.
            text = "\n".join(line.lstrip(" \t") for line in text.split("\n"))
        self.countdown(self.s["countdown"], "Starting")
        self.next_break = self.break_interval()
        self.started = time.perf_counter()
        if mode == "natural":
            if self.s["editor"] != "auto":
                lines = [l.rstrip() for l in text.split("\n")]
                python = sum(l.endswith(":") for l in lines) > sum(l.endswith("{") for l in lines)
                self.model = EditorModel(self.s["editor"], self.s["indent_unit"], python)
                self.run_natural(text)
            else:
                try:
                    saved = self.get_clip()  # Auto copies to read the editor; put this back
                except Exception:
                    saved = None
                try:
                    self.run_natural(text)
                finally:
                    if saved is not None:
                        self.set_clip(saved)
        else:
            self.run_plain(text)
        wpm = self.actual_wpm(len(text) if not self.stopped.is_set() else self.done_chars)
        self.on_done(("Stopped." if self.stopped.is_set() else "Done.") + f"  Average {wpm:.0f} WPM.")

    def run_plain(self, text):
        total = len(text)
        i = 0
        while i < total and not self.stopped.is_set():
            ch = text[i]
            self.wait(self.key_delay(text, i))
            if self.stopped.is_set():
                break
            self.maybe_typo(text, i)
            self.press(ch)
            self.wait(self.pause_after(text, i))
            if ch == " ":
                self.maybe_rewrite(text, i)
            i += 1
            if i % 10 == 0 or i == total:
                self.report(i, total)


# -- UI -----------------------------------------------------------------------

# (light, dark) pairs. Neutral greys only; the primary button is the one strong mark.
BG = ("#f4f4f5", "#161618")
CARD = ("#ffffff", "#1f1f22")
BORDER = ("#e4e4e7", "#2c2c30")
TEXT = ("#18181b", "#ececee")
MUTED = ("#71717a", "#8e8e96")
TRACK = ("#e4e4e7", "#34343a")
FILL = ("#52525b", "#a1a1aa")
GHOST_HOVER = ("#ececee", "#2a2a2e")

FONT = "Segoe UI"

MODE_HELP = {
    "Off": "For essays, Docs, Word, normal text boxes.",
    "Natural": "For code editors that auto-add brackets, quotes and indents. Steps over what the "
               "editor added and fixes indents with Tab and Backspace, like a person. Works for "
               "Python too. Type into an empty file or at the end of one.",
    "Classic": "Fallback if Natural misbehaves in some editor. Deletes what the editor auto-adds "
               "and retypes the indent. Type into an empty file or at the end of one.",
}

EDITOR_HELP = {
    "Auto": "Reads the editor by selecting and copying (a quick highlight, and Ctrl+C presses). "
            "Works in any editor.",
    "VS Code": "Predicts VS Code and web IDEs built on it. No copying, no highlight.",
    "IntelliJ": "Predicts IntelliJ and Eclipse. No copying, no highlight.",
    "Plain": "For boxes that add nothing (Notepad, plain text areas). No copying, no highlight.",
}

# Sections of the settings panel: (title, [(key, label, min, max, default, step, unit), ...])
SECTIONS = [
    ("Speed", [
        ("wpm_min", "Min speed", 15, 150, 40, 1, "WPM"),
        ("wpm_max", "Max speed", 15, 150, 65, 1, "WPM"),
        ("variation", "Keystroke randomness", 0, 100, 45, 1, "%"),
        ("fatigue", "Fatigue", 0, 40, 10, 1, "% / 10 min"),
    ]),
    ("Pauses", [
        ("word_gap", "Extra gap between words", 0, 1.0, 0.25, 0.05, "s max"),
        ("punct_pause", "Pause after punctuation", 0, 3.0, 1.0, 0.1, "x"),
        ("think_chance", "Thinking-break chance", 0, 10, 1.5, 0.5, "% / word"),
        ("think_min", "Thinking break min", 0.5, 20, 2, 0.5, "s"),
        ("think_max", "Thinking break max", 0.5, 30, 7, 0.5, "s"),
        ("break_every", "Long break every", 0, 30, 12, 1, "min"),
        ("break_len", "Long break length", 15, 300, 60, 5, "s"),
    ]),
    ("Mistakes", [
        ("typo_rate", "Typo rate", 0, 8, 1.5, 0.5, "% / letter"),
        ("rewrite_rate", "Rewording (essays only)", 0, 30, 8, 1, "% / sentence"),
    ]),
    ("Start", [
        ("countdown", "Countdown", 2, 15, 5, 1, "s"),
    ]),
]


class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.typer = None
        self.vars = {}
        self.refreshers = []
        self.recommended = {}

        self.title("Human Typer")
        self.geometry("1040x720")
        self.minsize(860, 560)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=20, pady=(20, 12))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.build_editor(body)
        self.build_settings(body)
        self.build_footer()

        # Global hotkeys work even while another window has focus.
        self.listener = keyboard.Listener(on_press=self.on_hotkey)
        self.listener.daemon = True
        self.listener.start()
        self.load_settings()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.update_counts()

    # -- building blocks ------------------------------------------------------

    def card(self, parent):
        return ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=12)

    def ghost_button(self, parent, text, command, width=0):
        return ctk.CTkButton(
            parent, text=text, command=command, width=width, height=30, corner_radius=8,
            fg_color="transparent", hover_color=GHOST_HOVER, text_color=TEXT, text_color_disabled=("#b4b4bb", "#4a4a50"),
            border_color=BORDER, border_width=1, font=(FONT, 12))

    def segmented(self, parent, values, variable, command):
        return ctk.CTkSegmentedButton(
            parent, values=values, variable=variable, command=command,
            font=(FONT, 12), height=30, corner_radius=8, fg_color=TRACK, unselected_color=TRACK,
            unselected_hover_color=GHOST_HOVER, selected_color=CARD, selected_hover_color=CARD,
            text_color=TEXT)

    def help_label(self, parent, text=""):
        return ctk.CTkLabel(parent, text=text, justify="left", wraplength=270, font=(FONT, 11), text_color=MUTED)

    def build_editor(self, body):
        editor = self.card(body)
        editor.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(editor, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="Text", font=(FONT, 15, "bold"), text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.counts = ctk.CTkLabel(head, text="", font=(FONT, 12), text_color=MUTED)
        self.counts.grid(row=0, column=1, sticky="w", padx=12)
        self.ghost_button(head, "Load .txt", self.load_file, 84).grid(row=0, column=2, padx=(0, 6))
        self.ghost_button(head, "Clear", lambda: (self.text.delete("1.0", "end"), self.update_counts()), 64).grid(row=0, column=3)

        self.text = ctk.CTkTextbox(
            editor, wrap="word", font=(FONT, 13), fg_color="transparent", text_color=TEXT,
            border_width=0, corner_radius=0, undo=True)
        self.text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.text.bind("<KeyRelease>", lambda _: self.update_counts())
        self.text.bind("<<Paste>>", lambda _: self.after(10, self.update_counts))

    def build_settings(self, body):
        panel = self.card(body)
        panel.grid(row=0, column=1, sticky="ns")
        panel.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 4))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Settings", font=(FONT, 15, "bold"), text_color=TEXT).grid(row=0, column=0, sticky="w")
        self.ghost_button(head, "Recommended", self.apply_recommended, 110).grid(row=0, column=1)

        scroll = ctk.CTkScrollableFrame(
            panel, width=300, fg_color="transparent",
            scrollbar_button_color=TRACK, scrollbar_button_hover_color=FILL)
        scroll.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 12))
        scroll.grid_columnconfigure(0, weight=1)
        self.settings_scroll = scroll

        row = 0
        for title, specs in SECTIONS:
            ctk.CTkLabel(scroll, text=title.upper(), font=(FONT, 11, "bold"), text_color=MUTED).grid(
                row=row, column=0, sticky="w", padx=10, pady=(14, 2))
            row += 1
            for spec in specs:
                self.slider_row(scroll, row, *spec)
                row += 1

        ctk.CTkLabel(scroll, text="CODE", font=(FONT, 11, "bold"), text_color=MUTED).grid(
            row=row, column=0, sticky="w", padx=10, pady=(14, 2))
        row += 1
        self.code_mode = ctk.StringVar(value="Off")
        self.segmented(scroll, list(MODE_HELP), self.code_mode, self.on_mode_change).grid(
            row=row, column=0, sticky="ew", padx=10, pady=(8, 4))
        row += 1
        self.mode_help = self.help_label(scroll)
        self.mode_help.grid(row=row, column=0, sticky="w", padx=10, pady=(0, 4))
        row += 1

        # Natural only: which editor, and how it indents.
        self.editor = ctk.StringVar(value="Auto")
        self.indent_unit = ctk.StringVar(value="4 spaces")
        self.editor_label = ctk.CTkLabel(scroll, text="Editor", font=(FONT, 13), text_color=TEXT)
        self.editor_label.grid(row=row, column=0, sticky="w", padx=10, pady=(10, 0))
        row += 1
        self.editor_picker = self.segmented(scroll, list(EDITOR_HELP), self.editor, self.on_mode_change)
        self.editor_picker.grid(row=row, column=0, sticky="ew", padx=10, pady=(4, 4))
        row += 1
        self.editor_help = self.help_label(scroll)
        self.editor_help.grid(row=row, column=0, sticky="w", padx=10, pady=(0, 4))
        row += 1
        self.indent_label = ctk.CTkLabel(scroll, text="Editor's indent", font=(FONT, 13), text_color=TEXT)
        self.indent_label.grid(row=row, column=0, sticky="w", padx=10, pady=(8, 0))
        row += 1
        self.indent_picker = self.segmented(scroll, list(INDENT_UNITS), self.indent_unit, None)
        self.indent_picker.grid(row=row, column=0, sticky="ew", padx=10, pady=(4, 8))
        row += 1

        # Classic only.
        self.editor_indents = ctk.BooleanVar(value=False)
        self.indent_switch = ctk.CTkSwitch(
            scroll, text="Let the editor indent", variable=self.editor_indents, font=(FONT, 13),
            text_color=TEXT, text_color_disabled=MUTED, progress_color=FILL,
            button_color=("#ffffff", "#ececee"), button_hover_color=("#f4f4f5", "#ffffff"), fg_color=TRACK)
        self.indent_switch.grid(row=row, column=0, sticky="w", padx=10, pady=(8, 2))
        row += 1
        self.indent_help = self.help_label(
            scroll, "Drops your text's indentation and lets the editor place each line. "
                    "Java, C++, JS. Not Python.")
        self.indent_help.grid(row=row, column=0, sticky="w", padx=10, pady=(0, 8))
        self.on_mode_change()

    def slider_row(self, parent, row, key, label, lo, hi, default, step, unit):
        self.recommended[key] = default
        var = ctk.DoubleVar(value=default)
        self.vars[key] = var

        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row, column=0, sticky="ew", padx=10, pady=(6, 0))
        box.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(box, text=label, font=(FONT, 13), text_color=TEXT).grid(row=0, column=0, sticky="w")
        value = ctk.CTkLabel(box, font=(FONT, 12), text_color=MUTED)
        value.grid(row=0, column=1, sticky="e")

        def refresh(_=None):
            snapped = round(var.get() / step) * step
            var.set(snapped)
            if key == "break_every" and snapped == 0:
                value.configure(text="off")
            else:
                value.configure(text=f"{snapped:g} {unit}")
            self.update_counts()

        ctk.CTkSlider(
            box, from_=lo, to=hi, variable=var, command=refresh, height=14,
            fg_color=TRACK, progress_color=FILL, button_color=TEXT, button_hover_color=FILL,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        self.refreshers.append(refresh)
        refresh()

    def build_footer(self):
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 18))
        foot.grid_columnconfigure(3, weight=1)

        self.start_btn = ctk.CTkButton(
            foot, text="Start", command=self.start, width=110, height=36, corner_radius=8,
            fg_color=TEXT, hover_color=FILL, text_color=CARD, font=(FONT, 13, "bold"))
        self.start_btn.grid(row=0, column=0)
        self.pause_btn = self.ghost_button(foot, "Pause  F8", self.toggle_pause, 96)
        self.pause_btn.configure(height=36, state="disabled")
        self.pause_btn.grid(row=0, column=1, padx=8)
        self.stop_btn = self.ghost_button(foot, "Stop  Esc", self.stop, 96)
        self.stop_btn.configure(height=36, state="disabled")
        self.stop_btn.grid(row=0, column=2)

        status = ctk.CTkFrame(foot, fg_color="transparent")
        status.grid(row=0, column=3, sticky="ew", padx=(20, 0))
        status.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(status, text="Paste text, then hit Start.", font=(FONT, 12), text_color=MUTED, anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(status, height=4, fg_color=TRACK, progress_color=FILL)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.progress.set(0)

    # -- behaviour ------------------------------------------------------------

    def settings(self):
        s = {k: v.get() for k, v in self.vars.items()}
        s["countdown"] = int(s["countdown"])
        s["code_mode"] = self.code_mode.get().lower() if hasattr(self, "code_mode") else "off"
        # "Let the editor indent" is a refinement of Classic, so it only counts there.
        s["editor_indents"] = s["code_mode"] == "classic" and self.editor_indents.get()
        s["editor"] = self.editor.get().lower() if hasattr(self, "editor") else "auto"
        s["indent_unit"] = INDENT_UNITS[self.indent_unit.get()] if hasattr(self, "indent_unit") else "    "
        if s["wpm_min"] > s["wpm_max"]:  # sliders crossed; treat them as a range anyway
            s["wpm_min"], s["wpm_max"] = s["wpm_max"], s["wpm_min"]
        return s

    def on_mode_change(self, _=None):
        mode = self.code_mode.get()
        self.mode_help.configure(text=MODE_HELP[mode])
        self.editor_help.configure(text=EDITOR_HELP[self.editor.get()])
        natural = mode == "Natural"
        show = {
            self.editor_label: natural, self.editor_picker: natural, self.editor_help: natural,
            self.indent_label: natural and self.editor.get() != "Auto",
            self.indent_picker: natural and self.editor.get() != "Auto",
            self.indent_switch: mode == "Classic", self.indent_help: mode == "Classic",
        }
        for widget, visible in show.items():  # only show the options that mean something
            widget.grid() if visible else widget.grid_remove()

    def load_settings(self):
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # first launch, or a broken file: keep the defaults
        for key, var in self.vars.items():
            if isinstance(saved.get(key), (int, float)):
                var.set(saved[key])
        for var, options, key in ((self.code_mode, MODE_HELP, "code_mode"),
                                  (self.editor, EDITOR_HELP, "editor"),
                                  (self.indent_unit, INDENT_UNITS, "indent_unit")):
            if saved.get(key) in options:
                var.set(saved[key])
        self.editor_indents.set(bool(saved.get("editor_indents", False)))
        for refresh in self.refreshers:
            refresh()
        self.on_mode_change()

    def save_settings(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data["code_mode"] = self.code_mode.get()
        data["editor"] = self.editor.get()
        data["indent_unit"] = self.indent_unit.get()
        data["editor_indents"] = self.editor_indents.get()
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass  # not worth interrupting typing over

    def on_close(self):
        self.save_settings()
        self.destroy()

    def apply_recommended(self):
        for key, value in self.recommended.items():
            self.vars[key].set(value)
        for refresh in self.refreshers:
            refresh()

    def update_counts(self):
        if not hasattr(self, "counts") or not hasattr(self, "indent_unit"):
            return  # still building the window
        text = self.text.get("1.0", "end-1c")
        if not text.strip():
            self.counts.configure(text="")
            return
        s = self.settings()
        words = len(text.split())
        secs = len(text) * 60.0 / ((s["wpm_min"] + s["wpm_max"]) / 2 * 5)
        secs += words * (s["word_gap"] / 2 + s["think_chance"] / 100 * (s["think_min"] + s["think_max"]) / 2)
        secs += sum(text.count(c) for c in SENTENCE_END) * 0.7 * s["punct_pause"]
        secs += text.count("\n") * 1.6 * s["punct_pause"]
        secs += len(text) * s["typo_rate"] / 100 * 1.2
        if s["code_mode"] == "off":
            secs += words / 12 * s["rewrite_rate"] / 100 * 5
        secs *= 1 + min(0.4, s["fatigue"] / 100 * secs / 600) / 2
        if s["break_every"]:
            secs += int(secs / 60 / s["break_every"]) * s["break_len"]
        m, sec = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        took = f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"
        self.counts.configure(text=f"{words} words  ·  about {took}")

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt *.md"), ("All files", "*.*")])
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.text.delete("1.0", "end")
                self.text.insert("1.0", f.read())
            self.update_counts()

    def set_status(self, msg):
        self.after(0, lambda: self.status.configure(text=msg))

    def set_progress(self, done, total, wpm):
        # The first few seconds are too short for a meaningful WPM.
        speed = f"  ·  {wpm:.0f} WPM actual" if done >= 25 else ""
        self.after(0, lambda: (self.progress.set(done / total),
                               self.status.configure(text=f"Typing  {done} / {total} characters{speed}")))

    def start(self):
        text = self.text.get("1.0", "end-1c")
        if not text.strip() or self.typer:
            return
        self.save_settings()
        self.typer = Typer(self.settings(), self.set_status, self.set_progress, self.finished)
        self.progress.set(0)
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause  F8")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self.typer.run, args=(text,), daemon=True).start()

    def toggle_pause(self):
        if not self.typer:
            return
        if self.typer.paused.is_set():
            self.typer.paused.clear()
            self.after(0, lambda: self.pause_btn.configure(text="Pause  F8"))
        else:
            self.typer.paused.set()
            self.after(0, lambda: self.pause_btn.configure(text="Resume  F8"))

    def stop(self):
        if self.typer:
            self.typer.stopped.set()

    def finished(self, msg):
        def reset():
            self.typer = None
            self.status.configure(text=msg)
            self.start_btn.configure(state="normal")
            self.pause_btn.configure(state="disabled", text="Pause  F8")
            self.stop_btn.configure(state="disabled")
        self.after(0, reset)

    def on_hotkey(self, key, injected=False):
        if injected:  # our own keystrokes (code mode taps Esc) must not trigger hotkeys
            return
        if key == keyboard.Key.f8:
            self.toggle_pause()
        elif key == keyboard.Key.esc:
            self.stop()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on the G14's scaled display
    except Exception:
        pass
    ctk.set_appearance_mode("system")
    App().mainloop()
