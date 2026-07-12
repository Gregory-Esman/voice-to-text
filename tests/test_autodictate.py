"""Unit tests for windows/autodictate.py — run with the project venv:
  .venv\\Scripts\\python.exe tests\\test_autodictate.py
Covers: endpointer segmentation, specials, command/action/delete/snippet
matchers, glossary-echo detection, noise filter, speaker-gate plumbing.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "windows"))
import autodictate as ad  # noqa: E402

SR = 16_000
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def voice(sec, f0=150.0, amp=0.3):
    t = np.arange(int(sec * SR)) / SR
    carrier = np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(2 * np.pi * 2 * f0 * t)
    syll = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    return (carrier * syll * amp).astype("float32")


def silence(sec):
    return (np.random.randn(int(sec * SR)) * 0.002).astype("float32")


def feed_all(ep, audio, block=512):
    outs = []
    for i in range(0, len(audio) - block, block):
        u = ep.feed(audio[i:i + block])
        if u is not None:
            outs.append(u)
    return outs


# ── endpointer ──
ep = ad.Endpointer()
check("silence only -> no utterance", not feed_all(ep, silence(3.0)) and not ep.speaking)
ep.reset()
outs = feed_all(ep, np.concatenate([silence(0.5), voice(1.2), silence(1.5)]))
check("voice burst -> exactly one utterance", len(outs) == 1)
ep.reset()
outs = feed_all(ep, np.concatenate([silence(0.4), voice(1.0), silence(1.3),
                                    voice(0.8), silence(1.3)]))
check("two bursts -> two utterances", len(outs) == 2)
ep.reset()
outs = feed_all(ep, np.concatenate([silence(0.4), voice(0.8), silence(0.5),
                                    voice(0.8), silence(1.3)]))
check("0.5s mid-pause does not split", len(outs) == 1)
ep.reset()
check("60s cap closes a runaway utterance",
      len(feed_all(ep, np.concatenate([voice(65.0), silence(0.2)]))) >= 1)

# ── specials ──
check("'Scratch that.' -> scratch", ad.special_of("Scratch that.") == "scratch")
check("'SEND IT!' -> send", ad.special_of("SEND IT!") == "send")
check("'Sender.' -> send (mishear)", ad.special_of("Sender.") == "send")
check("'scratch that idea entirely' -> None",
      ad.special_of("scratch that idea entirely") is None)
check("'send it to John' -> None", ad.special_of("send it to John") is None)

# ── command routing ──
check("'Write a letter to my landlord' -> command",
      ad.is_command("Write a letter to my landlord"))
check("'reply saying I can't make it' -> command",
      ad.is_command("Reply saying I can't make it"))
check("'and also reply with a paragraph of well wishes' -> command (lead-in)",
      ad.is_command("and also reply with a paragraph of well wishes."))
check("'I want to write more often' -> dictation",
      not ad.is_command("I want to write more often"))
check("'Reply hazy, try again' -> dictation", not ad.is_command("Reply hazy, try again"))
check("'Also add a paragraph of well wishes.' -> maybe-command",
      ad.is_maybe_command("Also add a paragraph of well wishes."))
check("'make it shorter' -> maybe-command", ad.is_maybe_command("make it shorter"))
check("'I'll be there at five' -> not maybe",
      not ad.is_maybe_command("I'll be there at five"))
check("'happy birthday maggie' -> not maybe",
      not ad.is_maybe_command("Happy birthday, Maggie!"))

# ── app actions ──
check("'Switch to Slack' -> slack", ad.action_of("Switch to Slack.") == "slack")
check("'open chrome' -> chrome", ad.action_of("open chrome") == "chrome")
check("'now switch to slack' -> slack (lead-in)",
      ad.action_of("Now, switch to Slack") == "slack")
check("'switch to' alone -> None", ad.action_of("switch to") is None)

# ── delete commands ──
check("'Remove the last word.' -> (word,1)",
      ad.delete_of("Remove the last word.") == ("word", 1))
check("'delete the last three words' -> (word,3)",
      ad.delete_of("delete the last three words") == ("word", 3))
check("'remove last 5 words' -> (word,5)",
      ad.delete_of("remove last 5 words") == ("word", 5))
check("'Erase the last sentence' -> (sentence,1)",
      ad.delete_of("Erase the last sentence") == ("sentence", 1))
check("'and remove the last word' -> delete (lead-in)",
      ad.delete_of("and remove the last word") == ("word", 1))
check("'remove the last cookie' -> None", ad.delete_of("remove the last cookie") is None)

t = "Hello there. How are you today?"
check("delete 1 word", t[:-ad.chars_to_delete(t, "word", 1)] == "Hello there. How are you")
check("delete 3 words", t[:-ad.chars_to_delete(t, "word", 3)] == "Hello there. How")
check("delete 1 sentence", t[:-ad.chars_to_delete(t, "sentence", 1)] == "Hello there.")
check("delete 2 sentences -> all", ad.chars_to_delete(t, "sentence", 2) == len(t))
check("delete from empty -> 0", ad.chars_to_delete("", "word", 1) == 0)

# ── personal snippets + fixers ── (fictional fixtures — never real details)
P = {"name": "Jamie Rivera", "email": "jamierivera@example.com"}
check("'Type my email.' -> address", ad.snippet_of("Type my email.", P) == P["email"])
check("'enter my name' -> name", ad.snippet_of("enter my name", P) == P["name"])
check("'my email is broken' -> None", ad.snippet_of("my email is broken", P) is None)
FX = ad.build_fixers(P, {"jamie riviera": "Jamie Rivera"})
check("spoken email fixed",
      ad.apply_fixers("Contact me at Jamie Rivera at example dot com please", FX)
      == "Contact me at jamierivera@example.com please")
check("misspelling replacement",
      ad.apply_fixers("Hi, this is Jamie Riviera speaking", FX)
      == "Hi, this is Jamie Rivera speaking")
check("unrelated text untouched", ad.apply_fixers("Nothing to fix here.", FX)
      == "Nothing to fix here.")

# ── glossary-echo detection ──
check("transcript == email -> echo", ad.is_prompt_echo("jamierivera@example.com", P))
check("transcript == 'Jamie Rivera.' -> echo", ad.is_prompt_echo("Jamie Rivera.", P))
check("real sentence -> not echo",
      not ad.is_prompt_echo("Hey Maggie, I hope you have a great day", P))
check("email inside sentence -> not echo",
      not ad.is_prompt_echo("Reach me at jamierivera@example.com anytime", P))
check("empty -> not echo", not ad.is_prompt_echo("", P))

# ── noise filter ──
check("'Ahem.' -> noise", ad.is_noise("Ahem."))
check("'*coughs*' -> noise", ad.is_noise("*coughs*"))
check("'Ahem, let's get started' -> NOT noise", not ad.is_noise("Ahem, let's get started"))

# ── speaker gate plumbing ──
prof = os.path.join(tempfile.mkdtemp(), "prof.npy")
gate = ad.SpeakerGate(prof, threshold=0.75)
check("gate deps available", gate.available())
gate.enroll(voice(20.0))
ok, score = gate.accept(voice(2.5))
check("same synthetic voice accepted", ok)
ok2, score2 = gate.accept(np.random.randn(int(2.5 * SR)).astype("float32") * 0.3)
check("noise scores lower", score2 < score)

print(("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
