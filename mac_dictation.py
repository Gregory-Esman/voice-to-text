"""Dictation cleanup + clean-during-pauses streaming for the macOS app.

Ports windows/streaming.py's "#3 design" (clean pause-delimited chunks in the
background while you talk, so only the final short tail is left to process at
tap-stop) onto flow.py's FlowApp. flow.py lazy-imports this module from three
hooks (_finalize_dictation / _maybe_start_dictation_stream /
_finish_dictation_stream) and fails open to the raw transcript if anything
here errors — see flow.py's hook wrappers for the fallback behavior.

Both STT and LLM cleanup route through the app's own backend selection
(app.transcribe_for_auto for STT; vtt_core.chat_complete's base_url switch for
cleanup — "" = local Ollama, set = OpenAI-compatible/Groq). Never hardcoded to
one backend, and [personal] is NEVER fed to Whisper as vocabulary bias (only
used to build regex fixers for spoken email/name forms).
"""
import portable
from portable import vtt_core, autodictate, streaming


def build_fixers_from_cfg(cfg: dict) -> list:
    """[(regex, replacement)] built from [personal] (spoken email/name forms)
    and [replacements]. Port of windows/app.py's _rebuild_personal (~L686-701).
    [personal] is used ONLY to build these fixers — never passed to Whisper."""
    personal = {str(k).lower(): str(v)
                for k, v in (cfg.get("personal") or {}).items() if v}
    replacements = cfg.get("replacements") or {}
    return autodictate.build_fixers(personal, replacements)


def _clean_backend(cfg: dict) -> tuple:
    """(ollama_url, model, base_url, api_key_env, api_key_file) for the cleanup
    LLM call. base_url == "" is the single source of truth that routes
    vtt_core.chat_complete to local Ollama at ollama_url; a non-empty
    base_url routes to that OpenAI-compatible endpoint (Groq/OpenAI) instead."""
    fcfg = cfg.get("formatting", {})
    dcfg = cfg.get("dictation", {})
    ollama_url = fcfg.get("ollama_url", "http://localhost:11434")
    command_base_url = (fcfg.get("command_base_url") or "").strip()
    if command_base_url:
        return (ollama_url, dcfg.get("model", "llama-3.1-8b-instant"),
                command_base_url, fcfg.get("command_api_key_env", "OPENAI_API_KEY"),
                fcfg.get("command_api_key_file", ""))
    return (ollama_url, dcfg.get("model_local", "llama3.1:8b"), "", "", "")


def clean_scope(cfg: dict) -> str:
    """"final" = one cleanup pass over the WHOLE transcript at tap-stop (the
    chunks are only transcribed while you talk). "chunk" = the old behaviour,
    one cleanup call per pause-delimited chunk.

    "final" exists because a lone chunk cannot be punctuated correctly: the
    audio is cut at a pause, Whisper ends every clip it is handed with a period,
    and a cleanup model shown one fragment has no way to know the sentence
    continues. Only a pass over the whole transcript can tell a breath from a
    full stop."""
    scope = str(cfg.get("dictation", {}).get("clean_scope") or "final").lower()
    return scope if scope in ("final", "chunk") else "final"


def clean_chunk(cfg: dict, raw: str, prev: str, whole: bool = False) -> str:
    """Clean dictation text into written text, continuing from `prev`. ANY
    exception (network down, bad key, model not pulled…) returns "" — the
    stream worker treats "" as "drop this chunk" and finalize()/finish_stream()
    fall back to the raw transcript, so a cleanup hiccup never loses your words.

    `whole` says this call sees the entire transcript, which is what unlocks
    sentence-boundary repair (stitching fragments a hesitation split apart)."""
    try:
        ollama_url, model, base_url, api_key_env, api_key_file = _clean_backend(cfg)
        dcfg = cfg.get("dictation", {})
        tone = dcfg.get("tone") or None
        return vtt_core.clean_dictation(
            raw, url=ollama_url, model=model, prev=prev, tone=tone,
            base_url=base_url, api_key_env=api_key_env, api_key_file=api_key_file,
            stitch=bool(whole and dcfg.get("stitch_fragments", True)),
            strict_lists=bool(dcfg.get("strict_lists", True)))
    except Exception:
        return ""


def transcribe_chunk(app, audio, fixers) -> str:
    """Transcribe one streamed chunk: speech gate -> app's active STT backend
    -> repeat-collapse -> personal/replacement fixers -> lexical/hallucination
    gates. Returns "" to drop the chunk (silence, noise, or a phantom)."""
    if not vtt_core.contains_speech(audio):
        return ""
    text = (app.transcribe_for_auto(audio) or "").strip()
    text = vtt_core.collapse_repeats(text)
    text = autodictate.apply_fixers(text, fixers)
    if not vtt_core.has_lexical_content(text) or vtt_core.is_hallucination(text):
        return ""
    return text


def maybe_start_stream(app):
    """Start a clean-during-pauses DictationStream for this recording, or None
    when it doesn't apply: [dictation] clean/stream both off, or the active
    transcription backend is "assemblyai" (it already streams live, so a
    second background pipeline would be redundant work for nothing)."""
    cfg = app.cfg
    dcfg = cfg.get("dictation", {})
    if not (dcfg.get("clean") and dcfg.get("stream")):
        return None
    backend = (cfg.get("transcription", {}).get("backend") or "local").lower()
    if backend == "assemblyai":
        return None
    fixers = build_fixers_from_cfg(cfg)
    # In "final" scope the per-chunk clean is the identity function: chunks are
    # still TRANSCRIBED in your pauses (that's the slow, network-bound half, and
    # the whole point of streaming), but the cleanup LLM is deferred to one
    # full-context pass in finish_stream().
    if clean_scope(cfg) == "chunk":
        clean_fn = lambda raw, prev: clean_chunk(app.cfg, raw, prev)  # noqa: E731
    else:
        clean_fn = lambda raw, prev: raw  # noqa: E731
    dstream = streaming.DictationStream(
        snapshot=app.recorder.snapshot,
        transcribe=lambda audio: transcribe_chunk(app, audio, fixers),
        clean=clean_fn,
        log=_stream_log,
        min_silence=float(dcfg.get("pause_seconds", 0.7)),
    )
    dstream.start()
    return dstream


def _stream_log(fmt, *args):
    """Route the DictationStream's internal chunk trace into the app log. It was
    previously left unset (a no-op), which is why per-chunk behaviour — the
    exact place the text was being fragmented — was invisible in the log."""
    try:
        from flow import log
        log("  " + (fmt % args if args else fmt))
    except Exception:
        pass


def finish_stream(app, dstream, audio) -> str:
    """Stop the stream, drain the transcribed chunks + final tail, and return
    the assembled text — or "" if nothing survives the lexical/hallucination
    gates.

    In "final" scope this is where the single whole-transcript cleanup runs, so
    the model can see the entire dictation and repair sentence boundaries that a
    mid-sentence pause split apart. Falls back to the raw assembled text if the
    cleanup errors or returns nothing — a cleanup failure must never lose
    words."""
    text = dstream.finish(audio)
    if not text or not vtt_core.has_lexical_content(text) or vtt_core.is_hallucination(text):
        return ""
    cfg = app.cfg
    if cfg.get("dictation", {}).get("clean") and clean_scope(cfg) == "final":
        cleaned = clean_chunk(cfg, text, prev="", whole=True)
        if cleaned and vtt_core.has_lexical_content(cleaned):
            text = cleaned
        else:
            _stream_log("stream: final cleanup returned nothing — using raw transcript")
    return vtt_core.start_case(text)


def finalize(app, text: str) -> str:
    """Non-streaming cleanup path: apply personal/replacement fixers, then (if
    [dictation] clean is on) run one clean_dictation pass over the whole
    transcript — falling back to the raw (fixed-up) text if the cleaner
    errors or returns nothing. start_case + fix_urls run EVEN when clean is
    off: deterministic casing/URL fixing is a headline feature on its own."""
    cfg = app.cfg
    fixers = build_fixers_from_cfg(cfg)
    text = autodictate.apply_fixers(text, fixers)
    if cfg.get("dictation", {}).get("clean"):
        cleaned = clean_chunk(cfg, text, prev="", whole=True)
        text = cleaned or text
    return vtt_core.start_case(text)
