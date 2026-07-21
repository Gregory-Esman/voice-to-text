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


def clean_chunk(cfg: dict, raw: str, prev: str) -> str:
    """Clean one chunk of raw dictation into written text, continuing from
    `prev`. ANY exception (network down, bad key, model not pulled…) returns
    "" — the stream worker treats "" as "drop this chunk" and finalize() falls
    back to the raw transcript, so a cleanup hiccup never loses your words."""
    try:
        ollama_url, model, base_url, api_key_env, api_key_file = _clean_backend(cfg)
        tone = cfg.get("dictation", {}).get("tone") or None
        return vtt_core.clean_dictation(
            raw, url=ollama_url, model=model, prev=prev, tone=tone,
            base_url=base_url, api_key_env=api_key_env, api_key_file=api_key_file)
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
    dstream = streaming.DictationStream(
        snapshot=app.recorder.snapshot,
        transcribe=lambda audio: transcribe_chunk(app, audio, fixers),
        clean=lambda raw, prev: clean_chunk(app.cfg, raw, prev),
    )
    dstream.start()
    return dstream


def finish_stream(app, dstream, audio) -> str:
    """Stop the stream, drain the already-cleaned chunks + final tail, and
    return the assembled text (sentence-cased, URL-fixed) — or "" if nothing
    survives the lexical/hallucination gates."""
    text = dstream.finish(audio)
    if not text or not vtt_core.has_lexical_content(text) or vtt_core.is_hallucination(text):
        return ""
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
        cleaned = clean_chunk(cfg, text, prev="")
        text = cleaned or text
    return vtt_core.start_case(text)
